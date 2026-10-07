"""写操作的人审通道（macOS 原生弹窗）—— 让"AI 要改真机配置"必须经人手点一下。

设计原则：
1. **fail closed**：无 GUI / osascript 失败 / 超时 / 关窗 → 一律当"拒绝"，不发送任何东西。
2. **不可被 AI 自行放行**：本通道只认"人在弹窗上点了允许"。
   AI 唯一能做的就是"发起弹窗"，点不了。
3. **放宽策略也要人批**：把 writes 从 ask 放宽到 allow，同样要走这个弹窗。
4. **全部留档**：每一次询问（允许/拒绝/超时）都写 logs/approvals.log。
"""
from __future__ import annotations
import re

import json
import os
import pathlib
import subprocess
import time

# 2026-10-03 修：原来写死 `home()/"netops"`。装到 --prefix /opt/netops 之类的地方时，
# 这里会去读 ~/netops/state/policy.json —— 读不到就静默回落成 "ask"，
# 看着"安全"，实际上**用户的策略和审计流水都写到了另一个目录**。
# 现在与全项目共用 lib/paths.py 的解析结果（唯一真源）。
from . import host, paths as _paths

ROOT = _paths.ROOT
POLICY = _paths.state_dir() / "policy.json"
AUDIT = _paths.runtime_dir("logs") / "approvals.log"
DEVICES = _paths.cfg("devices.toml")
DEFAULT_TIMEOUT = 60

# 网页审批通道：若设了（由 netdev-ui 注入给 AI 子进程），审批优先走界面自己的弹窗，
# 风格统一；不可达时再回退到 macOS 原生弹窗。
APPROVAL_URL = os.environ.get("NETDEV_APPROVAL_URL", "").strip()


MODES = ("readonly", "ask", "allow")          # 只读模式 / 确认模式 / 放行模式
DEFAULT_ALLOW_MIN = 300                        # 放行模式默认 TTL：5 小时后自动回落确认模式
MODE_LABEL = {"readonly": "只读模式", "ask": "确认模式", "allow": "放行模式"}


def _raw() -> dict:
    try:
        d = json.loads(POLICY.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def mode_info() -> dict:
    """生效模式 + 放行到期时间。放行带 TTL，过期自动回落到确认模式。"""
    d = _raw()
    m = d.get("writes", "ask")
    if m not in MODES:
        m = "ask"
    until = d.get("allow_until")
    remaining = None
    if m == "allow" and isinstance(until, (int, float)):
        remaining = int(max(0, until - time.time()))
        if remaining <= 0:
            m, remaining = "ask", None
    return {"mode": m, "label": MODE_LABEL[m], "allow_until": until, "remaining": remaining,
            "file_mode": d.get("writes", "ask")}


def policy() -> str:
    """当前生效的写操作策略。"""
    return mode_info()["mode"]


def set_policy(value: str, minutes: int | None = None):
    """设置模式；allow 可带 TTL（分钟），到期自动回落 ask。"""
    if value not in MODES:
        raise ValueError(f"模式只能是 {'/'.join(MODES)}")
    POLICY.parent.mkdir(parents=True, exist_ok=True)
    d = {"writes": value}
    if value == "allow":
        m = minutes if minutes else DEFAULT_ALLOW_MIN      # 不给 TTL 也自动 5 小时
        d["allow_until"] = time.time() + m * 60
    POLICY.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def redact_cmd(lines) -> list:
    """审批留档前，把命令里的明文密码抹掉。
    为什么：实测 approvals.log 里留下了整条
      local-user admin password irreversible-cipher <明文>
    —— 命令本身要留档（审计用），但密码不能留。"""
    out = []
    for ln in (lines or []):
        t = str(ln)
        # password/passwd/secret 后面跟的 token（含 irreversible-cipher / cipher / simple 等修饰）
        t = re.sub(r"(?i)(\b(?:password|passwd|secret)\s+(?:irreversible-cipher|cipher|simple)\s+)\S+",
                   r"\1[REDACTED]", t)
        t = re.sub(r"(?i)(\b(?:password|passwd|secret)\s*[=:]\s*)\S+", r"\1[REDACTED]", t)
        t = re.sub(r"(?i)(\bset\s+authentication\s+password\s+(?:simple|plain|cipher)?\s*)\S+", r"\1[REDACTED]", t)
        t = re.sub(r"(?i)(snmp-agent\s+community\s+(?:read|write)\s+)\S+", r"\1[REDACTED]", t)
        out.append(t)
    return out


def _audit(device: str, lines: list, decision: str, latency: float, via: str = "dialog"):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {decision:<7} {device:<18} via={via:<8} "
                    + " | ".join(redact_cmd(lines))[:220] + f"   ({latency:.1f}s)\n")
    except Exception:
        pass


def _message(device: str, lines: list, kind: str) -> str:
    head = {"screen-send": "AI 要往设备控制台直接打字",
            "apply": "AI 要下发配置变更",
            "save": "AI 要把配置写入设备 flash",
            "policy": "有人要把写操作策略放宽为'不再询问'"}.get(kind, "AI 要执行写操作")
    shown = [str(x) for x in lines][:8]
    body = "\n".join(f"  {i}. {c[:56]}" for i, c in enumerate(shown, 1))
    more = f"\n  …（共 {len(lines)} 条）" if len(lines) > len(shown) else ""
    return (f"{head}\n\n设备：{device}\n\n{body}{more}\n\n"
            f"点「允许」才会发送；{DEFAULT_TIMEOUT} 秒无操作 = 拒绝。")


def _ask_web(device: str, lines: list, kind: str, timeout: int):
    """走「网页审批通道」（netdev-ui 提供）。

    返回 True/False；通道根本不可用（没设 URL / UI 没开）返回 None，由调用方决定回退。
    约定：UI 侧提供 POST <base>/api/ask，收到后把请求推给浏览器弹窗，
          等人在网页上点「允许/拒绝」，再把 {'allow': bool} 返回。
    拿不到回答（超时/断连）一律当拒绝（fail closed）。
    """
    if not APPROVAL_URL:
        # ★ 回退也要留痕（2026-09-30）：之前这里静默 return None，日志只见 via=dialog，
        #   无法区分「没配 URL」还是「网页没开」，排障靠猜（实测踩过）。
        _audit(device, lines, "DENY", 0.0, "web:no-url(环境变量 NETDEV_APPROVAL_URL 为空)")
        return None
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(
            APPROVAL_URL.rstrip("/") + "/api/ask",
            data=_json.dumps({"device": device, "lines": [str(x) for x in lines],
                              "kind": kind, "timeout": int(timeout)}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=int(timeout) + 15) as r:
            d = _json.loads((r.read() or b"{}").decode("utf-8", "replace"))
        if d.get("available") is False:   # 界面没开着 / 页面是旧版 → 交给调用方回退原生弹窗
            _audit(device, lines, "DENY", 0.0,
                   f"web:unavailable({d.get('reason', '界面未轮询')})")
            return None
        return bool(d.get("allow"))
    except Exception as e:
        _audit(device, lines, "DENY", 0.0, f"web:{type(e).__name__}")
        return None


def _is_local_sim(name: str) -> bool:
    """该设备名在 devices.toml 里是不是「本机模拟器」。

    判据两条同时成立才算：host 是回环地址 **且** 显式标了 sim = true。
    少一条都不算 —— 免得有人把真机的 host 写成 127.0.0.1 来骗过闸门。
    """
    try:
        import tomllib
        data = tomllib.loads(DEVICES.read_text(encoding="utf-8"))
    except Exception:
        return False
    for d in data.get("device", []) or []:
        if str(d.get("name", "")).strip() != str(name).strip():
            continue
        if not d.get("sim"):
            return False
        return str(d.get("host", "")).strip() in ("127.0.0.1", "localhost", "::1")
    return False


def _selftest_sim_bypass(device: str) -> bool:
    """离线自检（`netdev selftest`）打的是本机模拟器，**没有真人可点弹窗**。

    这条豁免是给 CI 和"没人在旁边时先验一遍"用的，因此条件必须窄到能被证伪：
      1) 环境变量 NETDEV_SELFTEST=1 —— 只有 selftest 命令会设，且用完立刻清掉；
      2) 目标设备在本机模拟器上（见 _is_local_sim）；
      3) 目标主机是回环地址（_is_local_sim 里已含）。
    **任何真实设备都不豁免**，照旧走人审；豁免时也一定写审计（via=selftest:sim）。
    """
    if os.environ.get("NETDEV_SELFTEST", "").strip() not in ("1", "true", "yes"):
        return False
    return _is_local_sim(device)


def _ask_win_dialog(msg: str, title: str, timeout: int) -> tuple[bool, str]:
    """Windows 原生审批弹窗（user32.MessageBoxW），返回 (是否允许, 原因)。

    语义对齐 macOS osascript 版（红线：fail-closed 不许放松）：
      · 默认焦点在「否」（MB_DEFBUTTON2），人只能主动点「是」才放行；
      · 超时：看门狗线程按标题找到弹窗，直接点「否」→ 拒绝；
      · 无 GUI / 调用失败 → 拒绝。
    """
    import ctypes
    import threading
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # MB_YESNO=0x4, MB_ICONWARNING=0x30, MB_DEFBUTTON2=0x100,
    # MB_TOPMOST=0x40000, MB_SETFOREGROUND=0x10000, MB_TASKMODAL=0x2000
    FLAGS = 0x4 | 0x30 | 0x100 | 0x40000 | 0x10000 | 0x2000
    IDYES, IDNO = 6, 7

    result = {"id": 0}
    timed_out = {"v": False}
    box_ready = threading.Event()

    def _box():
        try:
            user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW.restype = ctypes.c_int
            box_ready.set()
            result["id"] = user32.MessageBoxW(0, msg, title, FLAGS)
        except Exception:
            result["id"] = 0

    t = threading.Thread(target=_box, daemon=True)
    t.start()
    box_ready.wait(2.0)

    def _watchdog():
        # 超时后按标题找本进程的弹窗，点「否」按钮（控件 ID=7，不受语言影响）
        t.join(max(1, int(timeout)))
        if not t.is_alive():
            return
        timed_out["v"] = True
        found = []

        def _enum(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum), 0)
        for hwnd in found:
            btn = user32.GetDlgItem(hwnd, IDNO)
            if btn:
                BM_CLICK = 0x00F5
                user32.SendMessageW(btn, BM_CLICK, 0, 0)
        t.join(5)
        if t.is_alive():
            for hwnd in found:
                WM_COMMAND = 0x0111
                user32.PostMessageW(hwnd, WM_COMMAND, IDNO, 0)
            t.join(3)

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()
    t.join(timeout + 10)

    rid = result["id"]
    if rid == IDYES:
        return True, "人点了允许"
    if timed_out["v"]:
        return False, f"超时未点({int(timeout)}s)"
    if rid == IDNO:
        return False, "人点了拒绝"
    return False, "弹窗失败/无 GUI"


def ask(device: str, lines: list, *, kind: str = "screen-send", timeout: int = DEFAULT_TIMEOUT) -> bool:
    """弹原生弹窗问人。返回 True 仅当人点了「允许」。"""
    lines = [str(x) for x in (lines or [])]
    mi = mode_info()
    if mi["mode"] == "readonly":
        _audit(device, lines, "DENY", 0.0, "policy:readonly")
        return False
    if mi["mode"] == "allow":
        _audit(device, lines, "ALLOW", 0.0, f"policy:allow{'(TTL)' if mi['remaining'] else ''}")
        return True

    if _selftest_sim_bypass(device):
        _audit(device, lines, "ALLOW", 0.0, "selftest:sim(本机模拟器·离线自检)")
        return True

    # ── 优先走「网页审批通道」（界面统一风格）；不可达才回退原生弹窗 ──
    w = _ask_web(device, lines, kind, timeout)
    if w is not None:
        _audit(device, lines, "ALLOW" if w else "DENY", 0.0, "web")
        return w

    msg = _message(device, lines, kind)
    if host.IS_WIN:
        t0 = time.time()
        ok, why = _ask_win_dialog(msg, "netdev 写操作审批（人审）", int(timeout))
        _audit(device, lines, "ALLOW" if ok else "DENY", time.time() - t0,
               "dialog" if ok else f"dialog:{why}")
        return ok
    short = msg.split("\n")[0]
    script = (
        'set msg to ' + json.dumps(msg, ensure_ascii=False) + "\n"
        'set shortMsg to ' + json.dumps(short, ensure_ascii=False) + "\n"
        'try\n'
        '  display notification shortMsg with title "netdev：AI 请求写操作，等你批准" sound name "Glass"\n'
        'end try\n'
        'beep 2\n'
        'tell application "System Events" to activate\n'
        'try\n'
        '  set r to display dialog msg with title "netdev 写操作审批（人审）" '
        'buttons {"拒绝", "允许"} default button "拒绝" cancel button "拒绝" '
        f'with icon caution giving up after {int(timeout)}\n'
        '  return "BTN=" & (button returned of r) & "|GAVEUP=" & (gave up of r)\n'
        'on error errm\n'
        '  return "ERROR=" & errm\n'
        'end try\n'
    )
    t0 = time.time()
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout + 20)
        out = (r.stdout or "").strip()
    except Exception as e:
        out = f"ERROR|{type(e).__name__}: {e}"

    ok = ("BTN=允许" in out) and ("GAVEUP=false" in out)
    if out.startswith("ERROR=") or not out:
        why = "弹窗失败/无 GUI"
    elif "GAVEUP=true" in out:
        why = f"超时未点({int(timeout)}s)"
    elif ok:
        why = "人点了允许"
    elif "BTN=拒绝" in out:
        why = "人点了拒绝"
    else:
        why = "关窗/未识别"
    _audit(device, lines, "ALLOW" if ok else "DENY", time.time() - t0,
           "dialog" if ok else f"dialog:{why}[raw={out[:48]}]")
    return ok


def ask_policy_relax(timeout: int = DEFAULT_TIMEOUT) -> bool:
    """把策略放宽到 allow —— 本身也要人批。"""
    return ask("-", ["policy: writes → allow（以后写操作不再询问）"], kind="policy", timeout=timeout)
