#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netdev-ui —— netdev 的独立界面（本地 HTTP 服务，纯 Python 标准库，零第三方依赖）

设计原则（与 netdev 保持一致）
  1. 能力留在 netdev：本服务 **不 import netdev 内部模块**，只调它的 CLI（netdev list / run / apply …）
     —— 进程边界就是护栏的物理基础，AI 与界面都无法绕过 lib/gates.py
  2. 终端是"人机同屏"：attach 到 netdev 建的 tmux 设备窗格，你和 AI 看的是同一块屏
  3. 每个浏览器标签一个专属 tmux 会话（用 tmux 会话组共享 windows）
     —— 各自有各自的"当前窗口"，多个标签互不抢屏
  4. 写操作必须人点：审批通道独立于 AI（人机分离），AI 拿不到确认入口
  5. 只绑定 127.0.0.1：本机工具，不对外监听

用法
  python3 ~/netops/ui/server.py [--port 8898] [--host 127.0.0.1]
  浏览器打开 http://127.0.0.1:8898
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import http.server
import json
import os
import pathlib
import pty
import queue
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse

HOME = pathlib.Path.home()
# 2026-10-03：ui/server.py 是**直接跑的**（`python ui/server.py`），此时 sys.path[0]
# 是 ui/ 而不是项目根。下面 from lib import ... 会 ModuleNotFoundError（实测踩到：
# 服务根本起不来）。所以先把项目根算出来放进 sys.path，再导入 paths。
_ROOT_SELF = pathlib.Path(
    os.environ.get("NETDEV_ROOT") or pathlib.Path(__file__).resolve().parent.parent
).expanduser().resolve()
if str(_ROOT_SELF) not in sys.path:
    sys.path.insert(0, str(_ROOT_SELF))
from lib import paths as _P          # noqa: E402  路径统一真源
ROOT = _P.ROOT
STATIC = pathlib.Path(__file__).resolve().parent / "static"
# 版本号单一真源：dist/installer/VERSION（发布流水线写它）。
# 原来 /api/health 里硬编码 "0.1"，而 VERSION 是 1.0.0 —— 同一件事两个答案，
# 排障时会被误当成"装的是旧版"。读不到就退回一个明确的开发版号，不要瞎猜。
def _app_version() -> str:
    vf = ROOT / "dist" / "installer" / "VERSION"
    try:
        v = vf.read_text(encoding="utf-8").strip()
        return v or "dev"
    except Exception:
        return "dev"


APP_VERSION = _app_version()
TMUX_SESSION = "netops"          # netdev 的设备窗格住在这个会话里

# ── 找 tmux（本机 PATH 可能被 shim 动过，所以显式列候选）───────────────────
def find_tmux() -> str | None:
    cands = [shutil.which("tmux"),
             str(HOME / "homebrew/bin/tmux"),
             "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"]
    for p in cands:
        if p and pathlib.Path(p).exists():
            return p
    return None

TMUX = find_tmux()


def netdev_json(args: list[str], timeout: int = 20):
    """调 netdev CLI 并解析 JSON。失败返回 None（绝不抛给调用方）。"""
    cli = ROOT / "netdev"
    if not cli.exists():
        return None
    try:
        r = subprocess.run([str(cli), *args], capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def raw_netdev(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    cli = ROOT / "netdev"
    # ★ 网页审批通道：把 UI 自己的地址带给 netdev CLI 子进程，
    #   让它审批走「网页弹窗」（/api/ask）而不是 macOS 原生 osascript 弹窗。
    #   之前漏了这里 —— 界面里点「下发配置」时 approval 读到空的
    #   NETDEV_APPROVAL_URL，就会回退到系统弹窗（用户 2026-09-30 反馈）。
    env = dict(os.environ)
    if UI_BASE:
        env["NETDEV_APPROVAL_URL"] = UI_BASE
    try:
        r = subprocess.run([str(cli), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _which(name: str) -> str | None:
    """在几个常见位置找可执行文件（本机 PATH 常被 shim 动过，不能只靠 which）。"""
    p = shutil.which(name)
    if p:
        return p
    for d in (str(HOME / ".npm-global/bin"), "/usr/local/bin", "/opt/homebrew/bin",
              str(HOME / "homebrew/bin"), str(HOME / ".local/bin")):
        c = pathlib.Path(d) / name
        if c.exists():
            return str(c)
    return None


def _ver(cmd: str) -> str:
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=8)
        line = ((r.stdout or "").strip().splitlines() or [""])[0]
        return line[:48]
    except Exception:
        return ""


def _file_ok(p: pathlib.Path) -> bool:
    """只看存在与大小 —— 凭证文件绝不读取内容。"""
    try:
        return p.is_file() and p.stat().st_size > 2
    except Exception:
        return False


def _env_with_node() -> dict:
    """给子进程一个能找到 node 的 PATH（本机 node 在 /usr/local/bin）。"""
    env = dict(os.environ)
    extra = [str(HOME / ".npm-global/bin"), "/usr/local/bin", "/opt/homebrew/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


def _rpc_startable(cmd: str, args: list[str], wait: float = 4.0) -> tuple[bool, str]:
    """把 agent 的 RPC 进程真的拉起来一次，看 wait 秒内是否存活。
    不发任何 prompt ⇒ 不消耗额度。"""
    try:
        p = subprocess.Popen([cmd, *args], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, env=_env_with_node(), cwd=str(HOME),
                             start_new_session=True)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    time.sleep(wait)
    alive = p.poll() is None
    why = ""
    if not alive:
        try:
            why = (p.stderr.read() or b"").decode("utf-8", "replace").strip()[:140]
        except Exception:
            pass
        why = why or f"进程已退出（code={p.returncode}）"
    try:
        p.terminate()
        time.sleep(0.2)
        p.kill()
    except Exception:
        pass
    return alive, why


# ══════════════════════════════════════════════════════════════════════════
#  pi 启动自愈（2026-10-01 实测踩到，必须固化成产品能力）
#
#  事故链（真机复现，非猜测）：
#    1) pi 的 settings.json 里若声明了 packages（如 npm:pi-web-access），
#       每次启动都会执行 `npm install <pkg> --prefix ~/.pi/agent/npm --legacy-peer-deps`；
#    2) 国内访问 registry.npmjs.org 会 502/超时（实测 70s 后 E502）；
#    3) pi 用 proper-lockfile 做配置锁 —— 锁就是一个空目录（mkdir 原子锁）
#       `~/.pi/agent/{settings,auth,models-store}.json.lock`；
#       崩溃时 signal-exit 的清理没跑到 ⇒ **锁目录永久残留**；
#    4) 之后每次启动都是 `EEXIST: mkdir '.../settings.json.lock'`
#       ⇒ settings.json 读不了 ⇒ pi 静默回退内置默认 provider（google）
#       ⇒ 报 "No API key found for the selected model"（报错点离真因十万八千里）；
#    5) netdev 侧的表现是「AI 三后端里 pi 起不来」，而 auth.json 明明在。
#
#  所以这里做两件事：**能探测出来** + **能自愈**（移入隔离区，不物理删除）。
# ══════════════════════════════════════════════════════════════════════════
PI_DIR = HOME / ".pi/agent"
PI_LOCK_NAMES = ("settings.json.lock", "auth.json.lock", "models-store.json.lock")
PI_LOCK_STALE_SEC = 60          # 正在持有的锁会被 proper-lockfile 持续 utimes 刷新


def _pi_stale_locks() -> list[pathlib.Path]:
    """列出「崩溃残留」的 pi 锁目录。

    三条同时满足才算（避免误删正在使用的锁）：
      1) 是**目录** —— proper-lockfile 用 mkdir 当原子锁；
      2) 是**空的** —— 真锁目录里不放任何东西；
      3) mtime 距今 > PI_LOCK_STALE_SEC —— 活锁的 mtime 会被持续刷新。
    """
    out = []
    now = time.time()
    for name in PI_LOCK_NAMES:
        p = PI_DIR / name
        try:
            if not p.is_dir():
                continue
            if any(p.iterdir()):          # 非空 → 不是锁目录，别碰
                continue
            if now - p.stat().st_mtime < PI_LOCK_STALE_SEC:
                continue                  # 太新 → 可能是正在持有的锁
            out.append(p)
        except Exception:
            continue
    return out


def pi_heal_locks(dry: bool = False) -> dict:
    """把崩溃残留的 pi 锁目录移入隔离区。**只移动、不删除**，可原样还原。"""
    stale = _pi_stale_locks()
    if not stale:
        return {"ok": True, "found": 0, "moved": [], "quarantine": None}
    if dry:
        return {"ok": True, "found": len(stale), "moved": [], "quarantine": None,
                "would_move": [p.name for p in stale]}
    # 隔离区放在 ~/.pi/agent **之外** —— 别往 pi 自己会扫描的配置目录里塞东西
    q = HOME / ".quarantine-pi-locks" / time.strftime('%Y%m%d_%H%M%S')
    moved = []
    try:
        q.mkdir(parents=True, exist_ok=True)
        for p in stale:
            try:
                shutil.move(str(p), str(q / p.name))
                moved.append(p.name)
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "found": len(stale), "moved": moved, "quarantine": str(q),
                "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "found": len(stale), "moved": moved, "quarantine": str(q)}


def _pi_settings_defaults() -> tuple[str, str]:
    """直接读 pi 的 settings.json，取 defaultProvider/defaultModel。

    为什么 netdev 要自己读：settings.json 一旦因锁残留读取失败，pi 会**静默**回退到
    内置默认 provider（google）。netdev 显式把 provider/model 传下去，等于堵死这条
    「配置没生效但表面看不出来」的降级路径。读不到就返回空串（不改变原行为）。
    """
    try:
        d = json.loads((PI_DIR / "settings.json").read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return "", ""
        return str(d.get("defaultProvider") or "").strip(), str(d.get("defaultModel") or "").strip()
    except Exception:
        return "", ""


def _pi_first_provider() -> str:
    """从 auth.json 里取出第一个 provider 名（**只读键名，不读任何 Key 内容**）。

    用途：settings.json 读不到时，`pi auth check` 还得有个 provider 可问。
    取不出来就返回空串，调用方自己兜底。
    """
    try:
        d = json.loads((PI_DIR / "auth.json").read_text(encoding="utf-8"))
        if isinstance(d, dict):
            for k in d:
                if isinstance(k, str) and k.strip():
                    return k.strip()
    except Exception:
        pass
    return ""


def _pi_auth_ready(provider: str = "") -> tuple[bool, str]:
    """用 pi 自己的 `auth check` 判「这个 provider 到底有没有可用凭据」。

    浅探测的 `auth.json 存在` 太乐观 —— 实测文件在、内容对，pi 依然因锁读不到，
    照样报 "No API key found"。所以这里真的问 pi 一次（`--no-refresh` 纯离线，不耗额度）。
    """
    pi = _which("pi")
    if not pi:
        return False, "未安装 pi"
    prov = provider or _pi_first_provider()
    if not prov:
        return False, "settings.json 与 auth.json 都没给出 provider（无法判定凭据）"
    args = [pi, "auth", "check", "--no-refresh", "--json", "--provider", prov]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15, env=_env_with_node())
        raw = (r.stdout or "").strip()
        d = json.loads(raw) if raw.startswith("{") else {}
        st = str(d.get("status") or "").strip()
        if st == "ready":
            return True, f"凭据就绪（{d.get('provider', prov)} · {d.get('authType', 'api_key')}）"
        why = d.get("reason") or (r.stderr or raw or "无输出")
        return False, f"凭据不可用（provider={prov}）：{str(why)[:120]}"
    except Exception as e:
        return False, f"auth check 失败：{type(e).__name__}: {e}"


def probe_agents(deep: bool = False) -> dict:
    """探测本机可用的 AI agent 后端 —— 目标是“装上就能用，不用手配”。

    浅探测：本机有没有这个 CLI（快）
    深探测：真的把它拉起来一次 + 看凭证在不在（仍不消耗额度）
    verdict：ready 能接入 / no-auth 装了没登录 / broken 起不来 / missing 没装
    """
    out = []
    pia = HOME / ".pi/agent"

    # ── pi agent ──
    pi = _which("pi")
    prov, mdl = _pi_settings_defaults()
    stale = _pi_stale_locks()
    item = {"id": "pi", "name": "pi agent", "available": bool(pi), "path": pi,
            "version": _ver(pi) if pi else "", "mode": "RPC · JSONL 双向", "tested": True,
            "hint": "npm i -g @earendil-works/pi-coding-agent",
            "authed": _file_ok(pia / "auth.json") or _file_ok(pia / "provider-keys.json"),
            "default_provider": prov or "（pi 内置默认）",
            "default_model": mdl or "（pi 内置默认）",
            "auth_note": "~/.pi/agent/auth.json 或 provider-keys.json"}
    # 崩溃残留的锁目录：**这不只是提示，是 pi 起不来的头号真因**，必须显式报出来
    if stale:
        item["stale_locks"] = [p.name for p in stale]
        item["fix"] = (f"检测到 {len(stale)} 个崩溃残留的锁目录（{', '.join(p.name for p in stale)}）；"
                       "打开 AI 会话时会自动移入 ~/.quarantine-pi-locks/ 修复，也可手删。"
                       "残留锁会让 pi 读不到 settings.json，"
                       "进而静默回退默认 provider 并报『No API key found』。")
        item["authed"] = False        # 有残留锁时不能谎报 ready
    if pi and deep:
        # 深探测前先自愈（否则锁残留必然误判成 broken）
        heal = pi_heal_locks()
        if heal.get("moved"):
            item["healed"] = heal["moved"]
            item["stale_locks"] = []   # 已修好 → 清掉「待修复」标记，免得界面还催你修
        ok, why = _rpc_startable(pi, ["--mode", "rpc", "--no-session"])
        item["deep"] = {"ok": ok, "detail": "RPC 进程启动正常（未发 prompt）" if ok else why}
        if ok:
            aok, anote = _pi_auth_ready(prov)
            item["authed"] = aok
            item["auth_note"] = anote
    out.append(item)

    # ── 直连 API Key（自实现 agent loop，复用 netdev_mcp 工具与护栏）──
    d_ok, d_note = _direct_cfg_state()
    keys = [k for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                        "GEMINI_API_KEY", "OPENROUTER_API_KEY") if os.environ.get(k)]
    out.append({"id": "direct", "name": "直连 API Key", "available": d_ok, "path": None,
                "version": "", "mode": "自实现 agent loop（直连 OpenAI 兼容 API）",
                "tested": False, "env_keys": keys, "authed": d_ok,
                "auth_note": d_note,
                "hint": "设 NETDEV_DIRECT_API_KEY + NETDEV_DIRECT_BASE_URL，"
                        "或写 config/direct.json"})

    # ── WorkBuddy agent（桌面版自带 codebuddy CLI，2026-09-29 加）──
    wbb = _wb_bin()
    wb_ok, wb_note = _wb_token_state()
    item = {"id": "wb", "name": "WorkBuddy agent", "available": bool(wbb), "path": wbb,
            "version": _ver(wbb) if wbb else "",
            "mode": "headless · -p --output-format stream-json", "tested": False,
            "hint": "随 WorkBuddy 桌面版安装；走 custom-token 免登录直连，无需 /login",
            "authed": wb_ok, "auth_note": wb_note}
    if wbb and deep:
        ok, why = _wb_deep_probe(wbb)
        item["deep"] = {"ok": ok, "detail": why}
    out.append(item)

    # ── 判定 verdict ──
    for a in out:
        if not a["available"]:
            a["verdict"] = "missing"
        elif a.get("authed"):
            a["verdict"] = "ready"
        else:
            a["verdict"] = "no-auth"
        if deep and a.get("deep") and not a["deep"].get("ok"):
            a["verdict"] = "broken"

    rank = {"ready": 0, "no-auth": 1, "broken": 2, "missing": 3}
    rec = ""
    for a in sorted(out, key=lambda x: rank[x["verdict"]]):
        if a["verdict"] == "ready":
            rec = a["id"]
            break
    return {"agents": out, "recommended": rec, "deep": deep,
            "note": "ready=能接入 / no-auth=装了但未见凭证 / broken=起不来 / missing=未安装；"
                    "深探测不会发送任何 prompt，因此不消耗额度"}


# ══════════════════════════════════════════════════════════════════════════
#  终端会话：一个浏览器标签  ↔  一个专属 tmux 会话（共享 netops 的窗格）
# ══════════════════════════════════════════════════════════════════════════
class TermSession:
    BACKLOG_MAX = 300_000        # 回放缓冲上限（字节）

    def __init__(self, sid: str, dev: str, window: str):
        self.sid = sid
        self.dev = dev
        self.window = window
        self.tmux_session = f"ui-{sid}"     # 保留（仅作标识），不再建会话
        self._client_tty: str | None = None   # 自己那个 tmux client 的 tty
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self.subs: set[queue.Queue] = set()
        self.backlog = bytearray()
        self.lock = threading.Lock()
        self.alive = False
        self.created = time.time()
        self.last_active = time.time()      # 最近一次输入/订阅，供空闲回收用

    # ── 起会话 ──
    def start(self, rows: int = 40, cols: int = 140) -> None:
        if not TMUX:
            raise RuntimeError("找不到 tmux（同屏终端依赖它）")
        # ★ 2026-09-26 改回【直接 attach netops】，不再建 ui-* 临时会话。
        #   原因：临时会话是独立 session，其 client 尺寸被 tmux 锁死
        #   （实测 pty 改到 200x60，client 仍停在 98x31），
        #   于是最大化后 window 逻辑尺寸虽然变成 194x55，屏幕只画 30 行 → 下方大片空白。
        #   直接 attach netops 时 client 尺寸跟着 pty 走，最大化正常。
        #   代价：多个终端同时 attach netops 会共享“当前窗口”（切窗口互相影响）。
        # 1) 切到目标设备窗格
        subprocess.run([TMUX, "select-window", "-t", f"{TMUX_SESSION}:{self.window}"],
                       capture_output=True, timeout=10)
        # 3) attach 进伪终端
        master, slave = pty.openpty()
        self._set_winsize(master, rows, cols)
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            [TMUX, "attach", "-t", TMUX_SESSION],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, env=env, close_fds=True)
        os.close(slave)
        self.master = master
        self.alive = True
        # 记下自己这个 client 的 tty（close 时要 detach 它）
        try:
            self._client_tty = os.ttyname(master)
        except Exception:
            self._client_tty = None
        # 立刻把窗格尺寸控制权从 client 手里拿回来（否则多 client 会把它锁在最小值）
        try:
            subprocess.run([TMUX, "set-option", "-t", self.tmux_session, "window-size", "manual"],
                           capture_output=True, timeout=6)
            subprocess.run([TMUX, "resize-window", "-t", f"{TMUX_SESSION}:{self.window}",
                            "-x", str(cols), "-y", str(rows)],
                           capture_output=True, timeout=6)
        except Exception:
            pass
        threading.Thread(target=self._reader, daemon=True).start()

    # ── pty 尺寸 ──
    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            rows = max(5, min(300, int(rows)))
            cols = max(20, min(500, int(cols)))
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """调整终端尺寸。

        ★ 2026-09-26 修正（终端最大化后底部大片空白）：
        tmux 的窗格尺寸 = 所有连着的 client 里的【最小值】。
        原来这里只改了自己的 pty 尺寸 —— 对 tmux 来说等于没说，
        所以最大化后 xterm 变高了、tmux 窗格还是按旧行列画，下方就是空白。
        现在：改完 pty 后，再用 refresh-client -C 告诉 tmux
        「我这个 client（用它的 tty 标识）现在是 WxH」。
        """
        if self.master is None:
            return
        self._set_winsize(self.master, rows, cols)          # ① 自己的 pty 尺寸
        # ①b 显式通知 tmux 尺寸变了 —— 关键的一步。
        #      tmux 只在 attach 时读一次 pty 尺寸；之后必须收到 SIGWINCH 才重读。
        #      ioctl(TIOCSWINSZ) 虽然会触发该信号，但实测没送到 tmux 那边
        #      （client 一直停在 98x31，window 却已 194x55 → 屏幕只画 30 行）。
        #      这里直接给 tmux 进程组发一次 SIGWINCH，让它重读 pty 尺寸。
        try:
            if self.proc and self.proc.poll() is None:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGWINCH)
        except Exception:
            pass
        try:
            sess = self.tmux_session                        # ★ 是我们的 ui-* 临时会话
            tty = None
            try:
                tty = os.ttyname(self.master)               # 例如 /dev/ttys062
            except Exception:
                pass
            # ② window-size manual 【设在自己的会话上】
            #    踩过的坑：原来设在 netops 上，但终端用的是 ui-* 临时会话 ——
            #    选项是会话级的，设在别处等于没设；实测最大化后 window-size 仍是 latest，
            #    于是窗口尺寸被 client（98x31）拽住，终端只画上面一小块。
            subprocess.run([TMUX, "set-option", "-t", sess, "window-size", "manual"],
                           capture_output=True, timeout=6)
            # ③ 把 client 的尺寸也改掉（tmux 按 client 决定实际绘制范围）
            #    只改 window 不改 client：状态会显示 194x55，但屏幕上只画 98x31 那么大。
            if tty:
                subprocess.run([TMUX, "refresh-client", "-C", f"{cols}x{rows}", "-t", tty],
                               capture_output=True, timeout=6)
            # ④ 真正把窗格尺寸设成 xterm 算出来的行列
            subprocess.run([TMUX, "resize-window", "-t", f"{TMUX_SESSION}:{self.window}",
                            "-x", str(cols), "-y", str(rows)],
                           capture_output=True, timeout=6)
        except Exception:
            pass

    # ── 读 pty → 广播 ──
    def _reader(self) -> None:
        while self.alive:
            try:
                data = os.read(self.master, 8192)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.backlog += data
                if len(self.backlog) > self.BACKLOG_MAX:
                    del self.backlog[: len(self.backlog) - self.BACKLOG_MAX // 2]
                for q in list(self.subs):
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass
        self.alive = False
        # 通知所有订阅者：结束
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    # ── 写 pty（键盘输入）──
    def write(self, data: bytes) -> None:
        if self.master is None or not self.alive:
            return
        self.last_active = time.time()
        try:
            os.write(self.master, data)
        except OSError:
            self.alive = False

    # ── 订阅（SSE）──
    def subscribe(self) -> tuple[queue.Queue, bytes]:
        q: queue.Queue = queue.Queue(maxsize=2000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            snap = bytes(self.backlog)
        return q, snap

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        if self.master is not None:
            try:
                os.close(self.master)
            except Exception:
                pass
            self.master = None
        # ★ 不能 kill-session —— 现在直接 attach 的是 netops 本体！
        #   改成 detach 我们自己那个 client（按 tty 认）。
        if TMUX:
            try:
                if self._client_tty:
                    subprocess.run([TMUX, "detach-client", "-t", self._client_tty],
                                   capture_output=True, timeout=8)
            except Exception:
                pass
        self.alive = False


SESSIONS: dict[str, TermSession] = {}
SESS_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
#  AI 会话：一个 pi RPC 子进程  ↔  一个浏览器 AI 面板
#    要点：
#      · pi 以 --mode rpc 常驻，JSONL 双向（ stdin 发命令 / stdout 收事件）
#      · dialog 类 UI 请求（select/confirm/input/editor）是阻塞的，
#        只有界面里的人能回答 ⇒ 人机分离的审批通道，AI 答不了
#      · 本轮不限制 pi 的工具集（有完整权限）—— 收紧需要 pi 扩展，见 README
# ══════════════════════════════════════════════════════════════════════════
PI_BIN = _which("pi")

# 工具白名单（只作用于界面启动的那个 pi 子进程）——
#   关键：netdev 的 MCP 工具在 pi 里是一个命名空间代理 `mcp__netdev`（参数 {tool,args}），
#         不是 netdev_list 这种独立名字——写成独立名字会被当成不存在的工具全部过滤掉。
#   read        : 只看不碰设备（最安全）
#   read+netdev : 多给 mcp__netdev（写操作会走 netdev 自身的闸门/人审）
#   full        : 不加限制（= 你终端里那个 pi 的完整能力，仅供对比）
# 共通点：不给 bash / edit / write / nyaterm ⇒ AI 没有 shell，绕不过 netdev。
_ND = "mcp__netdev"
TOOLSETS = {
    "read": "read,ls,grep,find",
    "read+netdev": "read,ls,grep,find," + _ND,
    "full": "",
}


def self_win(sess) -> str:
    """取一个 TermSession 的窗格名（用于日志）。"""
    try:
        return getattr(sess, "window", "") or ""
    except Exception:
        return ""


# ── 终端会话清理（2026-09-26 加）────────────────────────────────────────
#   问题：面板标题栏的 ✕ 只关面板，不会调 /api/term/close，
#        于是 ui-* 临时会话和 tmux client 一路累积（实测攒到 4 个）。
#        多 client 会互相拖累窗格尺寸，还会让新终端 attach 到混乱状态。
#   做法：开终端前先清理该设备相关的残留；服务启动时也清一次。
def cleanup_term_sessions(dev: str = "", keep_sid: str = "") -> dict:
    """杀掉该设备相关的 ui-* 临时会话（不动 netops 本体）。返回清理明细。"""
    if not TMUX:
        return {"killed": [], "detached": []}
    killed, detached = [], []
    # ① 先清掉不 attach 的僵尸 client
    try:
        r = subprocess.run([TMUX, "list-clients", "-F", "#{client_tty} #{client_session} #{client_attached}"],
                           capture_output=True, text=True, timeout=8)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not (len(parts) > 2 and parts[2] == "1"):
                tty, sess = parts[0], parts[1]
                if sess.startswith("ui-") and (not keep_sid or keep_sid not in sess):
                    subprocess.run([TMUX, "detach-client", "-t", tty], capture_output=True, timeout=6)
                    detached.append(tty)
    except Exception:
        pass
    # ② 杀掉该设备相关的 ui-* 会话
    #    会话名形如 ui-<sid>；我们用"这个 sid 是否属于同一设备"来判断
    with SESS_LOCK:
        want = {sid for sid, sess in SESSIONS.items()
                if (not dev or getattr(sess, "dev", "") == dev) and sid != keep_sid}
    try:
        r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=8)
        for name in (r.stdout or "").split():
            if not name.startswith("ui-"):
                continue
            sid = name[3:]
            # 只杀"已不在 SESSIONS 里"或"属于目标设备"的
            if sid in want or sid not in SESSIONS:
                subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True, timeout=8)
                killed.append(name)
    except Exception:
        pass
    # ③ 把那些已死会话从内存表里摘掉
    with SESS_LOCK:
        for sid in [k for k, v in SESSIONS.items() if not getattr(v, "alive", False)]:
            SESSIONS.pop(sid, None)
    return {"killed": killed, "detached": detached}


# ── AI 人设：设备调试助手（2026-09-26 加）──────────────────────────────
#   为什么要这个：原来 AI 是"一个通用助手，恰好有 netdev 工具"——
#   它不知道自己正在调试一台真实设备、不知道操作会显示在人眼前、
#   不知道写操作要先说计划。用户反馈过"它自己就干完了""我看不到它在干什么"。
#   这里用 --append-system-prompt 把"设备调试助手"的行为准则刻进人设。
#   （选择 append 而不是 replace：保留 pi 原有的通用规范与安全约定）
DEBUG_SYSTEM_PROMPT = """你是【网络设备调试助手】，正在协助一位网络工程师调试真实设备。

关键前提（务必时刻记住）：
· 你操作的是一台/几台【真实设备】，不是模拟环境。
· 你的每一个动作都会【实时显示在工程师的屏幕上】（人机同屏）—— 他看得见你调用
  哪个工具、敲了哪条命令、设备回了什么。
· 你手上有一台设备的【多条通道】（例如 huawei=串口、huawei-ssh=SSH 是同一台设备）。
  用户消息里会明确给出【你正在调试的设备】，**所有 netdev 操作都用那个设备名，
  不要自己换成其它通道**。

工作准则：
1. 只读操作（display / show / dir / ping）直接执行，不要问、不要先解释。
   · 命令必须【一次性完整下发】（如 netdev_run 传 "display clock"），
     【严禁】把一个词拆成单个字符（d/i/s/p/l/a/y）逐个发 —— 那是错误的
     调用方式，会被写操作护栏拦成"已拒绝"。
2. 写操作（改配置）之前，必须先用一两句话说明清楚：
     · 要改什么（具体命令级别）
     · 为什么改
     · 风险与回滚方式（例如 undo vlan 888）
   说清楚之后再调 netdev_apply —— 它会弹审批给工程师确认。
   **绝不要**招呼都不打就直接下发配置。
3. 不要凭记忆回答"设备上有没有 X 配置" —— 去查（netdev_run / netdev_diff）。
   尤其注意：就算你刚才查过、工程师也可能已经手动改过了 —— 下结论前【重新查】。
4. 每次操作后确认结果（看回显、看返回的 ok / error），不要假设成功。
5. 不确定就说不确定，绝不编造。设备没回应就说"没读到"。
6. 发现异常（配置不符预期、报错、接口 down…）先报告给工程师，
   不要自己尝试修复。
7. 回答用中文，简洁、直接、给结论。涉及配置变更时用列表列清楚。

信息出处：工具返回里带 _identity（device / tool / at），
那是"这份数据来自哪台设备、什么时候取的"—— 引用数据时以此为准，
不要把不同设备、不同时间的数据混为一谈。

★ 认知保鲜（最关键的一条，务必执行）：
你调试的是【人机同屏】的真实设备，工程师会【在终端里手动改配置】——
你【看不见】他改了什么，你上下文里记的"设备状态"可能已经过时。
因此：
· 凡是【下结论】或【改配置】之前，必须先用 netdev_run（如
  display current-configuration / display vlan / display interface brief）
  或 netdev_diff【重查设备当前态】，不要凭上文记忆判断。
· 引用任何工具结果时，先看它的 _identity.at 时间戳——如果那是一次【很久之前】
  或【上一轮对话之前】取到的数据，就要先重查，再基于新结果下结论。
· 你记忆里的"设备上有 vlan 887"只是一条【旧快照】，工程师可能已经删了；
  判断设备现状的唯一依据是【刚刚查到的回显】，不是你的记忆。

★ 实时查询禁止翻留档（务必执行）：
本机 logs/ 目录下有设备的【历史回显留档】（*.log 文件）。这些是"过去某个时刻
抓到的快照"，不是"设备现在"的状态。因此：
· 凡是问【现在】的时间、状态、配置、接口、路由、vlan、流量等，
  一律用 netdev_run 实时查设备（如 display clock / display current-configuration），
  【禁止】用 Read/Glob/Grep 去翻 logs/ 下的留档来回答"现在是什么"。
· Read/Glob/Grep 只允许用于：看你自己落盘的工具结果摘要、读项目文档/配置模板，
  不允许用来代替"实时查设备"。
"""


class AiSession:
    def __init__(self, aid: str, model: str = "", cwd: str | None = None, tools: str = "read+netdev"):
        self.aid = aid
        self.model = (model or "").strip()
        self.tools_key = tools if tools in TOOLSETS else "read+netdev"
        self.tools = TOOLSETS[self.tools_key]
        self.cwd = cwd or str(ROOT)
        self.proc: subprocess.Popen | None = None
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.alive = False
        self.last_active = time.time()
        self.ready = threading.Event()      # 收到首个事件 = pi 已完成初始化
        self.log: list[dict] = []          # 事件回放（新订阅者补发，限量）
        self.stderr_tail = ""
        self.heal: dict = {}               # 启动时的锁自愈结果（供界面显示）

    # ── 起进程 ──
    def start(self) -> None:
        if not PI_BIN:
            raise RuntimeError("找不到 pi（npm i -g @earendil-works/pi-coding-agent）")
        # ★ 开箱即用：先自愈崩溃残留的锁目录。
        #   不清的话 pi 必然 `EEXIST: mkdir 'settings.json.lock'` → 读不到 settings.json
        #   → 静默回退内置默认 provider → 报 "No API key found"（真因被藏起来）。
        self.heal = pi_heal_locks()
        cmd = [PI_BIN, "--mode", "rpc", "--no-session"]
        if self.tools:                       # 白名单：只给这一子进程，不影响用户自己的 pi
            cmd += ["--tools", self.tools]
        # ★ 人设：把它变成"设备调试助手"，而不是通用助手（见上方 DEBUG_SYSTEM_PROMPT）
        try:
            cmd += ["--append-system-prompt", DEBUG_SYSTEM_PROMPT]
        except Exception:
            pass
        if self.model:
            cmd += ["--model", self.model]
        else:
            # ★ 显式把 pi settings.json 里的默认 provider/model 传下去。
            #   不传的话，settings.json 一旦读取失败，pi 会静默用 google，
            #   用户看到的是 "No API key found for the selected model"，完全指不到根因。
            _prov, _mdl = _pi_settings_defaults()
            if _mdl:
                cmd += ["--model", (_prov + "/" + _mdl) if _prov else _mdl]
        env = _env_with_node()
        env.pop("PI_OFFLINE", None)
        if UI_BASE:      # 把「网页审批通道」地址交给子进程（它拉起的 MCP 也会继承）
            env["NETDEV_APPROVAL_URL"] = UI_BASE
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     text=True, bufsize=1, cwd=self.cwd, env=env)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            for line in self.proc.stdout:               # 严格按 \n 分行
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    ev = {"type": "raw", "text": line[:400]}
                self.ready.set()
                with self.lock:
                    self.log.append(ev)
                    if len(self.log) > 600:
                        del self.log[:200]
                    for q in list(self.subs):
                        try:
                            q.put_nowait(ev)
                        except queue.Full:
                            pass
        except Exception:
            pass
        self.alive = False
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def _err_reader(self) -> None:
        try:
            for line in self.proc.stderr:
                self.stderr_tail = (self.stderr_tail + line)[-800:]
        except Exception:
            pass

    # ── 发命令（stdin）──
    def _write(self, obj: dict) -> bool:
        if not (self.proc and self.proc.poll() is None):
            return False
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            self.last_active = time.time()
            return True
        except Exception:
            return False

    def prompt(self, text: str) -> bool:
        # pi 冷启动要几秒（node + 配置 + MCP）；太早写 stdin 会被丢弃 ⇒ 等它就绪
        self.ready.wait(timeout=15)
        return self._write({"type": "prompt", "message": text})

    def abort(self) -> bool:
        return self._write({"type": "abort"})

    def compact(self, instructions: str = "") -> bool:
        """手动压缩上下文（抗长对话污染 —— pi 会把旧消息压成结构化摘要）。"""
        msg = {"type": "compact"}
        if instructions:
            msg["customInstructions"] = instructions
        return self._write(msg)

    def stats(self) -> bool:
        """问 pi 当前会话的 token 用量。"""
        return self._write({"type": "get_session_stats"})

    def respond_ui(self, req_id: str, value=None, cancelled: bool = False) -> bool:
        """回应 dialog 类 UI 请求（审批通道）。"""
        msg = {"type": "extension_ui_response", "id": req_id}
        if cancelled:
            msg["cancelled"] = True
        else:
            msg["value"] = value
        return self._write(msg)

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=3000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            replay = list(self.log[-120:])
        return q, replay

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        # 先把这个 client 从 tmux 摘掉（不然它会以"最小尺寸"拖着窗格）
        try:
            if self.master is not None:
                tty = os.ttyname(self.master)
                subprocess.run([TMUX, "detach-client", "-t", tty],
                               capture_output=True, timeout=6)
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


AI_SESSIONS: dict[str, AiSession] = {}
AI_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
#  WorkBuddy agent 后端（2026-09-29 加；同日二次修订：打通免登录直连）
#    形态：WorkBuddy 桌面版自带的 codebuddy CLI，headless 调用
#         （-p --output-format stream-json），每轮一个短进程，
#         用 --session-id / --resume 维持多轮上下文。
#
#  ── 六个硬坑（均本机实测，不写在这里必再踩）────────────────────────────
#   ① CLI 启动时要绑一个内部服务端口，缺省读共享配置 cell.server.port
#      （本机 = 54805，被 WorkBuddy 桌面 App 的引擎长期占用）。
#      冲突时【静默挂死】：无任何输出、无退出，连 `codebuddy ps` 也挂。
#      解法：必须用 SERVER__PORT 环境变量指定一个空闲端口。
#   ② CLI 的交互式 TUI 在桌面版里被裁掉了 —— dist/ 只有
#      codebuddy-lite-wb.mjs 与 codebuddy-headless.js，没有 dist/codebuddy。
#      所以【终端里根本没有 /login 这条命令可用】，别再让人去终端登录。
#   ③ 桌面 App 写出的 auth/workbuddy-desktop.info 里 accessToken 是
#      $wbEncrypted 信封；而独立启动的 CLI 走 standalone 凭据保护模式，
#      【解不开】⇒ 永远报 "Authentication required"。解密钥匙由宿主进程
#      经 CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET 反向推给子进程，
#      第三方 UI 拿不到，也不再尝试。
#   ④ ★正解：走 CLI 内置的 custom-token 通道
#      （CustomTokenAuthenticationStorage，优先级最高，无信封、无钥匙串）：
#        CODEBUDDY_AUTH_TOKEN      = <裸 JWT>
#        ACC_PRODUCT_CONFIG_PATH   = <产品配置副本，authentication.type=custom-token>
#      实测 `apiKeySource` 正常、stream-json 正常出结果、netdev 工具可调。
#   ⑤ --tools 是【全局白名单】，会把 MCP 工具一起掐掉（netdev 直接不可见）。
#      要保留 netdev MCP，必须改用 --disallowedTools —— 它是硬移除，
#      被拉黑的工具从模型的工具目录里彻底消失（实测连 ToolSearch 都搜不到）。
#   ⑤b ★ --disallowedTools 【不按逗号拆分】：传 "Bash,Write,Edit" 这种逗号串
#      会被当成【一个】工具名，整条限制静默失效（实测模型照样看得到 Bash）。
#      必须每个工具名各占一个 argv 元素（见 WB_DENY_LIST 与 _wb_argv 的 *展开）。
#      这个坑极隐蔽：不实测根本发现不了，因为它不报错、只是不生效。
#   ⑤c 不传 --mcp-config 时，CLI 会把机器上【其它】MCP 配置也读进来
#      （实测会冒出 mcp__sheetagent__* / mcp__weixinpay__* 等）。
#      所以无论哪个档位都要显式传 --mcp-config + --strict-mcp-config；
#      read 档位传一个空 mcpServers，把 MCP 面彻底关干净。
#   ⑥ MCP 工具默认 defer_loading=true，要经 DeferExecuteTool 调用，而后者
#      在非交互模式下需审批 ⇒ 必被拒。解法：mcp 配置里 defer_loading=false
#      （server 级 + tool 级都要写，见 _wb_mcp_file）。
#
#    安全边界（对齐 pi 的 read+netdev 白名单）：
#    · --disallowedTools 硬移除一切非只读内建工具（含 Bash/Write/Edit/Agent/
#      WebFetch/Skill/ToolSearch…），只留 Read/Glob/Grep ⇒ AI 没有 shell，
#      绕不过 netdev；
#    · 设备能力只经 --mcp-config + --strict-mcp-config 注入 netdev 一个 MCP，
#      它最终仍转调 netdev CLI —— 黑名单 / 人审 / 备份等护栏原样生效。
#
#    凭据来源（三级优先，见 _wb_token）：
#      NETDEV_WB_TOKEN 环境变量  >  config/workbuddy.token 文件  >  自动匹配
#      （读 ~/.wb-switch/accounts.json，取 uid 与桌面 App 当前登录账号一致那条）
# ══════════════════════════════════════════════════════════════════════════
WB_APP_CLI = ("/Applications/WorkBuddy.app/Contents/Resources"
              "/app.asar.unpacked/cli/bin/codebuddy")
WB_PRODUCT_BASE = ("/Applications/WorkBuddy.app/Contents/Resources"
                   "/app.asar.unpacked/cli/product.json")
WB_AUTH_INFO = ("Library/Application Support/CodeBuddyExtension"
                "/Data/Public/auth/workbuddy-desktop.info")
WB_ACCOUNTS = ".wb-switch/accounts.json"
WB_ENDPOINT = "https://www.workbuddy.cn"

WB_READONLY_TOOLS = ("Read", "Glob", "Grep")   # 只读内建工具（对标 pi 的 read）
# 内建工具全集（取自 CLI init 事件的 tools 字段，2026-09-29 实测）
WB_BUILTIN_CATALOG = (
    "Agent", "Write", "Edit", "Bash", "PowerShell", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode",
    "TaskCreate", "TaskGet", "TaskUpdate", "TaskList", "TaskStop", "TaskOutput",
    "WebFetch", "WebSearch", "Skill", "AskUserQuestion", "StructuredOutput",
    "ToolSearch", "DeferExecuteTool", "SendMessage", "TeamCreate", "TeamDelete",
    "ImageGen", "VideoGen", "WeChatReply", "WeComReply",
    "ListMcpResources", "ReadMcpResource", "MessageColleague", "SpeakInChannel",
)
# ★ 坑⑤⑤b：只读工具之外全部硬移除，且【必须是 list】—— 逗号串会被当单个名字
WB_DENY_LIST = [t for t in WB_BUILTIN_CATALOG if t not in WB_READONLY_TOOLS]
WB_TOOLSETS = {                                # 键与 TOOLSETS 对齐
    "read": "none",                            # 只给只读内建，不挂 MCP
    "read+netdev": "netdev",                   # 只读内建 + netdev MCP（默认）
    "full": "none",                            # 不再提供"放开内建工具"的档位
}
# netdev MCP 的 13 个工具名（用于逐个关掉延迟加载，见坑⑥）
WB_ND_TOOLS = (
    "netdev_apply", "netdev_backup", "netdev_connect_info", "netdev_diff",
    "netdev_list", "netdev_ping", "netdev_run", "netdev_save",
    "netdev_screen_list", "netdev_screen_read", "netdev_screen_send",
    "netdev_serial_run", "netdev_watch_tail",
)


def _wb_bin() -> str:
    """找 WorkBuddy 的 codebuddy CLI：环境变量 > 桌面 App 内置 > PATH。"""
    cands = [os.environ.get("NETDEV_WB_BIN") or "", WB_APP_CLI]
    cands += [shutil.which(n) or "" for n in ("cbc", "codebuddy")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _wb_free_port() -> int:
    """拿一个当前空闲的本地端口（给 CLI 内部服务绑，见坑①）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 凭据：三级优先取裸 JWT（见坑③④）────────────────────────────────────
def _wb_desktop_uid() -> str:
    """桌面 App 当前登录账号的 uid。

    注意：`account.uid` 不在 CLI 的 AUTH_CREDENTIAL_FIELDS 里 ⇒ 是明文，
    可以直接读，不需要解任何 $wbEncrypted 信封。
    """
    try:
        d = json.loads((HOME / WB_AUTH_INFO).read_text(encoding="utf-8"))
        return str(((d.get("account") or {}).get("uid")) or "")
    except Exception:
        return ""


def _wb_token() -> tuple[str, str]:
    """取裸 JWT，返回 (token, 来源说明)。"""
    t = (os.environ.get("NETDEV_WB_TOKEN") or "").strip()
    if t:
        return t, "环境变量 NETDEV_WB_TOKEN"
    f = ROOT / "config" / "workbuddy.token"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if t:
            return t, f"文件 {f}"
    except Exception:
        pass
    try:
        acts = json.loads((HOME / WB_ACCOUNTS).read_text(encoding="utf-8"))
        if isinstance(acts, dict):              # 兼容 {"accounts":[...]} 形态
            acts = acts.get("accounts") or []
        if not isinstance(acts, list):
            return "", ""
        uid = _wb_desktop_uid()
        pick = next((a for a in acts
                     if a.get("access_token") and uid and str(a.get("uid") or "") == uid), None)
        src = f"与桌面 App 同账号 {uid[:8]}…"
        if not pick:
            pick = next((a for a in acts if a.get("access_token")), None)
            src = "账号库首条（未匹配上桌面账号）"
        if pick:
            return str(pick["access_token"]).strip(), f"{WB_ACCOUNTS} · {src}"
    except Exception:
        pass
    return "", ""


def _wb_jwt_exp(tok: str) -> int:
    """从 JWT 里读 exp（只解码不验签，用于提示是否过期）。"""
    try:
        import base64
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0


def _wb_token_state() -> tuple[bool, str]:
    """凭据可用性 + 人话说明（给界面看）。"""
    tok, src = _wb_token()
    if not tok:
        return False, ("拿不到 WorkBuddy 凭据：请先让桌面版登录，"
                       f"或把裸 token 写到 {ROOT / 'config' / 'workbuddy.token'}")
    exp = _wb_jwt_exp(tok)
    if exp:
        import datetime
        d = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
        if exp < time.time():
            return False, f"凭据已于 {d} 过期（来源：{src}）—— 重新登录桌面版即可刷新"
        return True, f"凭据有效至 {d}（来源：{src}）"
    return True, f"已注入凭据（来源：{src}）"


def _wb_product_cfg(tok: str) -> pathlib.Path:
    """生成 custom-token 形态的产品配置（★ 坑④）。

    基底用 App 内稳定路径的 product.json（不依赖 /var/folders 里会消失的 spill）。
    authentication.id 特意改成 netdev-wb ⇒ 对应 auth/netdev-wb.info，
    绝不碰桌面端自己的 workbuddy-desktop.info。
    """
    p = ROOT / "config" / "workbuddy-product.json"
    try:
        if p.is_file() and (time.time() - p.stat().st_mtime) < 60:
            return p                          # 一分钟内已生成过，避免每轮重写
        cfg = json.loads(pathlib.Path(WB_PRODUCT_BASE).read_text(encoding="utf-8"))
        cfg["authentication"] = {
            "id": "netdev-wb",
            "type": "custom-token",
            "label": "WorkBuddy custom token",
            "attributes": {"token": tok},
        }
        cfg["endpoint"] = WB_ENDPOINT
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)                # 里面含 token，收紧权限
        except Exception:
            pass
    except Exception:
        pass
    return p


def _wb_mcp_file(use_netdev: bool = True) -> pathlib.Path:
    """--mcp-config 用的文件（★ 坑⑥ + 坑⑤c）。

    use_netdev=True  → 只注入 netdev，并关掉延迟加载；
    use_netdev=False → 写成空 mcpServers，配合 --strict-mcp-config
                       把环境里其它 MCP 一并挡掉。
    """
    # 两个档位必须用不同文件：argv 是「按轮现拼」的，共用一个路径会被并发会话互相覆盖
    p = ROOT / "config" / ("codebuddy-mcp.json" if use_netdev else "codebuddy-mcp-empty.json")
    servers: dict = {}
    if use_netdev:
        servers["netdev"] = {
            "command": str(ROOT / "netdev-mcp"),
            "defer_loading": False,
            # ★ 显式带上网页审批地址：codebuddy 拉 MCP 子进程时可能不继承
            #   全部环境（与 pi 同理），审批会静默回退到 macOS 原生弹窗。
            "env": {"NETDEV_APPROVAL_URL": UI_BASE or "http://127.0.0.1:8898"},
            "tools": {t: {"defer_loading": False} for t in WB_ND_TOOLS},
        }
    try:
        p.write_text(json.dumps({"mcpServers": servers},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    return p


# 从 UI 进程继承下来会把 CLI 带偏的变量前缀
# （实测最要命的是 SERVER__PORT=54805，直接静默挂死）
_WB_SCRUB = ("CODEBUDDY", "WORKBUDDY", "ACC_PRODUCT", "SERVER__")


def _wb_env() -> dict:
    """给 CLI 一个【干净】环境。

    UI 服务器本身可能是从某个 WorkBuddy 会话里拉起来的，env 里带着一整套
    宿主变量（SERVER__PORT / CODEBUDDY_MCP_CONFIG / ACC_PRODUCT_CONFIG_PATH
    / CODEBUDDY_CREDENTIALS_IN_MEMORY …），不清掉一定会互相打架。
    """
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(_WB_SCRUB)}
    env["PATH"] = ":".join([str(HOME / ".npm-global/bin"), "/usr/local/bin",
                            "/opt/homebrew/bin", env.get("PATH", "/usr/bin:/bin")])
    env["SERVER__PORT"] = str(_wb_free_port())       # ★ 坑①：不设必挂
    env["SERVER__HOST"] = "127.0.0.1"
    env.setdefault("CODEBUDDY_DISABLE_IDE", "1")
    env.setdefault("CODEBUDDY_DISABLE_CRON", "1")
    # 关掉 CLI 的自动记忆：它默认会想往 ~/.codebuddy/projects/*/memory 落盘，
    # 而 Write 已被拉黑 ⇒ 白烧轮次（实测浪费一整轮）。
    env.setdefault("CODEBUDDY_DISABLE_AUTO_MEMORY", "1")
    tok, _src = _wb_token()
    if tok:                                          # ★ 坑④：custom-token 通道
        env["CODEBUDDY_AUTH_TOKEN"] = tok
        env["ACC_PRODUCT_CONFIG_PATH"] = str(_wb_product_cfg(tok))
    if UI_BASE:      # netdev 的网页审批通道跟着 MCP 一起继承下去
        env["NETDEV_APPROVAL_URL"] = UI_BASE
    return env


def _wb_argv(tools_key: str, model: str, session_id: str, fresh: bool) -> list:
    """拼一轮 headless 调用的 argv。fresh=True 新会话，否则 resume。

    两个反直觉点，都踩过：
    · 这里【不能】用 --tools 去限制内建工具，否则 MCP 工具会被一起掐掉（★ 坑⑤）；
      改用 --disallowedTools，且每个工具名必须是独立 argv 元素（★ 坑⑤b）。
    · 每个档位都要显式传 --mcp-config + --strict-mcp-config，
      否则机器上其它 MCP 会被读进来（★ 坑⑤c）。
    """
    use_nd = WB_TOOLSETS.get(tools_key, "netdev") == "netdev"
    argv = [_wb_bin(), "-p", "--output-format", "stream-json"]
    argv += ["--session-id" if fresh else "--resume", session_id]
    argv += ["--disallowedTools", *WB_DENY_LIST]          # ★ 展开，不能逗号串
    argv += ["--mcp-config", str(_wb_mcp_file(use_nd)), "--strict-mcp-config"]
    argv += ["--append-system-prompt", DEBUG_SYSTEM_PROMPT]
    if model:
        argv += ["--model", model]
    return argv


def _wb_authed() -> bool:
    """是否拿到可用凭据（不消耗额度）。"""
    return _wb_token_state()[0]


def _wb_deep_probe(cmd: str) -> tuple[bool, str]:
    """深探测：真跑一轮最小 prompt（会消耗极少量额度）。"""
    try:
        r = subprocess.run(
            [cmd, "-p", "--max-turns", "1", "--output-format", "stream-json",
             "--disallowedTools", *WB_DENY_LIST,
             "--mcp-config", str(_wb_mcp_file(False)), "--strict-mcp-config",
             "连接测试：只回复两个字：在线"],
            capture_output=True, text=True, timeout=120,
            env=_wb_env(), cwd=str(ROOT), input="")
        txt = (r.stdout or "") + (r.stderr or "")
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    return False, str(ev.get("result") or ev.get("subtype") or "未知错误")[:160]
                return True, "headless 直连正常（custom-token 已生效）"
        blob = txt.strip()
        if "Authentication" in blob:
            return False, "凭据被拒：token 可能已失效，重新登录桌面版后重试"
        if "EADDRINUSE" in blob:
            return False, "端口被占（SERVER__PORT 没生效？）"
        tail = [x for x in blob.splitlines() if x.strip()]
        return False, (tail[-1] if tail else "无任何输出（可能网络受限）")[:140]
    except subprocess.TimeoutExpired:
        return False, "120 秒无响应（可能网络受限或代理拦截）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class WbSession:
    """WorkBuddy agent 会话：headless 每轮短进程 + session 复用。

    对外接口与 AiSession 一致（prompt/abort/subscribe/close/...）；
    codebuddy 的 stream-json 事件在这里翻译成前端已认识的 pi 形状
    （message_start / message_update / toolcall_start / tool_execution_end /
    agent_end / error）—— 前端零改动即可渲染。
    """

    def __init__(self, aid: str, model: str = "", cwd: str | None = None,
                 tools: str = "read+netdev"):
        self.aid = aid
        self.model = (model or "").strip()
        self.tools_key = tools if tools in TOOLSETS else "read+netdev"
        self.cwd = cwd or str(ROOT)
        self.proc: subprocess.Popen | None = None
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.alive = True
        self.last_active = time.time()
        self.ready = threading.Event()
        self.ready.set()                     # 每轮按需拉起，无需预热
        self.log: list[dict] = []            # 事件回放（新订阅者补发，限量）
        self.stderr_tail = ""
        self.cbc_sid = ""                    # codebuddy 会话 id
        self.cbc_ready = False               # 上一轮是否完整落袋（决定 resume 还是新开）
        self._last_tool = "tool"
        self._turn_text = ""                 # 本轮已输出的正文（用于识别登录类报错）

    # ── 事件出口（扇出 + 回放，与 AiSession 一致）──
    def _emit(self, ev: dict) -> None:
        with self.lock:
            self.log.append(ev)
            if len(self.log) > 600:
                del self.log[:200]
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    def _emit_text(self, text: str) -> None:
        if not (text or "").strip():
            return
        self._turn_text += text
        self._emit({"type": "message_start"})
        self._emit({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text}})
        self._emit({"type": "message_end"})

    # ── 对外接口 ──
    def prompt(self, text: str) -> bool:
        if not self.alive:
            return False
        # 上一轮还挂着 → 视为隐式中止后重开（界面 busy 时发不了，这里是兜底）
        self.abort()
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()
        self.last_active = time.time()
        return True

    def abort(self) -> bool:
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                return True
            except Exception:
                return False
        return False

    def compact(self, instructions: str = "") -> bool:
        return False                     # 该后端暂不支持（前端会原样提示）

    def stats(self) -> bool:
        return False

    def respond_ui(self, req_id: str, value=None, cancelled: bool = False) -> bool:
        return False                     # codebuddy headless 无 dialog 通道

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=3000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            replay = list(self.log[-120:])
        return q, replay

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        self.abort()
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    # ── 一轮对话 ──
    def _turn(self, text: str) -> None:
        self._turn_text = ""
        fresh = (not self.cbc_sid) or (not self.cbc_ready)
        if fresh:
            self.cbc_sid = secrets.token_hex(16)
            self.cbc_ready = False
        argv = _wb_argv(self.tools_key, self.model, self.cbc_sid, fresh)
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                cwd=self.cwd, env=_wb_env())
        except Exception as e:
            self._emit({"type": "error",
                        "error": f"WorkBuddy CLI 启动失败：{type(e).__name__}: {e}"})
            self._emit({"type": "agent_end"})
            self.proc = None
            return
        try:
            self.proc.stdin.write(text)
            self.proc.stdin.close()
        except Exception:
            pass

        saw_activity = False
        raw_tail: list[str] = []
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    raw_tail.append(line[:200])
                    raw_tail[:] = raw_tail[-4:]
                    continue
                if self._translate(ev):
                    saw_activity = True
        except Exception as e:
            self._emit({"type": "error",
                        "error": f"WorkBuddy 事件流中断：{type(e).__name__}: {e}"})
        finally:
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.wait(timeout=10)
            except Exception:
                pass
            try:
                if self.proc:
                    self.stderr_tail = (self.stderr_tail
                                        + (self.proc.stderr.read() or ""))[-800:]
            except Exception:
                pass
            if not saw_activity:
                blob = "".join(raw_tail) + (self.stderr_tail or "")
                if "Authentication" in blob:
                    self._emit({"type": "error", "error":
                                "WorkBuddy 凭据被拒 —— 走的是 custom-token 通道，"
                                "token 可能已过期。重新登录桌面版即可刷新；"
                                f"也可手写 {ROOT / 'config' / 'workbuddy.token'} 覆盖"})
                elif blob.strip():
                    self._emit({"type": "error", "error":
                                "WorkBuddy 无有效输出：" + blob.strip()[:220]})
                else:
                    self._emit({"type": "error", "error":
                                "WorkBuddy 无任何输出（可点设置里的「检测可接入的后端」排查）"})
            self._emit({"type": "agent_end"})
            self.last_active = time.time()
            self.proc = None

    def _translate(self, ev: dict) -> bool:
        """codebuddy stream-json → 前端的 pi 形状事件。返回是否为有效活动。"""
        t = ev.get("type", "")
        if t == "assistant":
            blocks = (ev.get("message") or {}).get("content") or []
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type", "")
                if bt == "text" and b.get("text"):
                    self._emit_text(b["text"])
                elif bt == "thinking" and b.get("thinking"):
                    self._emit({"type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "thinking_delta",
                                    "delta": b["thinking"]}})
                elif bt == "tool_use":
                    self._last_tool = b.get("name") or "tool"
                    self._emit({"type": "toolcall_start",
                                "toolName": self._last_tool,
                                "args": b.get("input") or {}})
            return True
        if t == "user":
            blocks = (ev.get("message") or {}).get("content") or []
            if isinstance(blocks, dict):
                blocks = [blocks]
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self._emit({"type": "tool_execution_end",
                                "toolName": self._last_tool,
                                "isError": bool(b.get("is_error"))})
            return True
        if t == "result":
            sid = ev.get("session_id")
            if sid:
                self.cbc_sid = str(sid)
                self.cbc_ready = True
            if ev.get("subtype") not in ("success", "success_during_malformed"):
                msg = str(ev.get("error") or ev.get("result") or "")
                blob = msg + self._turn_text + (self.stderr_tail or "")
                if "Authentication" in blob:
                    self._emit({"type": "error", "error":
                                "WorkBuddy 凭据被拒 —— custom-token 已失效，"
                                "重新登录桌面版刷新；或写 "
                                f"{ROOT / 'config' / 'workbuddy.token'} 覆盖"})
                else:
                    self._emit({"type": "error", "error":
                                (msg or "WorkBuddy 本轮未正常完成")[:400]})
            return True
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Direct 后端（2026-09-30 加）—— 自实现 agent loop，直连 OpenAI 兼容 API
#
#  为什么要有它：pi / WorkBuddy 都依赖本机装某个 CLI（pi 要 npm 装、
#  wb 要桌面版内置 codebuddy），新机器上还要折腾认证。direct 只需要一把
#  API Key（环境变量或一个配置文件），一个标准库 HTTP 客户端直连
#  DeepSeek / OpenAI / 任意 OpenAI 兼容网关 —— 零新依赖、零常驻进程。
#
#  护栏唯一性（最重要的一条设计约束）：
#     direct **不重新实现任何设备操作**。它 import netdev_mcp，复用
#     netdev_mcp.HANDLERS（13 个工具函数）+ netdev_mcp.envelope（身份信封）。
#     那些 t_* 函数最终全部转调 netdev CLI —— 黑名单 / 人审 / 先备份 /
#     逐条校验 / 同屏可见，一行不重写，与 pi、wb 走的是同一条护栏路径。
#
#  事件契约：与 AiSession / WbSession 完全一致
#     message_start / message_update / toolcall_start / tool_execution_end /
#     agent_end / error —— 前端 onAiEvent 零改动即可渲染。
# ══════════════════════════════════════════════════════════════════════════

# ── 凭据与端点：provider 表 + 三级优先取 key ─────────────────────────────
#   三级优先：环境变量 > config/direct.json（600 权限）> 探测本机已装后端
_DIRECT_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
        # ★ 2026-09-30 实测：账户实时型号只有 deepseek-flash / deepseek-v4-pro。
        #   deepseek-chat / deepseek-reasoner 等"文档名"返回 200 但被【静默映射】到
        #   deepseek-flash —— 配置名与实际执行不一致，排查时会被误导，故默认用真名。
        "default_model": "deepseek-flash",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4",
    },
}
_DIRECT_CFG_FILE = ROOT / "config" / "direct.json"


def _direct_cfg() -> dict:
    """解析 direct 凭据 + 端点。返回 {ok, provider, base_url, api_key, model, note}。

    三级优先：
      1. 环境变量（NETDEV_DIRECT_PROVIDER / NETDEV_DIRECT_BASE_URL /
         NETDEV_DIRECT_API_KEY / NETDEV_DIRECT_MODEL，或各 provider 自己的 *_API_KEY）
      2. config/direct.json（形如 {"provider":"deepseek","api_key":"...","base_url":"...","model":"..."}）
      3. 探测：本机已装的 pi / wb 的凭据（deepseek 兜底）
    """
    provider = (os.environ.get("NETDEV_DIRECT_PROVIDER") or "").strip().lower()
    base_url = (os.environ.get("NETDEV_DIRECT_BASE_URL") or "").strip()
    api_key = (os.environ.get("NETDEV_DIRECT_API_KEY") or "").strip()
    model = (os.environ.get("NETDEV_DIRECT_MODEL") or "").strip()

    # 若未显式指定 provider，则按 provider 表逐个查环境变量
    if not provider and not (api_key and base_url):
        for pid, p in _DIRECT_PROVIDERS.items():
            k = (os.environ.get(p["env"]) or "").strip()
            if k:
                provider, api_key, base_url = pid, k, p["base_url"]
                model = model or p["default_model"]
                break

    # 第二级：配置文件
    if not api_key:
        try:
            cfg = json.loads(_DIRECT_CFG_FILE.read_text(encoding="utf-8"))
            provider = str(cfg.get("provider") or provider or "").strip().lower()
            api_key = str(cfg.get("api_key") or "").strip()
            base_url = str(cfg.get("base_url") or base_url or "").strip()
            model = str(cfg.get("model") or model or "").strip()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # 若 provider 已知但缺 key / base_url，用表补齐
    if provider in _DIRECT_PROVIDERS:
        p = _DIRECT_PROVIDERS[provider]
        api_key = api_key or (os.environ.get(p["env"]) or "").strip()
        base_url = base_url or p["base_url"]
        model = model or p["default_model"]

    if not api_key or not base_url:
        return {"ok": False, "provider": provider, "note":
                "未配置直连凭据：请设 NETDEV_DIRECT_API_KEY + NETDEV_DIRECT_BASE_URL，"
                f"或写 {_DIRECT_CFG_FILE}（{{provider, api_key, base_url, model}}）"}
    return {"ok": True, "provider": provider, "base_url": base_url.rstrip("/"),
            "api_key": api_key, "model": model,
            "note": f"provider={provider or '(自定义)'} · base={base_url}"}


def _direct_cfg_state() -> tuple[bool, str]:
    """给界面看的凭据可用性 + 人话说明（不消耗额度）。"""
    cfg = _direct_cfg()
    if cfg["ok"]:
        return True, f"直连就绪：{cfg['note']}"
    return False, cfg["note"]


def _direct_cfg_public() -> dict:
    """界面用的 direct 配置快照。key 脱敏：只给尾 4 位，绝不回完整 Key。"""
    cfg = _direct_cfg()
    return {"ok": cfg["ok"], "provider": cfg.get("provider", ""),
            "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "note": cfg.get("note", ""),
            "key_tail": (cfg.get("api_key") or "")[-4:],
            "key_src_env": bool((os.environ.get("NETDEV_DIRECT_API_KEY") or "").strip())}


def _direct_models() -> list[str]:
    """拉账户实时可用模型列表（Key 只在服务端用，不回传前端）。

    背景：DeepSeek 的"文档模型名"与"账户实际型号"不一致（deepseek-chat 会被
    静默映射到 flash）—— 让界面从 /models 拉真名，客户不用猜。
    """
    cfg = _direct_cfg()
    if not cfg["ok"]:
        return []
    try:
        import urllib.request
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return sorted(str(m.get("id")) for m in d.get("data", []) if m.get("id"))
    except Exception:
        return []


def _direct_probe() -> tuple[bool, str]:
    """深探测：真发一条最小 prompt（非流式，消耗极少额度），量延迟。

    对标 _wb_deep_probe —— 界面上点「测试连通」就是它。
    """
    cfg = _direct_cfg()
    if not cfg["ok"]:
        return False, cfg["note"]
    t0 = time.time()
    try:
        s = DirectSession("__probe__", model=cfg.get("model", ""), tools="read")
        resp = s._chat([{"role": "user", "content": "连接测试：只回复两个字：在线"}],
                       stream=False)
        raw = resp.read().decode("utf-8", "replace")
        ms = int((time.time() - t0) * 1000)
        if resp.status != 200:
            return False, f"HTTP {resp.status}: {raw[:160]}"
        body = json.loads(raw)
        txt = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        return True, f"连通正常（{ms} ms）· 模型回应：{txt.strip()[:40] or '(空)'}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _direct_tool_schema() -> list[dict]:
    """把 netdev_mcp.TOOLS 转成 OpenAI 兼容的 tools 参数（去掉 MCP 专属字段）。"""
    try:
        import netdev_mcp
        out = []
        for t in netdev_mcp.TOOLS:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object"}),
                },
            })
        return out
    except Exception:
        return []


class DirectSession:
    """Direct 会话：裸 agent loop，直连 OpenAI 兼容端点。

    对外接口与 AiSession / WbSession 一致（prompt/abort/subscribe/close/...）。
    多轮上下文保存在 self.messages 里（本进程内存，不落盘）。
    """

    MAX_ITER = 8          # 单轮最多 8 次工具往返，防止死循环

    def __init__(self, aid: str, model: str = "", cwd: str | None = None,
                 tools: str = "read+netdev"):
        self.aid = aid
        cfg = _direct_cfg()
        self.cfg = cfg
        self.model = (model or cfg.get("model") or "").strip()
        self.tools_key = tools if tools in TOOLSETS else "read+netdev"
        # read 档位：不挂任何工具（只对话）；read+netdev / full 才给 netdev 工具
        self.use_netdev = self.tools_key in ("read+netdev", "full")
        self.cwd = cwd or str(ROOT)
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.alive = True
        self.last_active = time.time()
        self.ready = threading.Event()
        self.ready.set()                     # 无需预热，按需直连
        self.log: list[dict] = []            # 事件回放
        self.messages: list[dict] = []       # 多轮上下文（内存态）
        self._abort = threading.Event()      # 中止信号
        self._in_msg = False                 # 当前是否开着 message_start（一段回复一个气泡）
        self._tool_schemas = _direct_tool_schema() if self.use_netdev else []

    # ── 事件出口（与 AiSession / WbSession 一致）──
    def _emit(self, ev: dict) -> None:
        with self.lock:
            self.log.append(ev)
            if len(self.log) > 600:
                del self.log[:200]
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    def _close_msg(self) -> None:
        """收口当前文本段（一段回复 = 一个 message_start ... message_end，与 pi 一致）。"""
        if getattr(self, "_in_msg", False):
            self._emit({"type": "message_end"})
            self._in_msg = False

    # ── 工具结果「结构化摘要 + 落盘」（2026-09-30 加）──────────────────
    #   为什么：netdev_run / backup / screen_read 等工具返回几千行回显，
    #   原样塞回上下文会①冲垮上下文②烧 token③噪音淹没关键结论（实测 netdev_run
    #   动辄 8000 字符）。这里把「大文本字段」摘出来落盘，只回填摘要，
    #   让模型拿到「元信息 + 关键行 + 全文路径」，需要细节再按路径取。
    #   注意：只作用 direct 后端，不动 netdev_mcp（pi/wb 走 MCP 契约不变）。
    _BIG_TEXT_FIELDS = ("output", "screen", "diff", "lines", "raw", "results")
    _KEYWORD = re.compile(r"(?i)(error|fail|down|unrecognized|invalid|denied|refused|"
                          r"timeout|warning|mismatch|not found|no such|exceed)")

    def _summarize_tool_result(self, name: str, payload: dict) -> dict:
        """把 payload 里的大文本字段替换为「摘要 + 落盘路径」，其余元信息保留。"""
        try:
            big = {k: payload[k] for k in self._BIG_TEXT_FIELDS if k in payload
                   and isinstance(payload[k], (str, list)) and len(str(payload[k])) > 400}
        except Exception:
            big = {}
        if not big:
            return payload
        # 落盘原文
        logdir = ROOT / "logs" / "ai_tool" / (self.aid or "anon")
        try:
            logdir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return payload
        saved = {}
        for k, v in big.items():
            text = "\n".join(v) if isinstance(v, list) else str(v)
            fn = f"{time.strftime('%H%M%S')}_{name}_{k}.txt"
            p = logdir / fn
            try:
                p.write_text(text + "\n", encoding="utf-8", errors="replace")
                saved[k] = str(p)
            except Exception:
                saved[k] = None
        # 摘要：关键行 + 首尾
        for k, v in big.items():
            lines = v if isinstance(v, list) else str(v).splitlines()
            lines = [str(x) for x in lines]
            hits = [x for x in lines if self._KEYWORD.search(x)][:20]
            head = lines[:6]
            tail = lines[-6:] if len(lines) > 6 else []
            summary = {"全文已落盘": saved.get(k),
                       "总行数": len(lines)}
            if hits:
                summary["关键行"] = hits
            if head:
                summary["开头"] = head
            if tail:
                summary["结尾"] = tail
            payload[k] = summary
        payload["_truncated"] = True          # 显式标记：这是摘要，不是全文
        return payload

    # ── 工具执行：直接复用 netdev_mcp（不重写任何设备逻辑）──
    def _call_tool(self, name: str, args: dict) -> str:
        self._emit({"type": "toolcall_start", "toolName": name, "args": args or {}})
        try:
            import netdev_mcp
            fn = netdev_mcp.HANDLERS.get(name)
            if not fn:
                self._emit({"type": "tool_execution_end", "toolName": name,
                            "isError": True})
                return f"未知工具: {name}"
            payload = fn(args or {})
            payload = netdev_mcp.envelope(name, args or {}, payload)
            payload = self._summarize_tool_result(name, payload)
            self._emit({"type": "tool_execution_end", "toolName": name,
                        "isError": bool(payload.get("ok") is False)})
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            self._emit({"type": "tool_execution_end", "toolName": name,
                        "isError": True})
            return f"工具执行异常: {type(e).__name__}: {e}"

    # ── 一次 HTTP 流式请求（标准库，零新依赖）──
    def _chat(self, msgs: list[dict], stream: bool = True):
        """POST /chat/completions，yield 每个流式 SSE 事件（dict）。"""
        import http.client
        import ssl as _ssl
        import urllib.request
        u = urllib.parse.urlparse(self.cfg["base_url"] + "/chat/completions")
        body = {
            "model": self.model or "deepseek-chat",
            "messages": msgs,
            "stream": stream,
        }
        if self._tool_schemas:
            body["tools"] = self._tool_schemas
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                           timeout=180) if u.scheme == "https" \
            else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=180)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg['api_key']}",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        conn.request("POST", u.path + ("?" + u.query if u.query else ""),
                     body=payload, headers=headers)
        return conn.getresponse()

    def _read_sse(self, resp) -> None:
        """逐行读 SSE 流，交给 _handle_sse_event。"""
        buf = b""
        for chunk in iter(lambda: resp.read(1024), b""):
            if self._abort.is_set():
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(b"data: "):
                    data = line[6:]
                elif line.startswith(b"data:"):
                    data = line[5:]
                else:
                    continue
                if data == b"[DONE]":
                    return
                try:
                    ev = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                self._handle_sse_event(ev)

    def _handle_sse_event(self, ev: dict) -> None:
        """累积 tool_calls 分片 + 输出文本增量。"""
        choices = ev.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            if not self._in_msg:              # ★ 一段回复只开一次气泡（与 pi 事件形状一致）
                self._emit({"type": "message_start"})
                self._in_msg = True
            self._turn_text.append(delta["content"])
            self._emit({"type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta",
                                                  "delta": delta["content"]}})
        # ★ tool_calls 的 function.arguments 是分片 delta，必须累积拼接
        tc = delta.get("tool_calls") or []
        for part in tc:
            idx = part.get("index", 0)
            while len(self._tool_acc) <= idx:
                self._tool_acc.append({"id": "", "name": "", "args": ""})
            if part.get("id"):
                self._tool_acc[idx]["id"] = part["id"]
            fn = part.get("function") or {}
            if fn.get("name"):
                self._tool_acc[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                self._tool_acc[idx]["args"] += fn["arguments"]

    # ── 一轮对话：loop 直到纯文本或 max_iter ──
    def _turn(self, text: str) -> None:
        self._abort.clear()
        self._turn_text = []
        self._in_msg = False               # 新轮次：气泡状态复位
        self.messages.append({"role": "user", "content": text})
        if not self.cfg.get("ok"):
            self._emit({"type": "error", "error": self.cfg.get("note", "直连未配置")})
            self._emit({"type": "agent_end"})
            return

        # 系统人设：设备调试助手（与 pi / wb 同一套）
        msgs = [{"role": "system", "content": DEBUG_SYSTEM_PROMPT}] + self.messages

        try:
            for _ in range(self.MAX_ITER):
                if self._abort.is_set():
                    break
                self._tool_acc = []
                resp = self._chat(msgs, stream=True)
                if resp.status != 200:
                    err = resp.read().decode("utf-8", "replace")[:400]
                    self._emit({"type": "error",
                                "error": f"直连 API 返回 {resp.status}: {err}"})
                    break
                self._read_sse(resp)
                if self._abort.is_set():
                    break
                # 有工具调用 → 本地执行并回填，继续下一轮
                if self._tool_acc:
                    self._close_msg()      # 工具前的正文段收口（下一段文字是新气泡）
                    assistant_msg = {"role": "assistant", "content": "".join(self._turn_text) or None,
                                     "tool_calls": []}
                    tool_results = []
                    for t in self._tool_acc:
                        if not t["name"]:
                            continue
                        try:
                            args = json.loads(t["args"]) if t["args"] else {}
                        except Exception:
                            args = {}
                        assistant_msg["tool_calls"].append({
                            "id": t["id"], "type": "function",
                            "function": {"name": t["name"], "arguments": t["args"]},
                        })
                        result = self._call_tool(t["name"], args)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": t["id"],
                            "content": result,
                        })
                    msgs.append(assistant_msg)
                    msgs += tool_results
                    self.messages.append(assistant_msg)
                    self.messages += tool_results
                    self._turn_text = []
                    continue
                # 纯文本 → 结束
                break
        except Exception as e:
            self._emit({"type": "error",
                        "error": f"直连请求异常: {type(e).__name__}: {e}"})
        finally:
            self._close_msg()              # 兜底收口（含 abort / 异常路径）
            self._emit({"type": "agent_end"})
            self.last_active = time.time()

    # ── 对外接口 ──
    def prompt(self, text: str) -> bool:
        if not self.alive:
            return False
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()
        self.last_active = time.time()
        return True

    def abort(self) -> bool:
        self._abort.set()
        return True

    def compact(self, instructions: str = "") -> bool:
        # 内存态上下文：把旧消息压缩成一条摘要，保留最近 2 条
        if len(self.messages) > 6:
            keep = self.messages[-2:]
            summary = ("（以下为更早对话的压缩摘要）"
                       + (instructions or "") + " …")
            self.messages = [{"role": "system", "content": summary}] + keep
            return True
        return False

    def stats(self) -> bool:
        return False

    def respond_ui(self, req_id: str, value=None, cancelled: bool = False) -> bool:
        return False                     # direct 无 dialog 通道

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=3000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            replay = list(self.log[-120:])
        return q, replay

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        self._abort.set()
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass


# ── 网页审批通道：netdev 的 approval 会 POST 到这里，等人在浏览器上点 ──
ASK_LOCK = threading.Lock()
ASK_PENDING: dict = {}          # req_id -> {device, lines, kind, ev, allow, shown, at}
ASK_LAST_POLL = [0.0]           # 最近一次“浏览器长轮询”的时间（用于判断界面到底开没开）
UI_BASE = ""                    # 由 main() 填；会注入给 AI 子进程做 NETDEV_APPROVAL_URL


# ══════════════════════════════════════════════════════════════════════════
#  排障监控采集（只读）+ 快照管理
# ══════════════════════════════════════════════════════════════════════════
# 同屏会话的「自动化操作」要互斥：监控采集 / 快照保存 都会连续往屏上发命令，
# 并发会互相干扰（已实测：命令交错 ⇒ 设备回不到提示符 ⇒ 备份/下发失败）。
# 终端里“人”的输入不加锁（人机同屏，人优先）。
SCREEN_LOCKS: dict = {}
SCREEN_LOCK_G = threading.Lock()


def screen_lock(dev: str) -> threading.Lock:
    with SCREEN_LOCK_G:
        if dev not in SCREEN_LOCKS:
            SCREEN_LOCKS[dev] = threading.Lock()
        return SCREEN_LOCKS[dev]


def resolve_device(dev: str) -> dict:
    """把界面传来的“设备名”解析成设备（正式优先，其次连接簿）。

    为什么要它：界面现在同时展示【正式设备】和【临时目标】两类，
    而 netdev list 只含正式设备 —— 下游（开终端/采监控）如果还只查那一个源，
    临时设备就会“看得见、点不动”（实测踩到）。
    返回：{name, kind, window, uri, protocol, ...}；找不到返回 {}。
    """
    if not dev:
        return {}
    for d in (netdev_json(["list", "--json"]) or []):
        if dev in (d.get("name"), d.get("window")):
            return {**d, "kind": "formal", "target": d.get("name")}
    for c in conn_list():
        if dev in (c.get("id"), c.get("name")):
            # ★ 2026-09-26 修：不要用 c["window"] —— netdev conn list 返回的那个字段
            #   是从 URI 推导的（t-lab → "telnet-127-0-0-1"），而 CLI 实际建窗格
            #   用的是【连接簿的 name/id】（t-lab）。用错名字就找不到窗格 →
            #   run 回退直连 → 屏上不可见（实测踩到）。
            win = c.get("name") or c.get("id")
            # ★ 2026-09-27 修：target 必须是【连接簿的名字】，不能再是 URI。
            #   原来给的是 c["uri"]（如 telnet://admin@192.168.1.1:23），
            #   UI 用它去 netdev shell → netdev 按"匿名 URI"解析 → 自动生成
            #   名字 telnet-192-168-1-1 → 桥收到的设备名就成了这个，
            #   而凭据是按连接簿名（huawei23）存的 → 取不到 → 停在登录界面。
            #   还会连带：日志写到 telnet-192-168-1-1.screen.log（不是 huawei23.screen.log）、
            #   窗格名与界面显示不一致、找不到窗格时反复重试（表现为"打开很慢"）。
            #   用名字后走的是 CLI 的 resolve_target，它能正确查到连接簿（按 id/name 都认）。
            return {**c, "name": c.get("name") or c.get("id"), "id": c.get("id"),
                    "kind": "temp", "window": win,
                    "target": c.get("name") or c.get("id")}
    return {}


# ── 平台档案接入（2026-09-26）─────────────────────────────────────────
#   监控不再写死华为命令/格式：按 devices.toml 的 platform 分派。
#   未知平台退回华为档案，并标记 assumed（界面会提示）。
sys.path.insert(0, str(ROOT))
try:
    from lib import platforms as _plat
except Exception:                      # 极端情况下不影响主服务启动
    _plat = None
try:
    from lib import learned as _learned
except Exception:
    _learned = None


def _platform_of(dev_name: str) -> str:
    """从设备清单取 platform（取不到就空，让档案层决定退回哪套）。"""
    try:
        d = netdev_json(["list", "--json"], timeout=25) or []
        for x in d:
            if x.get("name") == dev_name:
                return (x.get("platform") or "").strip()
        # 临时目标（连接簿）没有 platform，按名字/协议兜个默认
    except Exception:
        pass
    return ""


MON_CMDS = {                       # 兼容旧引用：默认（华为）命令表
    "cpu":   "display cpu-usage",
    "mem":   "display memory-usage",
    "brief": "display interface brief",
}


def mon_cmds(platform: str | None, dev: str = "") -> dict:
    """按平台取监控命令表，并叠加「这台设备已学到的命令」。

    ★ 2026-10-01 修：原来只取平台默认命令，探测命中后 cache_put 的结果**从来没人用**，
      于是每轮采集都要先把默认命令撞一次墙、再逐条试候选（串口每条 1~2 秒）。
      现在走 platforms.plan_commands：学到的直接生效，第二次采集零探测成本。
    """
    if _plat is None:
        return dict(MON_CMDS)
    try:
        c = _plat.plan_commands(platform, dev, ["cpu", "mem", "brief"])
    except Exception:
        c = _plat.commands_for(platform, ["cpu", "mem", "brief"])
    return c or dict(MON_CMDS)


def _tmux_windows() -> set:
    if not TMUX:
        return set()
    try:
        r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                           capture_output=True, text=True, timeout=8)
        return {w for w in (r.stdout or "").split() if w}
    except Exception:
        return set()


def _tmux_live_windows() -> set:
    """活窗格集合（pane_dead==0）。

    为什么单列：_tmux_windows() 是【全量】窗口名（含死窗格）——
    僵尸清理必须看到死的才能杀；但「设备在线/监控通道决策」若认死窗格
    会出假象：窗格桥已死、串口早没了，还被当成"前台占用"走同屏，
    采回来一堆 Python 堆栈（2026-09-30 huawei 实测踩到）。
    """
    if not TMUX:
        return set()
    try:
        r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION,
                            "-F", "#{window_name}\t#{pane_dead}"],
                           capture_output=True, text=True, timeout=8)
        out = set()
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "0" and parts[0]:
                out.add(parts[0])
        return out
    except Exception:
        return set()


def _parse_metrics(text: str, platform: str | None = None, dev: str = "") -> dict:
    """从回显里抠指标。抠不到就是 None —— 不编造数值。

    2026-09-26：CPU/内存改用平台档案的多条正则（华为→华三→锐捷 各家句式不同）。
    档案里全不匹配时返回 None，界面显示「—」，绝不显示假的 0。
    """
    _prof = _plat.profile_of(platform) if _plat else None
    import re as _re
    def g(pat, cast=float):
        m = re.search(pat, text)
        if not m:
            return None
        try:
            v = cast(m.group(1))
            return int(v) if cast is int else round(v, 1)
        except Exception:
            return None
    m = {}
    # CPU / 内存：优先走平台档案的多条正则
    _pcpu = (_prof or {}).get("parse", {}).get("cpu") or []
    _pmem = (_prof or {}).get("parse", {}).get("mem") or []
    # 学到的规则优先（更贴近这台设备/这个版本）
    if _learned is not None:
        # 两级查找：设备级优先 → 平台级
        _lp, _lsrc = _learned.get_scoped(dev, platform or "", "cpu")
        if _lp:
            _pcpu = [_lp] + list(_pcpu)
    m["cpu_10s"] = _plat.pick(_pcpu, text, float) if _plat else None
    m["cpu_1m"]  = g(r"one minute:\s*([\d.]+)%")
    m["cpu_5m"]  = g(r"five minutes:\s*([\d.]+)%")
    if m["cpu_10s"] is None:                        # 档案没命中 → 兜底旧式
        m["cpu_10s"] = g(r"CPU utilization for (?:ten|five) seconds:\s*([\d.]+)%")
        if m["cpu_10s"] is None:
            m["cpu_10s"] = g(r"CPU\s+Usage\s*:\s*([\d.]+)%")
    m["cpu_5s"] = m["cpu_10s"]                      # 兼容旧字段名
    m["cpu_max"] = g(r"CPU\s+Usage\s*:\s*[\d.]+%\s*Max:\s*([\d.]+)%")
    if _learned is not None:
        _lm, _ = _learned.get_scoped(dev, platform or "", "ram")
        if not _lm:
            _lm, _ = _learned.get_scoped(dev, platform or "", "mem")
        if _lm:
            _pmem = [_lm] + list(_pmem)
    m["mem_pct"] = (_plat.pick(_pmem, text, int) if _plat else None) or g(r"Memory Using Percentage Is:\s*(\d+)%", int)
    m["mem_total"] = g(r"System Total Memory Is:\s*(\d+)\s*bytes", int)
    m["mem_used"] = g(r"Total Memory Used Is:\s*(\d+)\s*bytes", int)
    # CRC：两个来源 —— ① 单接口详情里的 "CRC: n" ② brief 表的 inErrors 列。
    #   原来只看 ①，而 brief 输出里没有 "CRC:" 字样，
    #   换了接口解析之后 crc 就变 None 了（实测踩到）。
    crcs = [int(x) for x in _re.findall(r"CRC:\s*(\d+)", text)]
    _err_sum = None
    try:
        _ifs2 = _parse_if_brief(text)
        if _ifs2:
            _err_sum = sum(int(x.get("in_err") or 0) for x in _ifs2)
    except Exception:
        _err_sum = None
    if crcs:
        m["crc"] = sum(crcs)
    elif _err_sum is not None:
        m["crc"] = _err_sum
    else:
        m["crc"] = None
    m["crc_ifaces"] = len(crcs) or (len(_ifs2) if _ifs2 else None)
    errs = [int(x) for x in _re.findall(r"inErrors\s+(\d+)", text)]
    m["in_err"] = sum(errs) if errs else None
    # 接口：走通用解析 _parse_if_brief。
    #   ★ 原来这里硬编码 `GigabitEthernet\d+/\d+/\d+` 且带宽只认 (\d+)%
    #     —— 换厂商（接口名前缀不同）或换端口（有流量时是 0.01%）就全空。
    #   现在：任意厂商前缀 + 小数带宽 + "--"（虚拟口）都能认。
    ifs = []
    try:
        ifs = _parse_if_brief(text)
    except Exception:
        ifs = []
    if ifs:
        _seen = set(); _u = []
        for it in ifs:                      # 同名只留最后一次（屏上可能有多次 brief）
            if it["name"] in _seen:
                _u = [x for x in _u if x["name"] != it["name"]]
            _seen.add(it["name"]); _u.append(it)
        _phys = [x for x in _u if (_plat.is_physical(x["name"]) if _plat else True)]
        m["if_total"] = len(_phys) or len(_u)
        m["if_up"] = sum(1 for x in (_phys or _u) if str(x["phy"]).startswith("up"))
        _ins = [x["in_uti"] for x in (_phys or _u) if x.get("in_uti") is not None]
        _outs = [x["out_uti"] for x in (_phys or _u) if x.get("out_uti") is not None]
        m["if_in_max"] = max(_ins) if _ins else None
        m["if_out_max"] = max(_outs) if _outs else None
    else:
        m["if_total"] = m["if_up"] = m["if_in_max"] = m["if_out_max"] = None
    return m


def collect_metrics(dev: str) -> dict:
    """采集只读指标：IP 设备（ssh/telnet）一律独立直连【静默采集】；
    串口空闲时同样直连；只有【串口被前台窗格占用】才走同屏（物理独占，绕不开）。

    2026-09-30 改通道优先级（用户要求"静默采集、不影响前台"）：
    · 原逻辑：设备有同屏窗格就走同屏 → 监控往前台屏幕塞命令，打断人工操作；
    · 现在：ssh/telnet 即使窗格开着也走 netdev run 独立短连接
      （开连 → 执行 → 断开，等价"后台开个会话拉完数据就关"），
      与前台窗格互不干扰；
    · 串口物理独占：窗格占用时 netdev run 打不开串口（CLI 会直接拒绝），
      只能走同屏 —— via 字段注明原因，界面会显示。
    多厂商适配不变：命令与解析仍按平台档案（华为/华三/锐捷/思科）分派 + 候选探测。
    """
    wins = _tmux_live_windows()          # 死窗格不算"前台占用"（桥已死 ≠ 占着串口）
    info = resolve_device(dev)
    window = info.get("window") or dev
    proto = (info.get("protocol") or "").strip().lower()
    screen_held = window in wins
    # 通道决策：只有【串口 + 前台窗格占用】才被迫走同屏；其余一律静默直连
    via_screen = bool(proto == "serial" and screen_held)
    via_note = ("同屏（串口被前台占用，物理独占）" if via_screen else "直连（静默）")
    cli = str(ROOT / "netdev")
    platform = _platform_of(dev)
    auto_detected = False
    # ★ 开箱即用：platform 留空（用户没标厂商）时，先发一条 display version
    #   自动识别厂商，再据此选命令/解析。识别不出才退回华为档案（候选探测兜底）。
    #   注意：这里只做「本次采集识别」，不持久化回写设备清单 ——
    #   每次采集多花一次 display version（约 1s），换来用户永远不用懂 platform 代号。
    if not platform and not via_screen:
        try:
            _rc, _vtext, _verr = raw_netdev(["run", dev, "display version"], timeout=30)
            if not _verr and _plat is not None:
                _detected = _plat.detect_platform(strip_ansi(_vtext))
                if _detected:
                    platform = _detected
                    auto_detected = True
        except Exception:
            pass
    base_cmds = mon_cmds(platform, dev)     # {cpu, mem, brief} 按平台取 + 叠加已学到的命令
    raws, cmds, used = {}, [], {}

    def _send_and_collect(work: dict):
        """发一组命令并读出回显（同屏或直连）。返回合并文本。"""
        if via_screen:
            for key, cmd in work.items():
                try:
                    subprocess.run([cli, "screen-send", window, cmd, "--yes"],
                                   capture_output=True, timeout=20)
                except Exception:
                    pass
                cmds.append(cmd); used[key] = cmd
                time.sleep(0.9)
            time.sleep(1.2)
            _rc, text, _e = raw_netdev(["screen-read", window, "--lines", "900"], timeout=30)
            raws["screen"] = strip_ansi(text)
            return raws["screen"]
        acc = []
        for key, cmd in work.items():
            _rc, text, err = raw_netdev(["run", dev, cmd], timeout=30)
            body = strip_ansi(text) if not err else ""
            raws[key] = body if not err else f"<err> {err[:200]}"
            cmds.append(cmd); used[key] = cmd
            acc.append(raws.get(key, ""))
        return "\n".join(acc)

    # ① 先按平台的默认命令采一次
    first_text = _send_and_collect(dict(base_cmds))

    # ② 探测：某条命令不被支持（或该指标没解析出来）→ 拿候选逐条试
    #   ★ 2026-10-01：原来这里先建了个 `retry` 字典把 cache_get 的结果塞进去，
    #     然后**从头到尾没用过**（死代码），缓存因此从来没生效过。已删除；
    #     缓存的生效点前移到 mon_cmds()/plan_commands()（建计划阶段就覆盖默认命令）。
    if _plat is not None and _plat.looks_like_bad_command(first_text):
        # 设备不认当前命令集：逐指标试候选，命中就缓存（下次直接生效）
        print(f"  · {dev}：平台命令未命中，开始探测候选命令…")
        for key in list(base_cmds.keys()):
            for cand in _plat.candidates_for(key, platform):
                if cand == base_cmds.get(key):
                    continue
                if via_screen:
                    try:
                        subprocess.run([cli, "screen-send", window, cand, "--yes"],
                                       capture_output=True, timeout=20)
                    except Exception:
                        pass
                    time.sleep(0.9)
                    _rc, t2, _e = raw_netdev(["screen-read", window, "--lines", "200"], timeout=25)
                    t2 = strip_ansi(t2)
                else:
                    _rc, t2, err2 = raw_netdev(["run", dev, cand], timeout=30)
                    t2 = strip_ansi(t2) if not err2 else f"<err> {err2[:120]}"
                if not _plat.looks_like_bad_command(t2) and t2.strip():
                    _plat.cache_put(dev, key, cand)
                    cmds.append(cand); used[key] = cand
                    raws.setdefault("probe", "")
                    raws["probe"] = (raws["probe"] + "\n" + t2)[-12000:]
                    print(f"  · {key} → 采用 {cand}（已记住，下次直接用）")
                    break

    # ③ 合并所有回显解析（带上平台）
    blob = "\n".join(v for v in raws.values() if v)
    learned = {}
    try:
        learned = _plat.cache_all(dev) if _plat is not None else {}
    except Exception:
        learned = {}
    return {"device": dev, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "via": via_note,
            "platform": platform or "(未标注)",
            "platform_auto": bool(auto_detected),
            "metrics": _parse_metrics(blob, platform, dev),
            "cmds": cmds, "used": used, "learned": learned,
            "raw": {k: v[-4000:] for k, v in raws.items()}}


def list_snapshots(device: str = "") -> list:
    data = netdev_json(["snap", "list", "--json"], timeout=25) or []
    if device:
        data = [d for d in data if d.get("device") == device]
    return data


def strip_ansi(s: str) -> str:
    import re as _re
    return re.sub(r"\x1b\[[0-9;]*m", "", s or "")


def serial_ports() -> list:
    """可用串口端点（解析 netdev serial-discover 的文本输出；它不支持 --json）。"""
    import re as _re
    _rc, out, _e = raw_netdev(["serial-discover"], timeout=40)
    ports = []
    for line in strip_ansi(out).splitlines():
        m = re.match(r"\s*(/dev/\S+)\s+(.*)$", line)
        if m:
            info = re.sub(r"\s+", " ", m.group(2)).strip()
            ports.append({"path": m.group(1), "info": info[:80]})
    return ports


# ── 接口详情（监控面板「接口」卡下钻用）────────────────────────────────
def _parse_if_brief(text: str) -> list:
    """解析 display interface brief → [{name, phy, proto, in_uti, out_uti, in_err, out_err}]"""
    # ★ 2026-09-26 修：原来用 (\d+)% 匹配带宽，把【有流量】的接口全漏了。
    #   实测在线接口的带宽是小数（GigabitEthernet0/0/4 显示 0.01%），
    #   于是一个在线接口都没解析出来（up_count=0）—— 越关键的越漏。
    #   现在：带宽字段按 \S+ 拿，再宽容地转数字；虚拟接口的 "--" 也能收进来。
    def _num(x):
        try:
            return int(float(str(x).rstrip("%")))
        except Exception:
            return None

    out = []
    for ln in (text or "").splitlines():
        toks = ln.split()
        if len(toks) < 7:
            continue
        name, phy, proto = toks[0], toks[1], toks[2]
        if not re.match(r"^(up|down|\*down|up\([a-z]\)|down\([a-z]\))$", phy):
            continue
        if not re.match(r"^(up|down|\*down|up\([a-z]\)|down\([a-z]\))$", proto):
            continue
        if not re.match(r"^([\d.]+%|--)$", toks[3]) or not re.match(r"^([\d.]+%|--)$", toks[4]):
            continue
        out.append({"name": name, "phy": phy, "proto": proto,
                    "in_uti": _num(toks[3]), "out_uti": _num(toks[4]),
                    "in_err": _num(toks[5]) or 0, "out_err": _num(toks[6]) or 0})
    return out


def _parse_if_detail(text: str) -> dict:
    """解析单个接口的详细信息 → {state, crc, in_rate, out_rate, in_util, out_util, ...}"""
    d = {}
    t = text or ""
    m = re.search(r"current state\s*:\s*(\S+)", t)
    if m:
        d["state"] = m.group(1)
    m = re.search(r"Line protocol current state\s*:\s*(\S+)", t)
    if m:
        d["proto"] = m.group(1)
    m = re.search(r"Last 300 seconds input rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec", t)
    if m:
        d["in_bps"], d["in_pps"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Last 300 seconds output rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec", t)
    if m:
        d["out_bps"], d["out_pps"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"CRC:\s*(\d+),\s*Giants:\s*(\d+)", t)
    if m:
        d["crc"], d["giants"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Input bandwidth utilization\s*:\s*(\d+)%", t)
    if m:
        d["in_util"] = int(m.group(1))
    m = re.search(r"Output bandwidth utilization\s*:\s*(\d+)%", t)
    if m:
        d["out_util"] = int(m.group(1))
    return d


def _parse_transceiver(text: str) -> dict:
    """解析光模块信息（光衰）。没有光模块时返回 {}。"""
    d = {}
    t = text or ""
    m = re.search(r"Rx Power\(dBm\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["rx_power_dbm"] = m.group(1)
    m = re.search(r"Tx Power\(dBm\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["tx_power_dbm"] = m.group(1)
    m = re.search(r"Temperature\(C\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["temp_c"] = m.group(1)
    return d


def interface_detail(dev_name: str) -> dict:
    """接口下钻：概览 + 逐接口 CRC/带宽/光衰。只对 up 的接口抓详情（串口慢）。"""
    info = resolve_device(dev_name)
    win = info.get("window") or dev_name
    via = "同屏会话"
    def _read(cmd, lines=400):
        """读屏，且【只取该命令回显之后】的内容。

        为什么不能直接读整屏：窗格是长期复用的，屏上堆着历史回显，
        直接解析会把同一批接口重复算很多遍（实测解析出 26 个，真实只有 7 个）。
        取"最后一次出现的命令回显"之后，就只剩本次输出。
        """
        rc, out, err = raw_netdev(["screen-read", win, "--lines", str(lines)], timeout=30)
        txt = strip_ansi(out)
        key = (cmd or "").split("|")[0].strip()
        ls = txt.splitlines()
        start = 0
        for i, l in enumerate(ls):
            if key and key in l:
                start = i + 1
        # 只取到"收尾提示符"为止：提示符形如 <Huawei> / [Huawei-vlan10]。
        # ⚠ 不能简单判 endswith(">") —— display interface brief 的表头里就有 `>` 之类字符，
        #   会把表格下半截误切掉（实测漏掉最后一个在线接口）。
        body_lines = ls[start:] if key else ls
        out2 = []
        for l in body_lines:
            out2.append(l)
            if re.match(r"^\s*[<\[]\S+[>\]]\s*$", l):
                break
        return "\n".join(out2)

    def _send(cmd):
        cli = str(ROOT / "netdev")
        try:
            subprocess.run([cli, "screen-send", win, cmd, "--yes"],
                           capture_output=True, timeout=20)
        except Exception:
            pass
        time.sleep(1.0)

    brief = ""
    try:
        _send("display interface brief")
        time.sleep(1.2)
        brief = _read("display interface brief")
    except Exception as e:
        return {"error": f"取接口概览失败：{e}"}
    ifs = _parse_if_brief(brief)
    _seen = set(); _uniq = []
    for _it in ifs:                       # 去重（同名只留一个）
        if _it["name"] in _seen:
            continue
        _seen.add(_it["name"]); _uniq.append(_it)
    ifs = _uniq
    if not ifs:
        return {"error": "没解析到接口列表（设备回显异常？）", "raw": brief[-600:]}

    up_count = 0
    for it in ifs:
        # 只对 up 的接口抓详细（串口每次 1s+，全抓太慢）
        if str(it["phy"]).startswith("up"):            # 物理 up 才去抓详情（含 NULL0/虚拟口除外）
            up_count += 1
            _send(f"display interface {it['name']}")
            time.sleep(0.8)
            it.update(_parse_if_detail(_read(f"display interface {it['name']}")))
            # 光衰（电口没有光模块 → 空）
            _send(f"display transceiver verbose interface {it['name']}")
            time.sleep(0.6)
            opt = _parse_transceiver(_read("display transceiver verbose"))
            if opt:
                it["optical"] = opt
    # 全局光模块总览（有些设备不支持按接口查）
    global_opt = {}
    try:
        _send("display transceiver verbose")
        time.sleep(0.8)
        global_opt = _parse_transceiver(_read("display transceiver verbose"))
    except Exception:
        pass

    return {"device": dev_name, "via": via, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "interfaces": ifs, "up_count": up_count,
            "optical_supported": bool(global_opt) or any("optical" in x for x in ifs),
            "optical_global": global_opt}


# ── 单项指标取值（走同屏，不抢串口）──────────────────────────────────
_METRIC_CMDS = {
    "cpu":  "display cpu-usage",
    "ram":  "display memory-usage",
    "crc":  "display interface brief",
    "if":   "display interface brief",
    "ver":  "display version",
    "clock": "display clock",
}


# ── AI 提议解析规则（自我修正的第 2 步）────────────────────────────────
_METRIC_LABEL = {
    "cpu": "CPU 使用率（百分比）",
    "ram": "内存使用率（百分比）",
    "crc": "接口的错误包总数（inErrors 列求和）",
    "if":  "处于 up 状态的物理接口【个数】（需要数行，不是取某个字段）",
}


def ai_suggest_pattern(dev_name: str, key: str, raw: str, cmd: str,
                       timeout: int = 90) -> dict:
    """让 AI 看原始回显，提议一条能提取该指标的正则。**只提议，不保存。**

    安全设计：AI 的输出会经过 lib/learned.validate() 自检 ——
    要求"它给的正则必须能在同一段回显上取出它自己声称的那个值"。
    自检不过直接否决，不让它进档案。
    """
    if not PI_BIN:
        return {"error": "找不到 pi，无法使用 AI 学习功能"}
    label = _METRIC_LABEL.get(key, key)
    prompt = (
        "你是网络设备回显解析助手。下面是某设备执行命令 `" + cmd + "` 的原始回显。\n"
        "请完成两件事：\n"
        f"  1. 从中读出「{label}」的当前值（只回数字，不要单位）；\n"
        "  2. 给出一条 Python 正则（必须含【一个捕获组】），能从这段回显里稳定取出该值。\n\n"
        "要求：正则尽量具体（带上命令特征字），不要用过于宽泛的 \\d+ 到处匹配；\n"
        "      不确定就如实说不知道，不要编造。\n\n"
        "只输出一行 JSON，不要任何解释、不要代码块围栏：\n"
        '{"value": <数字>, "pattern": "<正则>"}\n'
        "若确实无法判断，输出：{\"value\": null, \"pattern\": \"\"}\n\n"
        "===== 原始回显开始 =====\n"
        + (raw or "")[:6000] +
        "\n===== 原始回显结束 ====="
    )
    try:
        proc = subprocess.Popen([PI_BIN, "--mode", "rpc", "--no-session"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                cwd=str(HOME), env=_env_with_node())
    except Exception as e:
        return {"error": f"起 pi 失败：{e}"}

    out_text, kinds = "", []
    try:
        proc.stdin.write(json.dumps({"type": "prompt", "message": prompt}) + "\n")
        proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            kinds.append(ev.get("type"))
            d = (ev.get("assistantMessageEvent") or {})
            if d.get("type") == "text_delta" and d.get("delta"):
                out_text += d["delta"]
            if ev.get("type") == "extension_ui_request":
                # 通知类：不需要回应；dialog 类在采集这种无头场景下直接忽略
                continue
            if ev.get("type") in ("agent_end", "agent_settled"):
                break
    except Exception as e:
        return {"error": f"与 pi 通信失败：{e}"}
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    # 从 AI 的回复里抠出 JSON
    m = re.search(r'\{[^{}]*"value"\s*:\s*([^,}]+)[^{}]*\}', out_text or "", re.S)
    if not m:
        return {"error": "AI 没给出可用的 JSON", "raw_reply": (out_text or "")[:400]}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {"error": "AI 的 JSON 解析失败", "raw_reply": m.group(0)[:300]}
    return {"value": obj.get("value"), "pattern": (obj.get("pattern") or "").strip(),
            "events": len(kinds)}


def metric_one(dev_name: str, key: str) -> dict:
    """取单项指标的原始回显 —— 走同屏会话（不直连、不抢串口、用户看得见）。"""
    info = resolve_device(dev_name)
    win = info.get("window") or dev_name
    cmd = _METRIC_CMDS.get(key)
    if not cmd:
        return {"error": f"未知指标 {key}"}
    cli = str(ROOT / "netdev")
    # 1) 清历史（否则屏上的旧内容会混进来）
    try:
        subprocess.run([TMUX, "clear-history", "-t", f"{TMUX_SESSION}:{win}"],
                       capture_output=True, timeout=8)
    except Exception:
        pass
    # 2) 发命令（走同屏，屏幕可见）
    try:
        subprocess.run([cli, "screen-send", win, cmd, "--yes"],
                       capture_output=True, timeout=30)
    except Exception as e:
        return {"error": f"下发失败：{e}"}
    # 3) 等输出稳定再读 —— 固定 sleep 会读到半截。
    #    实测：display interface brief 有 26 行接口，2 秒时只吐到"表头+第 1 行"，
    #    结果交给 AI 的样例残缺，AI 只能回答"未给出"（它是对的，是我们给少了）。
    #    这里轮询：连续两次读数长度不变且出现收尾提示符，才算吐完。
    import hashlib as _hl
    prev_sig, prev_len, stable = "", -1, 0
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(1.2)
        _rc0, _o0, _e0 = raw_netdev(["screen-read", win, "--lines", "500"], timeout=25)
        _t0 = strip_ansi(_o0)
        # 只看命令之后那一段
        _l0 = _t0.splitlines()
        _st = 0
        for _i, _l in enumerate(_l0):
            if cmd in _l:
                _st = _i + 1
        _seg = "\n".join(_l0[_st:])
        _sig = _hl.md5(_seg.encode("utf-8", "replace")).hexdigest()
        if _sig == prev_sig and len(_seg) == prev_len:
            stable += 1
        else:
            stable = 0
        prev_sig, prev_len = _sig, len(_seg)
        # 稳定两轮 或 已看到收尾提示符 → 认为吐完
        if stable >= 2 or re.search(r"^\s*[<\[]\S+[>\]]\s*$", _seg, re.M):
            break
    # 读最终结果
    rc, out, err = raw_netdev(["screen-read", win, "--lines", "500"], timeout=30)
    txt = strip_ansi(out)
    ls = txt.splitlines()
    start = 0
    for i, l in enumerate(ls):
        if cmd in l:
            start = i + 1
    body = "\n".join(ls[start:])
    # 切到收尾提示符
    bl = body.splitlines(); keep = []
    for l in bl:
        keep.append(l)
        if re.match(r"^\s*[<\[]\S+[>\]]\s*$", l):
            break
    raw = "\n".join(keep).strip() or "(无回显)"
    val = None
    try:
        m = _parse_metrics(raw)
        if key == "cpu":
            val = m.get("cpu_10s")
        elif key == "ram":
            val = m.get("mem_pct")
        elif key in ("crc", "if"):
            # display interface brief 里没有 "CRC:" 字样，_parse_metrics 取不到；
            # 改用接口解析后汇总 inErrors（这才是"错包"口径）。
            _ifs = _parse_if_brief(raw)
            if _ifs:
                val = sum(int(x.get("in_err") or 0) for x in _ifs)
    except Exception:
        pass
    return {"device": dev_name, "key": key, "cmd": cmd,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lines": len(raw.splitlines()),
            "value": val, "output": raw}


# ── 回收区：彻底删除（单项 / 全部）──────────────────────────────────────
#   netdev CLI 的 `snap purge` 只能全清，没有单项；这里补上，并做严格路径校验：
#   只允许删除 S.TRASH_ROOT 目录【直接子项】，绝不允许越界（防止误删别处）。
def snap_purge(ref: str = "", all_of: bool = False) -> dict:
    sys.path.insert(0, str(ROOT))
    from lib import snapshot as S           # noqa: PLC0415
    import shutil as _sh
    trash = S.TRASH_ROOT
    items = S.list_trash()
    if all_of:
        if not items:
            return {"ok": True, "purged": [], "msg": "回收区已经是空的"}
        purged = []
        for d in items:
            try:
                _sh.rmtree(d)
                purged.append(d.name)
            except Exception as e:
                return {"ok": False, "purged": purged, "error": f"{d.name}: {e}"}
        return {"ok": True, "purged": purged, "n": len(purged)}
    ref = (ref or "").strip()
    if not ref:
        return {"ok": False, "error": "缺少 ref"}
    pick = next((d for d in items if ref in d.name), None)
    if not pick:
        return {"ok": False, "error": f"回收区里没找到：{ref}"}
    # ★ 路径校验：必须正好是 trash 目录的直接子项
    try:
        pick_r = pick.resolve()
        trash_r = trash.resolve()
        if pick_r.parent != trash_r:
            return {"ok": False, "error": "拒绝：目标不在回收区目录内"}
    except Exception as e:
        return {"ok": False, "error": f"路径校验失败：{e}"}
    try:
        _sh.rmtree(pick_r)
        return {"ok": True, "purged": [pick.name], "n": 1}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── 接入 / 断开（2026-09-26 新语义）────────────────────────────────────
#   「接入」= 建同屏窗格（占用通道）→ 设备模块出现条目
#   「断开」= 只杀窗格（释放通道）→ 设备模块条目消失，但定义仍在库里
#   「删除」= 从 devices.toml / 连接簿 销毁（只在「管理连接簿」里做）
def disconnect_device(dev: str) -> dict:
    """断开一个设备的通道占用（杀窗格），不删任何定义。"""
    info = resolve_device(dev)
    window = info.get("window") or dev
    killed = False
    try:
        r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                           capture_output=True, timeout=8)
        killed = r.returncode == 0
    except Exception:
        pass
    # 顺手清掉可能残留的 client
    try:
        cleanup_term_sessions(dev)
    except Exception:
        pass
    return {"ok": True, "device": dev, "window": window, "disconnected": killed}


def store_kc(service: str, username: str, password: str) -> bool:
    """保存凭据。

    2026-09-26：改用 lib/creds.py 的本地文件存储（~/.netops/credentials.json，600 权限）。
    名字保留 store_kc 是因为调用点没改；不再往 macOS 钥匙串写（改存本地文件）。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return _creds.store_credential(service, username, password)
    except Exception:
        return False


def conn_list() -> list:
    """连接簿（随手接入的临时目标）。"""
    d = netdev_json(["conn", "list", "--json"], timeout=25)
    return d if isinstance(d, list) else []


def remove_device(name: str) -> dict:
    """从 devices.toml 移除一台正式设备（先备份，按 [[device]] 块整块删）。

    netdev 只有 device-add、没有 device-rm —— 所以这里做块级文本删除，
    严格只动目标那一块（保留注释、顺序、其他设备）。
    """
    import re as _re
    p = ROOT / "config" / "devices.toml"
    if not p.exists():
        return {"ok": False, "error": "devices.toml 不存在"}
    txt = p.read_text(encoding="utf-8")
    lines = txt.splitlines(keepends=True)
    out, i, hit = [], 0, False
    while i < len(lines):
        if lines[i].strip() == "[[device]]":
            block, j = [], i
            while j < len(lines) and (j == i or lines[j].strip() != "[[device]]"):
                block.append(lines[j])
                j += 1
            body = "".join(block)
            if re.search(r'^\s*name\s*=\s*"%s"\s*$' % _re.escape(name), body, _re.M):
                hit = True
            else:
                out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    if not hit:
        return {"ok": False, "error": f"未找到设备 {name}"}
    bak = p.with_name(p.name + ".bak-" + time.strftime("%Y%m%d_%H%M%S"))
    bak.write_text(txt, encoding="utf-8")
    p.write_text("".join(out), encoding="utf-8")
    return {"ok": True, "removed": name, "backup": bak.name}


def policy_state() -> dict:
    """当前写操作权限模式（读 state/policy.json，与 lib/approval 同源；含 allow 的 TTL 倒计时）。"""
    p = _P.state_dir() / "policy.json"
    mode, until = "ask", None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        mode = d.get("writes", "ask")
        until = d.get("allow_until")
    except Exception:
        pass
    if mode not in ("readonly", "ask", "allow"):
        mode = "ask"
    remain = None
    if mode == "allow" and isinstance(until, (int, float)):
        remain = int(max(0, until - time.time()))
        if remain <= 0:                     # 过期自动回落确认模式
            mode, remain = "ask", None
    return {"mode": mode, "allow_until": until, "remaining": remain}


# ══════════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════════
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "netdev-ui/0.1"
    protocol_version = "HTTP/1.1"

    # ── 小工具 ──
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):        # 静音：终端流太吵
        pass

    # ── GET ──
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if p.startswith("/static/"):
            return self._file(STATIC / p[len("/static/"):])
        if p == "/api/devices":
            return self._api_devices()
        if p == "/api/health":
            return self._json({"ok": True, "tmux": bool(TMUX), "sessions": len(SESSIONS),
                               "version": APP_VERSION})
        if p == "/api/agents":
            deep = qs.get("deep", ["0"])[0] in ("1", "true", "yes")
            return self._json(probe_agents(deep=deep))
        if p == "/api/direct/config":
            return self._api_direct_cfg_get()
        if p == "/api/direct/models":
            return self._api_direct_models()
        if p == "/api/term/stream":
            return self._api_stream(qs.get("sid", [""])[0])
        if p == "/api/ai/stream":
            return self._api_ai_stream(qs.get("aid", [""])[0])
        if p == "/api/metric/learn/list":
            return self._api_metric_learn_list()
        if p == "/api/metric/one":
            return self._api_metric_one(qs)
        if p == "/api/interface/detail":
            return self._api_interface_detail(qs)
        if p == "/api/monitor":
            return self._api_monitor(qs)
        if p == "/api/policy":
            return self._api_policy_get()
        if p == "/api/ask/pending":
            return self._api_ask_pending()
        if p == "/api/conn/list":
            return self._api_conn_list()
        if p == "/api/panes/orphans":
            return self._api_panes_orphans()
        if p == "/api/panes/list":
            return self._api_panes_list()
        if p == "/api/portlocks":
            return self._api_portlocks()
        if p == "/api/creds/list":
            return self._api_creds_list()
        if p == "/api/serial-discover":
            return self._api_serial_discover()
        if p == "/api/snap/list":
            return self._json({"snapshots": list_snapshots(qs.get("device", [""])[0])})
        if p == "/api/snap/file":
            return self._api_snap_file(qs)
        if p == "/api/snap/trash":
            return self._api_snap_trash(qs)
        return self._send(404, b"not found")

    # ── POST ──
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        if u.path == "/api/term/open":
            return self._api_term_open(b)
        if u.path == "/api/term/input":
            return self._api_term_input(b)
        if u.path == "/api/term/resize":
            return self._api_term_resize(b)
        if u.path == "/api/term/cleanup":
            return self._api_term_cleanup(b)
        if u.path == "/api/term/close":
            return self._api_term_close(b)
        if u.path == "/api/metric/learn":
            return self._api_metric_learn(b)
        if u.path == "/api/metric/learn/save":
            return self._api_metric_learn_save(b)
        if u.path == "/api/metric/learn/remove":
            return self._api_metric_learn_remove(b)
        if u.path == "/api/metric/learn/clear":
            return self._api_metric_learn_clear()
        if u.path == "/api/netdev/run":
            return self._api_netdev_run(b)
        if u.path == "/api/ai/open":
            return self._api_ai_open(b)
        if u.path == "/api/direct/config":
            return self._api_direct_cfg_set(b)
        if u.path == "/api/direct/test":
            return self._api_direct_test()
        if u.path == "/api/ai/send":
            return self._api_ai_send(b)
        if u.path == "/api/ai/respond":
            return self._api_ai_respond(b)
        if u.path == "/api/ai/compact":
            return self._api_ai_compact(b)
        if u.path == "/api/ai/stats":
            return self._api_ai_stats(b)
        if u.path == "/api/ai/abort":
            return self._api_ai_abort(b)
        if u.path == "/api/ai/close":
            return self._api_ai_close(b)
        if u.path == "/api/snap/save":
            return self._api_snap_save(b)
        if u.path == "/api/snap/rm":
            return self._api_snap_rm(b)
        if u.path == "/api/snap/restore":
            return self._api_snap_restore(b)
        if u.path == "/api/snap/purge":
            return self._api_snap_purge(b)
        if u.path == "/api/snap/unrm":
            return self._api_snap_unrm(b)
        if u.path == "/api/policy":
            return self._api_policy_set(b)
        if u.path == "/api/ask":
            return self._api_ask(b)
        if u.path == "/api/ask/respond":
            return self._api_ask_respond(b)
        if u.path == "/api/pi/repair":
            return self._api_pi_repair(b)
        if u.path == "/api/conn/rm":
            return self._api_conn_rm(b)
        if u.path == "/api/portlock/free":
            return self._api_portlock_free(b)
        if u.path == "/api/creds/del":
            return self._api_creds_del(b)
        if u.path == "/api/device/disconnect":
            return self._api_device_disconnect(b)
        if u.path == "/api/device/free":
            return self._api_device_free(b)
        if u.path == "/api/device/rm":
            return self._api_device_rm(b)
        if u.path == "/api/conn/add":
            return self._api_conn_add(b)
        if u.path == "/api/device/add":
            return self._api_device_add(b)
        if u.path == "/api/panes/clean":
            return self._api_panes_clean(b)
        if u.path == "/api/panes/kill":
            return self._api_panes_kill(b)
        return self._send(404, b"not found")

    # ── 静态文件 ──
    def _file(self, path: pathlib.Path):
        try:
            rp = path.resolve()
            if not str(rp).startswith(str(STATIC.resolve())):
                return self._send(403, b"forbidden")
            data = rp.read_bytes()
        except FileNotFoundError:
            return self._send(404, b"not found")
        except Exception as e:
            return self._send(500, str(e).encode())
        ct = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
              ".json": "application/json; charset=utf-8"}.get(rp.suffix, "application/octet-stream")
        self._send(200, data, ct)

    # ── 设备清单：正式（devices.toml）+ 临时（连接簿）合并 ──
    #    原来只列 devices.toml，导致“界面接入的临时设备根本看不见”（用户反馈）
    def _api_devices(self):
        devs = netdev_json(["list", "--json"]) or []
        for d in devs:
            d["kind"] = "formal"
        formal_names = {d.get("name") for d in devs}
        for c in conn_list():                      # 连接簿：随手接入的临时目标
            nm = c.get("id") or c.get("name")
            if not nm or nm in formal_names:       # 同名优先算正式设备
                continue
            devs.append({"name": c.get("name") or nm, "id": nm, "kind": "temp",
                         "protocol": c.get("protocol"), "address": c.get("address"),
                         "host": c.get("host"), "port": c.get("port"),
                         "device": c.get("device"), "baud": c.get("baud"),
                         # ★ 2026-09-27 修：不能用 c["window"] —— netdev conn list 返回的
                         #   那个字段是从 URI 推导的（huawei23 → telnet-192-168-1-1），
                         #   而 CLI 实际建窗格用的是连接簿的 name/id。
                         #   用错名字 → connected 永远 false → 界面显示"未接入"、
                         #   点接入又走"新建连接"→ 报"已存在同名连接"（用户实测踩到）。
                         "note": c.get("note"), "window": c.get("name") or c.get("id"),
                         "username": c.get("username"), "official": False})
        wins = _tmux_live_windows()       # ★ 只认活窗格：死窗格不该标"已接入/在线"
        for d in devs:
            win = d.get("window") or d.get("name")
            d["window_live"] = win in wins
            d["connected"] = win in wins          # ★ 「已接入」= 有【活】同屏窗格
            # 挂上凭据（明文，供界面里直接看；AI 侧看不到）
            c = self._cred_of(str(d.get("id") or d.get("name") or ""), str(d.get("name") or ""))
            d["cred_service"] = c["service"]
            d["cred_username"] = c["username"] or d.get("username") or ""
            d["cred_password"] = c["password"]
        return self._json({"devices": devs, "tmux": TMUX_SESSION,
                           "windows": sorted(wins), "sessions": list(SESSIONS.keys())})

    # ── 开终端 ──
    def _api_term_open(self, b: dict):
        dev = (b.get("device") or "").strip()
        rows = int(b.get("rows") or 40)
        cols = int(b.get("cols") or 140)
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        info = resolve_device(dev)          # 正式 + 临时 都能解析
        # ★ 2026-09-27 修：原来没检查解析结果，于是【接一个不存在的设备也返回成功】
        #   （实测 device=nonexist999 → 返回 {"sid":...}），
        #   界面显示"已接入"，实际什么都没建 —— 用户看到的就是"接入失败"却查不到原因。
        if not info:
            return self._json({"error": f"找不到设备「{dev}」—— 不在 devices.toml，也不在连接簿里。"
                                        f"要新接入请点顶栏「＋」。"}, 404)
        window = info.get("window") or dev
        target = info.get("target") or dev   # 临时目标用设备名开窗
        # ★ 串口：开窗时统一用 @auto，让桥自己探测波特率。
        #   原因：netdev 登记里的 baud 只能量整数（--baud 不接受 auto），常写成默认 9600，
        #   而真机可能是 115200 —— 波特率不对就是“屏幕无输出/满屏乱码”（已实测踩到）。
        #   桥（serial_bridge）本身支持 auto 探测（115200→9600→…），所以开窗这个环节用 auto 最稳。
        #
        # ⚠ 2026-09-29 修：原来只判 target 是否以 "serial:" 开头 —— 但临时目标传进来的
        #   是【设备名】（如 serial-u-usbserial-AABBCCDD-dlt8eust），根本进不了这个分支，
        #   于是它用了连接簿里存的 baud=9600，而真机是 115200 → 屏上全空白。
        #   现在改为【只要是串口设备就一律 @auto】，不再依赖 target 的写法。
        #   ⚠ 但别把 target 改成 `serial:/dev/xxx@auto` 去开窗 —— 那样 netdev 会按
        #     URI 生成窗格名（serial--dev-cu-usbserial-AABBCCDD），跟界面显示的设备名
        #     对不上，反而导致"窗格没建立起来"的误报。
        #     正确做法是让【连接簿里的 baud = auto】（已改），走设备名开窗即可。
        # 该设备还没有同屏窗格 → 请 netdev 建一个（它会自己处理串口独占/桥）
        if TMUX:
            r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                               capture_output=True, text=True, timeout=8)
            # ★ 2026-09-26 修（用户报"点 SSH 不能自动连接"）：
            #   设备侧 vty 有 idle-timeout，闲置超时后设备断开、桥进程退出，
            #   但 tmux 窗格还在，侧栏还显示"窗格在线"。
            #   原来只判"窗格名在不在"，于是直接复用那个空壳 —— 屏上什么都没有。
            #   现在同时判：窗格 dead 状态 + 里面跑的还是不是桥（python）。
            def _pane_state(win):
                try:
                    rr = subprocess.run([TMUX, "list-panes", "-t", f"{TMUX_SESSION}:{win}",
                                         "-F", "#{pane_dead} #{pane_current_command}"],
                                        capture_output=True, text=True, timeout=8)
                    out = (rr.stdout or "").strip()
                    if not out:
                        return None
                    parts = out.split()
                    return {"dead": parts[0] == "1", "cmd": parts[1] if len(parts) > 1 else ""}
                except Exception:
                    return None

            st = _pane_state(window)
            need_spawn = (st is None) or st["dead"] or ("python" not in (st.get("cmd") or ""))
            if need_spawn:
                if st is not None:
                    # 清掉旧空壳，否则 netdev shell 会"看到窗格已在就复用"
                    subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                   capture_output=True, timeout=8)
                    time.sleep(0.6)
                # ★ 2026-09-27 修：把 netdev 的输出留下来（失败时能给出原因），
                #   并且【轮询到窗格真的跑起桥为止】—— 原来超时了也无条件返回 sid，
                #   于是"tmux 会话都不存在 / 设备连不上"也报成功，
                #   界面显示"已接入"但列表里没有（用户实测踩到）。
                _spawn = subprocess.Popen([str(ROOT / "netdev"), "shell", target, "--restart"],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, start_new_session=True)
                _ok = False
                for _ in range(100):                # 最多等 20s（设备登录慢时够用）
                    time.sleep(0.2)
                    st2 = _pane_state(window)
                    if st2 is not None and ("python" in (st2.get("cmd") or "")):
                        _ok = True
                        break
                if not _ok:
                    # 收集 netdev 的输出当原因（它自己会说明为什么连不上）
                    _why = ""
                    try:
                        _spawn.terminate()
                        _out, _ = _spawn.communicate(timeout=3)
                        _why = " ".join((_out or "").split())[:300]
                    except Exception:
                        pass
                    return self._json({
                        "error": f"接入「{dev}」失败：同屏窗格没能建立起来。"
                                 + (f" netdev 输出：{_why}" if _why else
                                    f" 请手动跑一次看报错：netdev shell {target} --restart")
                    }, 500)
        sid = secrets.token_urlsafe(9)
        s = TermSession(sid, dev, window)
        try:
            s.start(rows=rows, cols=cols)
        except Exception as e:
            return self._json({"error": f"终端启动失败：{e}"}, 500)
        with SESS_LOCK:
            SESSIONS[sid] = s
        # ★ 唤醒设备（2026-09-26）：
        #   串口设备空闲时不主动说话 —— 桥连上、波特率也对，但屏上只会停在桥横幅，
        #   等多久都不会自己出 <Huawei>（实测：敲一下回车立刻就出来了）。
        #   所以开终端后主动敲几下回车，让用户打开就看到提示符。
        try:
            for _i in range(3):
                time.sleep(0.7)
                subprocess.run([TMUX, "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"],
                               capture_output=True, timeout=6)
        except Exception:
            pass
        time.sleep(0.6)
        return self._json({"sid": sid, "device": dev, "window": window})

    def _api_term_input(self, b: dict):
        s = SESSIONS.get(b.get("sid", ""))
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        try:
            data = base64.b64decode(b.get("data") or "")
        except Exception:
            return self._json({"error": "data 需要 base64"}, 400)
        s.write(data)
        return self._json({"ok": True, "bytes": len(data)})

    def _api_term_resize(self, b: dict):
        rows = int(b.get("rows") or 40); cols = int(b.get("cols") or 140)
        s = SESSIONS.get(b.get("sid", ""))
        # ★ 埋点：前端到底报了什么尺寸？用来定位"最大化后不填满"
        try:
            with open(str(ROOT / "logs" / "resize.log"), "a", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%H:%M:%S')}\t前端报来 {cols}x{rows}"
                         f"\tsid={b.get('sid','')[:8]}\t存在={bool(s)}\n")
        except Exception:
            pass
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        s.resize(rows, cols)
        # 记下"设置完之后 tmux 里实际是多少"
        try:
            r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F",
                                "#{window_name}=#{window_width}x#{window_height}"],
                               capture_output=True, text=True, timeout=6)
            line = next((x for x in (r.stdout or "").split() if self_win(s) in x), "")
            with open(str(ROOT / "logs" / "resize.log"), "a", encoding="utf-8") as fp:
                fp.write(f"\t\t→ 设置后 tmux: {line or '(取不到)'}\n")
        except Exception:
            pass
        return self._json({"ok": True})

    def _api_term_cleanup(self, b: dict):
        """清理终端残留（临时会话 + 僵尸 client）。"""
        try:
            return self._json({"ok": True, **cleanup_term_sessions(str(b.get("device") or ""))})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _api_term_close(self, b: dict):
        sid = b.get("sid", "")
        with SESS_LOCK:
            s = SESSIONS.pop(sid, None)
        if s:
            s.close()
        return self._json({"ok": True})

    # ── 只读命令直通（AI 与界面共用；写操作不走这里）──
    def _api_netdev_run(self, b: dict):
        dev = (b.get("device") or "").strip()
        cmd = (b.get("command") or "").strip()
        if not dev or not cmd:
            return self._json({"error": "缺少 device/command"}, 400)
        rc, out, err = raw_netdev(["run", dev, cmd])
        return self._json({"rc": rc, "stdout": out, "stderr": err})

    # ══ AI 会话（pi RPC / WorkBuddy headless / direct 直连）══
    def _api_ai_open(self, b: dict):
        backend = (b.get("backend") or "pi").strip().lower()
        if backend == "wb":
            return self._ai_open_wb(b)
        if backend == "direct":
            return self._ai_open_direct(b)
        if not PI_BIN:
            return self._json({"error": "找不到 pi。装法：npm i -g @earendil-works/pi-coding-agent"}, 400)
        aid = secrets.token_urlsafe(9)
        s = AiSession(aid, model=b.get("model") or "", cwd=b.get("cwd") or None,
                      tools=b.get("tools") or "read+netdev")
        try:
            s.start()
        except Exception as e:
            return self._json({"error": f"启动 agent 失败：{e}"}, 500)
        with AI_LOCK:
            AI_SESSIONS[aid] = s
        # 等它把 MCP / 模型等初始化完。
        # ★ 注意：pi 的 RPC 是**惰性**的 —— 不发 prompt 就不吐任何 stdout 事件，
        #   所以「等不到事件」不等于「起不来」。判据改成「进程还活着 = 就绪」，
        #   否则健康会话也会被永远显示成"初始化中"。
        #   同时**一旦进程死掉就立刻返回**，不必把 12 秒等满 —— 失败要快。
        t0 = time.time()
        while time.time() - t0 < 6:
            if s.ready.is_set() or (s.proc and s.proc.poll() is not None):
                break
            time.sleep(0.25)
        ready = s.ready.is_set()
        alive = bool(s.proc and s.proc.poll() is None)
        if not ready and alive:
            ready = True
        resp = {"aid": aid, "backend": "pi", "model": s.model or "（pi 默认）",
                "cwd": s.cwd, "pid": s.proc.pid, "ready": bool(ready), "alive": alive,
                "tools": s.tools_key,
                "tool_list": s.tools.split(",") if s.tools else None}
        if s.heal.get("moved"):
            resp["healed"] = s.heal["moved"]        # 启动时顺手修掉的残留锁（界面可提示）
        if not alive:
            resp["ready"] = False
            resp["error"] = ("pi 进程已退出：" + ((s.stderr_tail or "").strip()[:400] or "无 stderr 输出"))
            resp["hint"] = ("常见真因：~/.pi/agent 下有崩溃残留的 *.json.lock 目录 → "
                            "已尝试自动修复；若仍失败，请检查 settings.json 里的 packages "
                            "能否连通（国内建议 npm 走镜像），或改用 WorkBuddy / 直连 API 后端。")
        return self._json(resp)

    def _ai_open_wb(self, b: dict):
        """WorkBuddy 后端：按需拉起，无常驻进程可等。"""
        if not _wb_bin():
            return self._json({"error": "找不到 WorkBuddy 的 codebuddy CLI"
                                "（NETDEV_WB_BIN / 桌面 App 内置 / PATH 三处都没命中）"}, 400)
        aid = secrets.token_urlsafe(9)
        s = WbSession(aid, model=b.get("model") or "", cwd=b.get("cwd") or None,
                      tools=b.get("tools") or "read+netdev")
        with AI_LOCK:
            AI_SESSIONS[aid] = s
        return self._json({"aid": aid, "backend": "wb",
                           "model": s.model or "（WorkBuddy 默认）",
                           "cwd": s.cwd, "pid": "按需拉起", "ready": True,
                           "tools": s.tools_key, "tool_list": None})

    def _ai_open_direct(self, b: dict):
        """Direct 后端：直连 OpenAI 兼容 API，无需本机 CLI。"""
        cfg = _direct_cfg()
        if not cfg["ok"]:
            return self._json({"error": cfg["note"]}, 400)
        aid = secrets.token_urlsafe(9)
        s = DirectSession(aid, model=b.get("model") or "", cwd=b.get("cwd") or None,
                          tools=b.get("tools") or "read+netdev")
        with AI_LOCK:
            AI_SESSIONS[aid] = s
        tool_list = [t["function"]["name"] for t in s._tool_schemas] if s._tool_schemas else None
        return self._json({"aid": aid, "backend": "direct",
                           "model": s.model or "（provider 默认）",
                           "cwd": s.cwd, "pid": "无（HTTP 直连）", "ready": True,
                           "tools": s.tools_key, "tool_list": tool_list})

    def _api_direct_cfg_get(self):
        """直连配置快照（key 脱敏）。"""
        return self._json(_direct_cfg_public())

    def _api_direct_cfg_set(self, b: dict):
        """保存直连配置 → config/direct.json（600 权限，gitignore 已排除）。

        api_key 留空 = 保留已保存的 Key（界面不回显明文，改 Key 才填）。
        """
        p = _DIRECT_CFG_FILE
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        provider = str(b.get("provider") or "").strip().lower()
        base_url = str(b.get("base_url") or "").strip()
        model = str(b.get("model") or "").strip()
        api_key = str(b.get("api_key") or "").strip()
        if provider in _DIRECT_PROVIDERS:      # 表内 provider：空缺项用默认补齐
            pd = _DIRECT_PROVIDERS[provider]
            base_url = base_url or pd["base_url"]
            model = model or pd["default_model"]
        api_key = api_key or str(old.get("api_key") or "").strip()
        if not api_key:
            return self._json({"ok": False,
                               "error": "API Key 不能为空（首次配置必须粘贴 Key）"}, 400)
        if not base_url:
            return self._json({"ok": False, "error": "接口地址不能为空"}, 400)
        cfg = {"provider": provider, "api_key": api_key,
               "base_url": base_url.rstrip("/"), "model": model}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            os.chmod(p, 0o600)
        except Exception as e:
            return self._json({"ok": False,
                               "error": f"写配置失败：{type(e).__name__}: {e}"}, 500)
        out = _direct_cfg_public()
        out["saved"] = True
        if out.get("key_src_env"):             # 环境变量优先级高于文件，得说清楚
            out["note"] = ("已写入 config/direct.json；但本进程环境变量里有 "
                           "NETDEV_DIRECT_API_KEY（优先级更高），去掉该变量后文件配置才生效")
        return self._json(out)

    def _api_direct_test(self):
        """直连连通测试（真发一条最小 prompt）。"""
        ok, detail = _direct_probe()
        return self._json({"ok": ok, "detail": detail})

    def _api_direct_models(self):
        """账户实时可用模型列表（给模型的 datalist 当候选）。"""
        models = _direct_models()
        return self._json({"ok": bool(models), "models": models})

    def _api_ai_send(self, b: dict):
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        text = (b.get("message") or "").strip()
        if not text:
            return self._json({"error": "缺少 message"}, 400)
        ok = s.prompt(text)
        if not ok:
            # 写 stdin 失败 = 进程已经死了。把 stderr 尾巴带出去，别让用户对着
            # 一个假死的面板干等（实测踩过：pi 因锁残留起不来，界面只显示"无响应"）。
            dead = getattr(s, "proc", None) is not None and s.proc.poll() is not None
            return self._json({"ok": False, "accepted": False,
                               "error": "agent 进程已退出，本条消息未送达" if dead else "发送失败（stdin 不可写）",
                               "detail": (getattr(s, "stderr_tail", "") or "").strip()[:400]})
        return self._json({"ok": ok, "accepted": ok})

    def _api_ai_respond(self, b: dict):
        """界面里的人点了同意/拒绝 → 回传给 pi —— 审批通道的另一半。"""
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        ok = s.respond_ui(str(b.get("id") or ""), b.get("value"), bool(b.get("cancelled")))
        return self._json({"ok": ok})

    def _api_ai_compact(self, b: dict):
        """手动压缩 AI 上下文（长对话用）。"""
        aid = b.get("aid", "")
        s = AI_SESSIONS.get(aid)
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        ok = s.compact(str(b.get("instructions") or ""))
        return self._json({"ok": ok,
                           "msg": "已请求压缩：pi 会把旧消息摘要掉，保留最近的工作上下文" if ok else "发送失败"})

    def _api_ai_stats(self, b: dict):
        """看当前会话的上下文用量。"""
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        s.stats()
        return self._json({"ok": True, "msg": "已查询（结果会以事件形式回到会话流）"})

    def _api_ai_abort(self, b: dict):
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        return self._json({"ok": s.abort()})

    def _api_ai_close(self, b: dict):
        with AI_LOCK:
            s = AI_SESSIONS.pop(b.get("aid", ""), None)
        if s:
            s.close()
        return self._json({"ok": True})

    def _api_ai_stream(self, aid: str):
        s = AI_SESSIONS.get(aid)
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, replay = s.subscribe()
        try:
            for ev in replay:
                self._sse(b"ev", json.dumps(ev, ensure_ascii=False).encode())
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    self._sse(b"end", b"")
                    break
                self._sse(b"ev", json.dumps(ev, ensure_ascii=False).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)

    def _api_serial_discover(self):
        try:
            return self._json({"ports": serial_ports()})
        except Exception as e:
            return self._json({"ports": [], "error": str(e)}, 500)

    def _api_conn_add(self, b: dict):
        """加入连接簿（临时目标）。密码如有 → 同存本地凭据文件。"""
        import json as _json
        proto = (b.get("protocol") or "ssh").strip()
        name = (b.get("name") or "").strip()
        args = ["conn", "add", proto]
        if proto == "serial":
            dev = (b.get("device") or "").strip()
            if not dev:
                return self._json({"error": "请选择串口设备"}, 400)
            args += ["--device", dev]
            # 注意：netdev 的 --baud 【只收整数】，传 'auto' 会 argparse 报错（已实测）。
            # 前端下拉里有 auto 选项 ⇒ auto 就不传，让 netdev 用默认（自动探测）。
            _b = str(b.get("baud") or "").strip().lower()
            if _b and _b != "auto":
                args += ["--baud", _b]
        else:
            host = (b.get("host") or "").strip()
            if not host:
                return self._json({"error": "请填主机地址"}, 400)
            args += [host]
            if b.get("port"):
                args += ["--port", str(b["port"])]
        if name:
            args += ["--name", name]
        if b.get("username"):
            args += ["--username", b["username"]]
        if b.get("note"):
            args += ["--note", b["note"]]
        if b.get("platform"):
            args += ["--platform", b["platform"]]
        args += ["--json"]
        rc, out, err = raw_netdev(args, timeout=90)
        data = {}
        try:
            data = _json.loads(out.strip()) if out.strip().startswith(("{", "[")) else {}
        except Exception:
            data = {}
        key = (data.get("id") or data.get("name")) if isinstance(data, dict) else None
        if not key:
            # 实测：conn add 的 --json 并不输出 JSON，所以从人读文本里把名字抓出来
            # 形如：  ✔ 已加入连接簿：test-cli-ssh ｜ ssh ｜ 10.99.99.99:22
            import re as _re
            m = re.search(r"已加入连接簿[:：]\s*(\S+)", strip_ansi(out))
            key = m.group(1) if m else name
        saved = None
        if rc == 0 and b.get("password") and key:
            saved = store_kc(f"netdev-{key}", b.get("username") or "admin", b["password"])
        return self._json({"ok": rc == 0, "rc": rc, "key": key, "password_saved": saved,
                           "stdout": strip_ansi(out)[-600:], "stderr": strip_ansi(err)[-300:]})

    def _api_device_add(self, b: dict):
        """登记为正式设备（写入 devices.toml）。密码如有 → 存本地凭据文件 netdev-<name>。"""
        proto = (b.get("protocol") or "ssh").strip()
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "正式设备必须填名称"}, 400)
        args = ["device-add", "--name", name, "--protocol", proto]
        if proto == "serial":
            dev = (b.get("device") or "").strip()
            if not dev:
                return self._json({"error": "请选择串口设备"}, 400)
            args += ["--device-port", dev]
            # 同上：--baud 只收整数，'auto' 会报错 ⇒ 不传
            _b = str(b.get("baud") or "").strip().lower()
            if _b and _b != "auto":
                args += ["--baud", _b]
        else:
            host = (b.get("host") or "").strip()
            if not host:
                return self._json({"error": "请填主机地址"}, 400)
            args += ["--host", host]
            if b.get("port"):
                args += ["--port", str(b["port"])]
        if b.get("username"):
            args += ["--username", b["username"]]
        if b.get("platform"):
            args += ["--platform", b["platform"]]
        if b.get("tags"):
            args += ["--tags", b["tags"]]
        rc, out, err = raw_netdev(args, timeout=120)
        saved = None
        if rc == 0 and b.get("password"):
            saved = store_kc(f"netdev-{name}", b.get("username") or "admin", b["password"])
        return self._json({"ok": rc == 0, "rc": rc, "name": name, "password_saved": saved,
                           "stdout": strip_ansi(out)[-800:], "stderr": strip_ansi(err)[-400:]})

    # ══ 设备 / 连接簿管理 ══
    def _api_conn_list(self):
        items = conn_list()
        for c in items:                       # 附上密码（明文，供连接簿条目直接看）
            cr = self._cred_of(str(c.get("id") or ""), str(c.get("name") or ""))
            c["password"] = cr["password"]
            c["cred_username"] = cr["username"] or c.get("username") or ""
        return self._json({"items": items})

    def _api_conn_rm(self, b: dict):
        key = (b.get("key") or "").strip()
        if not key:
            return self._json({"error": "缺少 key"}, 400)
        # 先记下它的窗格名：删条目后如果窗格还留着，串口会被它占死 —— 下次就接不进来了
        info = resolve_device(key)
        window = info.get("window") or ""
        # 注意：netdev conn rm 【不接受 --yes】——硬加会 argparse 报错（已实测）。直接删即可。
        rc, out, err = raw_netdev(["conn", "rm", key], timeout=60)
        killed = False
        if rc == 0 and window and TMUX:
            with screen_lock(key):
                r2 = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                    capture_output=True, timeout=8)
                killed = r2.returncode == 0
        return self._json({"ok": rc == 0, "stdout": strip_ansi(out)[-400:],
                           "pane_killed": killed, "pane": window,
                           "stderr": strip_ansi(err)[-300:]})

    # ══ 串口/窗格占用一览 + 单窗格释放 ══
    #   “接入”会建同屏窗格，而串口是物理独占的 —— 窗格不释放，同一个串口就再也接不进来。
    #   原来的“清僵尸”只清没人认领的，用户自己接的那种有主子的清不掉（用户反馈过）。
    def _api_panes_list(self):
        wins = _tmux_windows()
        devmap = {}
        for d in (netdev_json(["list", "--json"]) or []):
            devmap[d.get("window") or d.get("name")] = f"正式设备 {d.get('name')}"
        for c in conn_list():
            devmap.setdefault(c.get("window") or c.get("id"), f"临时目标 {c.get('id')}")
        rows = []
        for w in sorted(wins):
            # serial--dev-cu-usbserial-AABBCCDD  →  /dev/cu.usbserial-AABBCCDD
            serial = ""
            if w.startswith("serial--"):
                serial = "/" + w[len("serial--"):].replace("-", "/", 1).replace("-", ".", 1)
                serial = "/dev/cu." + w[len("serial--dev-cu-"):] if w.startswith("serial--dev-cu-") else serial
            rows.append({"window": w, "owner": devmap.get(w, "（无主子）"),
                         "serial": serial, "orphan": w not in devmap})
        # 串口占用（直接问系统）
        busy = {}
        for p in serial_ports():
            try:
                rr = subprocess.run(["/usr/sbin/lsof", "-t", p["path"]],
                                    capture_output=True, text=True, timeout=8)
                busy[p["path"]] = bool((rr.stdout or "").strip())
            except Exception:
                busy[p["path"]] = None
        return self._json({"panes": rows, "serial_busy": busy})

    def _api_panes_kill(self, b: dict):
        """释放指定窗格（无论它有没有主子）—— 人主动要求才杀。"""
        w = (b.get("window") or "").strip()
        if not w:
            return self._json({"error": "缺少 window"}, 400)
        if w not in _tmux_windows():
            return self._json({"error": f"窗格不存在：{w}"}, 404)
        r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{w}"],
                           capture_output=True, timeout=8)
        return self._json({"ok": r.returncode == 0, "killed": w})

    # ══ 清理“僵尸窗格” ══
    #   只把「名字像自动生成的串口窗格」（serial--dev-cu-* / telnet-* / ssh-*）
    #   且当前没人认领的算作僵尸 —— 避免把用户手动开的窗格（如 switch-01）误判。
    @staticmethod
    def _looks_autogen(w: str) -> bool:
        return bool(__import__("re").match(r"^(serial--|telnet-|ssh-)", w or ""))

    def _api_panes_orphans(self):
        formal = {(d.get("window") or d.get("name")) for d in (netdev_json(["list", "--json"]) or [])}
        conn = {(c.get("window") or c.get("id")) for c in conn_list()}
        keep = formal | conn
        rows = []
        for w in sorted(_tmux_windows()):
            rows.append({"window": w, "orphan": (w not in keep) and self._looks_autogen(w)})
        return self._json({"panes": rows, "keep": sorted(keep)})

    def _api_panes_clean(self, b: dict):
        """真的杀。只杀“没人认领”的窗格，不碰正式设备/连接簿的窗格。"""
        formal = {(d.get("window") or d.get("name")) for d in (netdev_json(["list", "--json"]) or [])}
        conn = {(c.get("window") or c.get("id")) for c in conn_list()}
        keep = formal | conn
        killed = []
        for w in list(_tmux_windows()):
            if w in keep or not self._looks_autogen(w):
                continue
            r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{w}"],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                killed.append(w)
        return self._json({"ok": True, "killed": killed, "kept": sorted(keep)})

    # ── 凭据查看 / 删除（本地明文文件）──
    #   ★ 安全边界：这些接口只给网页上的人用。
    #     AI（pi 子进程）的工具白名单是 read/read+netdev，**没有任何 web/bash 工具**，
    #   而 netdev 的 MCP 工具清单里也没有凭据类接口 → AI 拿不到明文密码。
    @staticmethod
    def _cred_of(dev_id: str, name: str = "") -> dict:
        """查一个对象对应的凭据（明文）。"""
        try:
            sys.path.insert(0, str(ROOT))
            from lib import creds as _creds      # noqa: PLC0415
            for svc in (f"netdev-{dev_id}", f"netdev-{name}"):
                if not svc or svc == "netdev-":
                    continue
                u, p = _creds.keychain_item(svc)
                if p or u:
                    return {"service": svc, "username": u or "", "password": p or ""}
        except Exception:
            pass
        return {"service": "", "username": "", "password": ""}

    def _api_creds_list(self):
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return self._json({"file": _creds.CRED_FILE,
                           "items": _creds.list_credentials(show_password=True)})

    def _api_creds_del(self, b: dict):
        svc = (b.get("service") or "").strip()
        if not svc:
            return self._json({"error": "缺少 service"}, 400)
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return self._json({"ok": bool(_creds.delete_credential(svc)), "deleted": svc})

    # ── 串口锁（带业主）──
    def _api_portlocks(self):
        sys.path.insert(0, str(ROOT))
        from lib import portlock as _pl            # noqa: PLC0415
        return self._json({"locks": _pl.all_locks(), "file": str(_pl.LOCKFILE)})

    def _api_portlock_free(self, b: dict):
        """人工释放某个串口锁（网页上的「释放」）。"""
        port = (b.get("port") or "").strip()
        if not port:
            return self._json({"error": "缺少 port"}, 400)
        sys.path.insert(0, str(ROOT))
        from lib import portlock as _pl            # noqa: PLC0415
        return self._json({"ok": _pl.force_release(port), "port": port})

    # ── 只释放通道占用（杀同屏窗格），不动 devices.toml ──
    #   正式设备的 ✕ 走这里：设备保留，只把占着串口/连接的那个窗格释掉。
    #   （之前 ✕ 会真从 devices.toml 里删设备，被误点太多次了）
    def _api_device_disconnect(self, b: dict):
        """断开：只释放通道，不删定义。"""
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        try:
            return self._json(disconnect_device(name))
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _api_device_free(self, b: dict):
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        info = resolve_device(name)
        window = info.get("window") or ""
        killed = False
        if window and TMUX:
            with screen_lock(name):
                r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                   capture_output=True, timeout=8)
                killed = r.returncode == 0
        return self._json({"ok": True, "freed": name, "pane_killed": killed, "pane": window})

    # ── 真删除：从 devices.toml 移除 + 连带清窗格 ──
    #   入口是「📋 管理连接簿」弹窗（不是列表上的 ✕），有备份。
    def _api_device_rm(self, b: dict):
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        info = resolve_device(name)
        window = info.get("window") or ""
        r = remove_device(name)
        killed = False
        if r.get("ok") and window and TMUX:
            with screen_lock(name):
                rr = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                    capture_output=True, timeout=8)
                killed = rr.returncode == 0
        return self._json({"ok": bool(r.get("ok")), "removed": name,
                           "backup": r.get("backup"), "error": r.get("error"),
                           "pane_killed": killed, "pane": window})

    # ══ 网页审批通道（netdev 的 lib/approval 打过来）══
    def _api_ask(self, b: dict):
        """阻塞等待「人在网页上点一下」。

        这就是人机分离的落地：AI 侧工具白名单里没有 bash/web，它发不出这个 POST；
        审批结果只存在内存，也不落文件，所以它也无从伪造。
        """
        # 通道健康检查：最近 30 秒内没有浏览器轮询 ⇒ 界面没开或是旧版页面。
        # 这时直接告诉 approval “网页通道不可用”，让它回退原生弹窗 —— 避免静默超时拒发。
        if time.time() - ASK_LAST_POLL[0] > 30:
            return self._json({"available": False, "allow": False,
                               "reason": "网页未在轮询（页面没开或未刷新）"})
        rid = secrets.token_urlsafe(8)
        ev = threading.Event()
        item = {"id": rid, "device": b.get("device") or "-",
                "lines": [str(x) for x in (b.get("lines") or [])],
                "kind": b.get("kind") or "write", "ev": ev, "allow": None,
                "shown": False, "at": time.time()}
        with ASK_LOCK:
            ASK_PENDING[rid] = item
        timeout = float(b.get("timeout") or 60)
        try:
            answered = ev.wait(timeout=timeout + 5)
        finally:
            with ASK_LOCK:
                ASK_PENDING.pop(rid, None)
        return self._json({"allow": bool(item["allow"]) if answered else False,
                           "req_id": rid, "answered": bool(answered)})

    def _api_ask_pending(self):
        """浏览器长轮询：取一个待审批请求（最多等 25 秒）。

        注意：不做“只发一次”限制 —— 多标签页时，若第一个标签弹窗失败/被忽略，
        请求就被永久吞掉（已实测踩到：连续两次 DENY 都是没人应答）。
        改成可重复取，谁先应答谁生效（应答后 answered=True 就不再下发）。
        """
        ASK_LAST_POLL[0] = time.time()          # 心跳：证明“界面真的开着且是新版页面”
        t0 = time.time()
        while time.time() - t0 < 25:
            with ASK_LOCK:
                for it in ASK_PENDING.values():
                    if not it.get("answered"):
                        return self._json({"req": {"id": it["id"], "device": it["device"],
                                                   "lines": it["lines"], "kind": it["kind"]}})
            time.sleep(0.4)
        return self._json({"req": None})

    def _api_ask_respond(self, b: dict):
        rid = str(b.get("id") or "")
        with ASK_LOCK:
            it = ASK_PENDING.get(rid)
        if not it:
            return self._json({"ok": False, "error": "请求不存在或已过期"}, 404)
        it["allow"] = bool(b.get("allow"))
        it["answered"] = True
        it["ev"].set()
        return self._json({"ok": True})

    # ══ pi 启动自愈（真因见文件头「pi 启动自愈」注释块）══
    def _api_pi_repair(self, b: dict):
        """把崩溃残留的 pi 锁目录移入隔离区；可选顺带真起一次 pi 验证。

        dry=1 时只报告不落手 —— 界面先展示「要动哪些」，用户点了才修。
        """
        dry = bool(b.get("dry"))
        found = [p.name for p in _pi_stale_locks()]
        if dry or not found:
            return self._json({"ok": True, "dry": dry, "found": found,
                               "moved": [], "verify": None,
                               "note": "没有需要修复的残留锁" if not found else "以上为待修复项"})
        r = pi_heal_locks()
        verify = None
        if b.get("verify"):
            pi = _which("pi")
            if pi:
                ok, why = _rpc_startable(pi, ["--mode", "rpc", "--no-session"])
                verify = {"ok": ok, "detail": "pi RPC 可拉起" if ok else why}
        return self._json({"ok": bool(r.get("ok")), "dry": False, "found": found,
                           "moved": r.get("moved", []), "quarantine": r.get("quarantine"),
                           "verify": verify,
                           "note": "已移入隔离区（可原样还原）；建议重开一次 AI 会话"})

    # ══ 写操作策略（三模式）══
    def _api_policy_get(self):
        return self._json(policy_state())

    def _api_policy_set(self, b: dict):
        mode = (b.get("mode") or "").strip()
        if mode not in ("readonly", "ask", "allow"):
            return self._json({"error": "mode 只能是 readonly / ask / allow"}, 400)
        rc, out, err = raw_netdev(["policy", mode], timeout=180)   # 切 allow 会弹人审窗
        st = policy_state()
        return self._json({"ok": rc == 0, "rc": rc, "requested": mode,
                           "stdout": out[-600:], "stderr": err[-400:], **st})

    # ══ 排障监控 ══
    # AI 提议时看到的原始回显，按 device:key 暂存；
    # 保存时必须用【同一份】来自检 —— 之前保存用的是截断后的 sample，
    # 与 AI 看到的那段不一致，导致"提议时通过、保存时匹配不到"（实测踩到）。
    _LEARN_CACHE: dict = {}

    def _api_metric_learn(self, b: dict):
        """让 AI 提议一条解析规则（只提议，不保存）。"""
        dev = (b.get("device") or "").strip()
        key = (b.get("key") or "").strip()
        if not dev or not key:
            return self._json({"error": "缺少 device/key"}, 400)
        if _learned is None:
            return self._json({"error": "学习模块不可用"}, 500)
        with screen_lock(dev):
            try:
                one = metric_one(dev, key)
            except Exception as e:
                return self._json({"error": f"取回显失败：{e}"}, 500)
        if one.get("error"):
            return self._json(one, 500)
        raw = one.get("output") or ""
        sug = ai_suggest_pattern(dev, key, raw, one.get("cmd") or "")
        if sug.get("error"):
            return self._json({**sug, "raw": raw[-1500:]})
        # 自检（三道护栏之一）——用 AI 看到的那份原文
        ok, why, got = _learned.validate(sug.get("pattern"), raw, sug.get("value"))
        self._LEARN_CACHE[f"{dev}:{key}"] = raw      # 供保存时复用同一份
        plat = _platform_of(dev) or ""
        # 未标平台 → 只能存设备级（避免不同厂商挤进同一个桶里互相串）
        scopes = [{"scope": "dev", "label": f"只给这台设备（{dev}）"}]
        if plat:
            scopes.append({"scope": "plat", "label": f"给该平台所有设备（{plat}）"})
        return self._json({"ok": True, "device": dev, "key": key,
                           "platform": plat or "(未标注)",
                           "scopes": scopes, "cmd": one.get("cmd"),
                           "sample": raw[:2500],
                           "ai_value": sug.get("value"),
                           "ai_pattern": sug.get("pattern"),
                           "selfcheck_ok": ok, "selfcheck_msg": why,
                           "selfcheck_value": got,
                           "verified": bool(ok and sug.get("value") is not None)})

    def _api_metric_learn_save(self, b: dict):
        """采纳 AI 的提议（保存到学习档案）。"""
        if _learned is None:
            return self._json({"error": "学习模块不可用"}, 500)
        key = (b.get("key") or "").strip()
        pat = b.get("pattern") or ""
        dev = (b.get("device") or "").strip()
        scope = (b.get("scope") or "dev").strip()          # 默认设备级（最安全）
        plat = (b.get("platform") or "").strip()
        name = dev if scope == "dev" else (plat or dev)
        # 用提议时缓存的原文（与 AI 看到的完全一致）做自检；
        # 取不到才退回前端传来的 sample。
        sample = self._LEARN_CACHE.get(f"{dev}:{key}") or b.get("sample") or ""
        val = b.get("value")
        ok, why = _learned.put_scoped(scope, name, key, pat, sample, val, source="ai")
        if ok:
            self._LEARN_CACHE.pop(f"{dev}:{key}", None)
        return self._json({"ok": ok, "msg": why, "scope": scope, "name": name, "key": key})

    def _api_metric_learn_list(self):
        if _learned is None:
            return self._json({"rules": []})
        return self._json({"rules": _learned.all_rules_scoped(), "file": str(_learned.FILE)})

    def _api_metric_learn_remove(self, b: dict):
        """删除一条学到的规则。"""
        if _learned is None:
            return self._json({"ok": False})
        full = (b.get("platform") or b.get("full") or "").strip()
        return self._json({"ok": _learned.remove(full, (b.get("key") or "").strip())})

    def _api_metric_learn_clear(self):
        if _learned is None:
            return self._json({"ok": False})
        return self._json({"ok": True, "cleared": _learned.clear()})

    def _api_metric_one(self, qs):
        """单项指标：走同屏取，不在终端里插命令。"""
        dev = (qs.get("device", [""])[0] or "").strip()
        key = (qs.get("key", [""])[0] or "").strip()
        if not dev or not key:
            return self._json({"error": "缺少 device/key"}, 400)
        with screen_lock(dev):
            try:
                return self._json(metric_one(dev, key))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_interface_detail(self, qs):
        """接口下钻：在线接口的带宽占用率 / CRC / 光衰。"""
        dev = (qs.get("device", [""])[0] or "").strip()
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        with screen_lock(dev):
            try:
                return self._json(interface_detail(dev))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_monitor(self, qs):
        dev = (qs.get("device", [""])[0] or "").strip()
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        try:
            return self._json(collect_metrics(dev))
        except Exception as e:
            return self._json({"error": f"采集失败：{type(e).__name__}: {e}"}, 500)

    # ══ 快照管理 ══
    def _api_snap_save(self, b: dict):
        dev = (b.get("device") or "").strip()
        tag = (b.get("tag") or "").strip() or "UI手工"
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        with screen_lock(dev):          # 与监控采集 / 其它同屏操作互斥，避免抓配置时被插命令
            info = resolve_device(dev)
            target = info.get("target") or dev     # 临时设备 → 用 URI 当快照目标
            rc, out, err = raw_netdev(["snap", "save", target, "--tag", tag], timeout=300)
        return self._json({"ok": rc == 0, "rc": rc, "stdout": strip_ansi(out)[-2000:],
                           "stderr": strip_ansi(err)[-600:]})

    def _api_snap_rm(self, b: dict):
        dev = (b.get("device") or "").strip()
        idx = str(b.get("idx") or "").strip()
        if not dev or not idx:
            return self._json({"error": "缺少 device/idx"}, 400)
        rc, out, err = raw_netdev(["snap", "rm", dev, idx, "--yes"], timeout=90)   # 不带 --yes 只是预览
        # 注意：netdev 移入回收区后还会去复制 workbuddy 副本，源目录已移走 → FileNotFoundError，
        # 于是 rc≠0 但实际已经删成功了。所以用「该项是否还在快照目录」判定真实结果。
        still = any(str(idx) == str(s.get("idx"))
                    for s in (netdev_json(["snap", "list", "--json"], timeout=25) or []))
        return self._json({"ok": not still, "rc": rc, "stdout": out[-1200:], "stderr": err[-400:]})

    def _api_snap_purge(self, b: dict):
        """彻底删除回收区的内容（物理删除，不可恢复）。ref 不给则按 all 处理。"""
        try:
            return self._json(snap_purge(str(b.get("ref") or ""), bool(b.get("all"))))
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _api_snap_trash(self, qs):
        """回收区：netdev snap trash 只输出文本，这里解析成结构化列表。"""
        import re as _re
        _rc, out, _e = raw_netdev(["snap", "trash"], timeout=40)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", out)     # 先去 ANSI，否则行尾是 \x1b[0m
        items = []
        for m in _re.finditer(r"^\s+(\S+?)(?:_removed-(\d{8})_?(\d{6})?)?\s+(\d+)B\s*$", clean, _re.M):
            name = m.group(1)
            stamp = ""
            if m.group(2):
                s = m.group(2) + (m.group(3) or "")
                stamp = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}" if len(s) >= 12 else s
            items.append({"name": name, "full": m.group(0).strip().split()[0],
                          "size": int(m.group(4)), "removed_at": stamp})
        return self._json({"items": items, "raw": out[-1500:]})

    def _api_snap_unrm(self, b: dict):
        dev = (b.get("device") or "").strip()
        ref = (b.get("ref") or "").strip()
        if not ref:
            return self._json({"error": "缺少 ref"}, 400)
        args = ["snap", "unrm"] + ([dev] if dev else []) + [ref]
        rc, out, err = raw_netdev(args, timeout=90)
        if rc != 0 and ("--yes" in (err or "") or "确认" in (out or "")):
            rc, out, err = raw_netdev(args + ["--yes"], timeout=90)
        return self._json({"ok": rc == 0, "rc": rc, "stdout": out[-1500:], "stderr": err[-500:]})

    def _api_snap_restore(self, b: dict):
        dev = (b.get("device") or "").strip()
        idx = str(b.get("idx") or "").strip()
        apply = bool(b.get("apply"))
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        args = ["snap", "restore", dev] + (["--from", idx] if idx else [])
        if apply:
            args += ["--apply", "--yes"]          # 写操作：仍由 netdev 弹人审窗
        rc, out, err = raw_netdev(args, timeout=360)
        return self._json({"ok": rc == 0, "rc": rc, "applied": apply,
                           "stdout": out[-4000:], "stderr": err[-800:]})

    def _api_snap_file(self, qs):
        sid = (qs.get("id", [""])[0] or "").strip()
        name = pathlib.Path(qs.get("name", ["running.cfg"])[0]).name
        base = (ROOT / "backups" / "snapshots").resolve()
        f = (base / sid / name).resolve()
        if not str(f).startswith(str(base)):
            return self._send(403, b"forbidden")
        try:
            data = f.read_bytes()
        except Exception:
            return self._send(404, b"not found")
        return self._send(200, data, "text/plain; charset=utf-8")

    # ── SSE 终端流 ──
    def _api_stream(self, sid: str):
        s = SESSIONS.get(sid)
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q, snap = s.subscribe()
        try:
            if snap:
                self._sse(b"snap", snap)
            while True:
                try:
                    data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # 保活
                    self.wfile.flush()
                    continue
                if data is None:
                    self._sse(b"end", b"")
                    break
                self._sse(b"out", data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)

    def _sse(self, ev: bytes, data: bytes) -> None:
        payload = base64.b64encode(data)
        self.wfile.write(b"event: " + ev + b"\ndata: " + payload + b"\n\n")
        self.wfile.flush()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """浏览器刷新/关标签导致的 RST 是常态，不要刷 traceback。"""
        if sys.exc_info()[0] in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


def cleanup_stale() -> int:
    """清掉上次异常退出残留的 ui-* 会话（只动 ui-*，绝不碰 netops / view-*）。"""
    if not TMUX:
        return 0
    n = 0
    try:
        r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=8)
        for name in (r.stdout or "").split():
            if name.startswith("ui-"):
                subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True, timeout=8)
                n += 1
    except Exception:
        pass
    return n


def idle_reaper() -> None:
    """无浏览器订阅、且长时间无输入的终端会话，自动回收 —— 避免 tmux 会话堆积。"""
    while True:
        time.sleep(60)
        now = time.time()
        with SESS_LOCK:
            dead = [sid for sid, s in SESSIONS.items() if not s.subs and now - s.last_active > 600]
            for sid in dead:
                s = SESSIONS.pop(sid)
                try:
                    s.close()
                except Exception:
                    pass
        with AI_LOCK:      # AI 会话：空闲 30 分钟且无订阅者就收掉（进程有成本）
            dead_ai = [aid for aid, s in AI_SESSIONS.items()
                       if not s.subs and now - s.last_active > 1800]
            for aid in dead_ai:
                s = AI_SESSIONS.pop(aid)
                try:
                    s.close()
                except Exception:
                    pass


def main() -> int:
    global UI_BASE
    ap = argparse.ArgumentParser(description="netdev-ui —— netdev 的独立界面")
    ap.add_argument("--port", type=int, default=8898)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    UI_BASE = f"http://{a.host}:{a.port}"
    # ★ 2026-09-30：把自己的地址也写进【本进程的 os.environ】——
    #   不只是 Python 变量。原因：① direct 后端在进程内 import netdev_mcp，
    #   它的 _netdev_cli 靠 os.environ 转发审批地址；② 任何从本进程继承
    #   环境的子进程（raw_netdev 的 CLI 等）都能看到网页审批通道。
    #   之前 UI_BASE 只是 Python 变量，os.environ 里没有 → direct/界面下发
    #   的审批全部静默回退到 macOS 原生弹窗（用户实测反馈）。
    os.environ["NETDEV_APPROVAL_URL"] = UI_BASE

    if not TMUX:
        print("⚠ 没找到 tmux：终端同屏不可用（其他功能仍在）", file=sys.stderr)
    if not (ROOT / "netdev").exists():
        print(f"⚠ 没找到 {ROOT/'netdev'}：设备清单与命令透传将不可用", file=sys.stderr)

    n = cleanup_stale()
    if n:
        print(f"  已清理上次残留的 {n} 个 ui-* 会话")
    threading.Thread(target=idle_reaper, daemon=True).start()

    srv = Server((a.host, a.port), Handler)
    # flush=True：作为后台守护进程跑时 stdout 是文件、不是 tty →
    # Python 会整块缓冲，日志文件长时间是空的，出问题无从查起（2026-10-01 实测踩到）。
    print(f"▮ netdev-ui 已启动  http://{a.host}:{a.port}   PID {os.getpid()}   "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  tmux={TMUX or '未找到'}  设备窗格会话={TMUX_SESSION}  静态目录={STATIC}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在收尾…")
    finally:
        with SESS_LOCK:
            for s in list(SESSIONS.values()):
                s.close()
            SESSIONS.clear()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
