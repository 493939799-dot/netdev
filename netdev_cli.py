#!/usr/bin/env python3
"""netdev —— 网络设备调试通道（CLI 形态）。

用法速查：
  netdev list
  netdev run <设备> "命令" ["命令2" ...]
  netdev apply <设备> --cmd "..." --cmd "..." [--yes] [--no-save]
  netdev save <设备> [--yes]
  netdev backup <设备>
  netdev diff <文件A> <文件B>
  netdev ping <设备> <目标> [--source Vlanif110] [--count 5]
  netdev shell <设备>
  netdev watch [设备] [--lines 40] [--no-follow]
  netdev serial-discover
  netdev onboard <设备> [--yes]
  netdev selftest
  netdev mcp
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lib import approval, colorize, creds, engine, gates, mirror, paths as _P, vrp_commands  # noqa: E402

# ROOT：优先 NETDEV_ROOT 环境变量，其次从脚本自身位置推导（支持任意安装路径 / --prefix）。
# 历史：曾硬编码为 ~/netops，导致 --prefix 换目录后配置/备份/日志全部错位。
# ROOT 统一由 lib/paths.py 解析（2026-10-03）：此前本文件与 lib/ 各写一遍，
# 装到 ~/netops 之外就会读错文件（策略、审计流水会静默写到别处）。
ROOT = _P.ROOT
BACKUPS = ROOT / "backups"
BACKUPS.mkdir(parents=True, exist_ok=True)
C = mirror.C

# ── 网页服务（netdev-ui）─────────────────────────────────────────────
# 约定：主机名/端口只在这里定义一次，doctor / ui / 启动器都从这里取。
UI_HOST = os.environ.get("NETDEV_UI_HOST", "127.0.0.1")
UI_PORT = int(os.environ.get("NETDEV_UI_PORT", "8898"))
UI_BASE = f"http://{UI_HOST}:{UI_PORT}"
UI_PIDFILE = pathlib.Path(os.environ.get("NETDEV_UI_PIDFILE", str(ROOT / "logs" / "ui-service.pid")))
UI_LOGFILE = pathlib.Path(os.environ.get("NETDEV_UI_LOGFILE", str(ROOT / "logs" / "ui-service.log")))
UI_DAEMON = ROOT / "ui" / "daemonize.py"
UI_PLIST = pathlib.Path.home() / "Library/LaunchAgents/com.netdev.ui.plist"
UI_LABEL = "com.netdev.ui"



# ─────────────────────────────────────────────────────────────── 工具
def _ts():
    return _dt.datetime.now().strftime("%Y%m%d_%H%M")


def _pw(dev, allow_popup=True):
    return creds.get_password(dev, allow_popup=allow_popup)


def _tmux_holds(dev_name):
    """该设备是否正在【活着的人机同屏会话】里（串口独占 → 此时不能再直接打开）。

    2026-09-30 修：原来只看窗口名存在 —— 桥进程死了（串口拔出/设备断电）的
    死窗格也算"占用"，导致串口明明空闲却被拒（netdev run/apply 全部误伤，
    实测踩到）。现在用 pane_dead 过滤，只有活窗格才算占用。
    死窗格的清理仍走 panes 管理接口（那里看的是全量窗口列表）。
    """
    if not shutil.which("tmux"):
        return False
    r = subprocess.run([shutil.which("tmux"), "list-windows", "-t", "netops",
                        "-F", "#{window_name}\t#{pane_dead}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == dev_name and parts[1] == "0":
            return True
    return False


def _open_serial_soft(dev):
    """打开串口：失败时给人话提示（含“谁占着”与解决办法），并提醒多进程争用。"""
    try:
        s = engine.open_serial(dev)
    except SystemExit:
        raise
    except Exception as e:
        port = dev.get("port") or "auto"
        others = engine.other_port_holders(port)
        who = ("当前占用者 pid " + "、".join(others) + "（多半是 screen / NyaTerm 等其它程序）") if others else "未发现其它占用者"
        raise SystemExit(
            f"✘ 打开串口失败：{port}\n"
            f"   原因: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else 'busy'}\n"
            f"   {who}\n"
            f"   处理: 一个串口同一时刻只应有一个使用者：\n"
            f"         ① 若在用 screen → 先退出它（screen 里按 Ctrl+A 再按 K）\n"
            f"         ② 若在用 NyaTerm 的串口会话 → 关掉它\n"
            f"         ③ 查看到底谁占着：lsof {port}")
    if getattr(s, "port_warning", ""):
        print(f"{C['yel']}{s.port_warning}{C['reset']}")
    return s


def _serial_open(dev, allow_popup=True, store=False, retries=2):
    """串口：先问设备要什么（probe），再按需取凭据 —— 不猜账号。"""
    if _tmux_holds(dev["name"]):
        raise SystemExit(
            f"✘ {dev['name']} 正在人机同屏会话中（netops:{dev['name']}），串口被它占着。\n"
            f"   两种做法：① 走同屏会话：netdev screen-send {dev['name']} \"命令\"\n"
            f"             ② 先退出桥：tmux attach -t netops 然后按 Ctrl+]")
    s = _open_serial_soft(dev)
    kind = s.probe_login()
    label = {"user_pass": "要用户名 + 密码", "pass_only": "只要密码",
             "none": "无需认证", "unknown": "未识别（可能设备未上电或没回显）"}[kind]
    print(f"{C['dim']}· {dev['name']}（{s.port}）console 认证：{label}{C['reset']}")
    if kind == "unknown" and not a_force():
        s.close()
        raise SystemExit(f"✘ 串口无任何回显：确认设备已上电 / 波特率 {dev.get('baud',9600)} / 线接的是 Console 口")
    if kind in ("user_pass", "pass_only"):
        user = pw = None
        usrc = psrc = "none"
        if kind == "user_pass":
            user, usrc = creds.get_username(dev, allow_popup=allow_popup,
                                            prompt_hint="设备在问 Username")
            if not user:
                s.close()
                raise SystemExit("✘ 设备要求用户名，但没拿到（弹窗被取消？）")
        pw, psrc = creds.get_password(dev, allow_popup=allow_popup)
        if not pw:
            s.close()
            raise SystemExit("✘ 没拿到密码（弹窗被取消？）")
        ok, msg = s.login(user or "", pw, retries=retries)
        if not ok:
            s.close()
            raise SystemExit(f"✘ 串口登录失败：{msg}")
        print(f"{C['grn']}✔ {msg}{C['reset']}"
              + (f"{C['dim']}  用户名: {user}（{usrc}）｜ 密码来源: {psrc}{C['reset']}" if user else
                 f"{C['dim']}  密码来源: {psrc}{C['reset']}"))
        if store or usrc == "popup" or psrc == "popup":
            svc = creds.service_of(dev)
            if creds.store_credential(svc, user or dev.get("username", ""), pw):
                print(f"{C['dim']}· 已存入本地凭据文件：{svc}（用户名 {user or '未提供'} + 密码）{C['reset']}")
            else:
                print(f"{C['yel']}⚠ 本地凭据文件写入失败，下次仍需输入{C['reset']}")
    s.warmup()
    return s


def a_force():
    return False


def _open(dev, allow_popup=True):
    if dev.get("protocol") == "serial":
        return _serial_open(dev, allow_popup=allow_popup)
    pw, src = _pw(dev, allow_popup)
    if not pw and not dev.get("allow_no_credential"):
        raise SystemExit(f"✘ 拿不到 {dev['name']} 的密码（本地凭据文件/环境变量/弹窗均失败）")
    print(f"{C['dim']}· 凭据来源: {src}{C['reset']}")
    log_path = str(ROOT / "logs" / f"{dev['name']}_session_{_ts()}.txt")
    return engine.connect(dev, password=pw, session_log=log_path)



def _sanitize(name):
    """tmux 窗口名：只留字母数字下划线短横（点/冒号会被 tmux 当作 target 分隔符，必须去掉）"""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "target"


def resolve_target(spec):
    """解析目标：清单里的设备名，或临时 URI（不写清单，一条命令即用）。

      telnet://[user@]host[:port]     → 临时 Telnet（默认 23）
      ssh://[user@]host[:port]        → 临时 SSH（默认 22）
      serial:/dev/cu.usbserial-XXXX[@9600]  → 临时串口
    """
    if "://" not in spec and not spec.startswith("serial:"):
        devs = engine.load_devices()
        if spec in devs:
            d = dict(devs[spec])                               # 清单里的正式设备
            if d.get("protocol") == "serial" and str(d.get("baud", "")).strip().lower() in ("auto", ""):
                port = d.get("port") or engine.discover_serial_port() or ""
                if port:
                    baud, ev = engine.probe_serial_baud(port)
                    d["baud"] = baud
                    d["_baud_auto"] = baud
                    d["_baud_ev"] = ev
            return d
        # ★ 2026-09-26 加：查连接簿（随手接入的临时目标）。
        #   原来 resolve_target 只认 devices.toml 和 URI，于是连接簿里的临时目标
        #   在 CLI 跑不了（netdev run t-lab → "清单里没有 t-lab"），而 UI 侧能看到它 ——
        #   两侧能力不一致。这里补上，按 id 或 name 都认。
        try:
            from lib import conn_store as _cs
            _hit = _cs.find(spec)                 # ← 它有现成的 find(key)（按 id 或 name）
            if _hit:
                _c = _hit
                _proto = (_c.get("protocol") or "telnet").lower()
                _base = {"name": _c.get("name") or spec, "protocol": _proto,
                         "username": _c.get("username"),
                         "platform": _c.get("platform") or "huawei_vrp",
                         "backspace": _c.get("backspace"), "ad_hoc": True,
                         "_conn_id": _c.get("id")}
                if _proto == "serial":
                    _port = _c.get("device") or _c.get("port") or ""
                    try:
                        _baud = int(_c.get("baud") or 9600)
                    except Exception:
                        _baud = 9600
                    _base.update({"port": _port, "baud": _baud})
                else:
                    _base.update({"host": _c.get("host"), "port": _c.get("port")})
                return _base
        except SystemExit:
            raise
        except Exception:
            pass
        if _tmux_holds(spec):                              # 临时目标：已有同名同屏会话
            return {"name": spec, "protocol": "telnet", "ad_hoc": True}
        raise SystemExit(f"✘ 清单里没有 '{spec}'，也没有名为 '{spec}' 的同屏会话\n"
                         f"   可用：{', '.join(devs)} ｜ 或 telnet://IP[:端口] ｜ ssh://[用户@]IP ｜ serial:/dev/xxx")
    if spec.startswith("serial:"):
        rest = spec[len("serial:"):]
        dev_path, _, baud_s = rest.partition("@")
        baud_s = (baud_s or "").strip().lower()
        if baud_s in ("", "auto"):
            # 自动探测（被别的会话占用时探测会安全跳过，交给后面的“防双桥”闸门处理）
            baud_v = (engine.probe_serial_baud(dev_path)[0] or engine.serial_baud_cache_get(dev_path)
                      or 9600) if dev_path else 9600
        else:
            baud_v = int(baud_s)
        return {"name": _sanitize("serial-" + (dev_path or "auto")), "protocol": "serial",
                "port": dev_path or "auto", "baud": baud_v,
                "platform": "huawei_vrp", "ad_hoc": True}
    m = re.match(r"([A-Za-z]+)://(?:([^@/]+)@)?([^:/]+)(?::(\d+))?$", spec)
    if not m:
        raise SystemExit(f"✘ 认不出的目标写法: {spec}\n"
                         f"   可用：设备名 ｜ telnet://[用户@]IP[:端口] ｜ ssh://[用户@]IP[:端口] ｜ serial:/dev/xxx[@波特率]")
    proto, user, host, port = m.group(1).lower(), m.group(2), m.group(3), m.group(4)
    if proto not in ("telnet", "ssh"):
        raise SystemExit(f"✘ 不支持的协议: {proto}（只支持 ssh / telnet / serial:）")
    return {"name": _sanitize(f"{proto}-{host}"), "protocol": proto, "host": host,
            "port": int(port or (23 if proto == "telnet" else 22)),
            "username": user or "", "platform": "", "ad_hoc": True,
            "password_keychain": f"netdev-{proto}-{host}"}


def _risk_tag(k):
    return {"read_only": "只读", "view_nav": "视图", "write": "写", "blocked": "黑名单"}[k]


def _print_hint(cmd: str, text: str, device: str | None = None):
    """设备报了错 → 翻译成人话 + 拼写建议 + 怎么问设备。"""
    if not vrp_commands.looks_like_typo(text or ""):
        return
    col, seg = vrp_commands.parse_caret(text or "", cmd)
    if col:
        print(f"{C['yel']}   ↳ 设备在第 {col} 个字符附近报错：{cmd[:col-1]!r} 之后")
    sug = vrp_commands.hint(cmd)
    if sug:
        print(f"{C['yel']}   ↳ 你可能想输入：" + " ; ".join(sug))
    dev = device or "<设备>"
    print(f"{C['yel']}   ↳ 想看这台设备到底能敲什么：netdev hint {dev} \"{cmd.split()[0] if cmd.split() else ''} \"")
    print(f"{C['yel']}   ↳ 本地常用命令速查：netdev cmds{C['reset']}")


# ─────────────────────────────────────────────────────────────── 子命令
def cmd_list(a):
    devs = engine.load_devices()
    if getattr(a, "json", False):
        import json as _json
        out = []
        for d in devs.values():
            proto = d.get("protocol", "ssh")
            out.append({
                "name": d["name"], "protocol": proto,
                "host": d.get("host"), "port": d.get("port", 22 if proto == "ssh" else (23 if proto == "telnet" else None)),
                "baud": d.get("baud"), "username": d.get("username", ""), "platform": engine.platform_for(d),
                "esn": d.get("expected_esn"), "tags": d.get("tags", []),
                "sim": bool(d.get("sim")), "official": True,
                "address": (f"{d.get('port')}@{d.get('baud', 9600)}" if proto == "serial"
                            else f"{d.get('host')}:{d.get('port', 22 if proto == 'ssh' else 23)}"),
                "window": d["name"],
            })
        print(_json.dumps(out, ensure_ascii=False))
        return 0
    if not devs:
        print("设备清单为空：请编辑 ~/netops/devices.toml"); return
    print(f"{C['bold']}设备清单（{len(devs)} 条）{C['reset']}")
    print(f"{'名称':<16}{'类型':<10}{'通道':<34}{'平台':<18}{'凭据':<10}备注")
    for d in devs.values():
        proto = d.get("protocol", "ssh")
        if proto == "serial":
            chan = f"串口 {d.get('port') or 'auto'} @{d.get('baud', 9600)}"
        else:
            chan = f"{d.get('host')}:{d.get('port', 22 if proto == 'ssh' else 23)}（{proto.upper()}）"
        kind = "本机模拟" if d.get("sim") else ("真机·串口" if proto == "serial" else "真机·IP")
        key = "本地凭据文件" if d.get("password_keychain") else ("环境变量" if d.get("password_env") else "登录时问")
        remark = " ".join(d.get("tags", []))
        if d.get("expected_esn"):
            remark += f"  ESN尾号…{d['expected_esn'][-6:]}"
        print(f"{d['name']:<16}{kind:<10}{chan:<34}{engine.platform_for(d):<18}{key:<10}{remark}")
    print(f"{C['dim']}辨别真机：netdev identify <名称>  （连上读 ESN/型号，与清单里记录的 ESN 比对）{C['reset']}")
    live = sorted((ROOT / 'live').glob('*.log'))
    if live:
        print(f"\n{C['dim']}镜像流: " + ", ".join(f"{p.stem}({p.stat().st_size}B)" for p in live) + C['reset'])
    print(f"{C['dim']}会话留档: {ROOT/'logs'}  配置备份: {BACKUPS}{C['reset']}")


def cmd_identify(a):
    """连上设备读身份（型号/版本/ESN），并与清单里记录的 ESN 比对 —— 用于分辨“是不是我那台真机”。"""
    dev = resolve_target(a.device)
    s = _open(dev)
    m = mirror.Mirror(dev["name"])
    got = {}
    try:
        for cmd in ("display version", "display esn", "display clock"):
            m.send(cmd, "read_only")
            r = engine.run_smart(s, cmd)
            m.recv(r.text, r.ok)
            got[cmd] = r.text if r.ok else f"(失败: {r.error})"
    finally:
        s.close(); m.close()

    ver = got["display version"]
    esn = got["display esn"]
    import re as _re
    # 型号解析：优先 “Huawei <型号> Router/Switch/…”，否则取 Board Type
    model = None
    m = _re.search(r"Huawei\s+([A-Za-z0-9][\w\-]*)\s+(?:Router|Switch|Gateway|Firewall|AP)", ver)
    if m:
        model = m.group(1)
    if not model:
        m = _re.search(r"Board\s+Type\s*:\s*([A-Za-z0-9\-]+)", ver)
        model = m.group(1) if m else None
    if not model:
        m = _re.search(r"VRP \(R\) software, Version [\d.]+ \(([^)]+)\)", ver)
        model = m.group(1) if m else "未识别"
    esn_val = next((x.strip() for x in _re.findall(r"([A-Z0-9]{16,})", esn)), "未读到")
    uptime = next((ln.strip() for ln in ver.splitlines() if "uptime is" in ln), "?")
    print(f"\n{C['bold']}══ 身份卡 · {dev['name']} ══{C['reset']}")
    print(f"  通道      {dev.get('port') or dev.get('host')}")
    print(f"  型号      {model}")
    print(f"  ESN       {esn_val}")
    print(f"  运行时长  {uptime.replace('uptime is', 'uptime is') if uptime != '?' else '?'}")
    print(f"  时钟      {got['display clock'].strip().splitlines()[0] if got['display clock'].strip() else '?'}")
    exp = dev.get("expected_esn")
    if exp:
        if exp.replace(" ", "") in esn.replace(" ", ""):
            print(f"{C['grn']}  比对      ✔ ESN 与清单记录一致 —— 这就是你要的那台真机{C['reset']}")
        else:
            print(f"{C['red']}  比对      ✘ 与记录不符：清单记录 …{exp[-8:]}，实读 …{esn_val[-8:]}\n"
                  f"           → 这是一台**别的设备**（或 ESN 记录有误）{C['reset']}")
    else:
        print(f"{C['yel']}  比对      清单里没记 ESN。可把这行加进 devices.toml：\n"
              f"              expected_esn = \"{esn_val}\"   # {dev['name']} 于今天实测{C['reset']}")
    return 0


def cmd_run(a):
    dev = resolve_target(a.device)

    # ★ 2026-09-26 加：run 也走同屏（与 apply 一致）。
    #   问题：原来 run 只有直连一条路，于是 AI 通过 run 做的只读操作
    #        在用户的屏上【完全看不到】—— 而"人机同屏"是这个工具的核心承诺。
    #        apply 早就有 _snap_window 判断（有窗格走同屏），run 漏了。
    #   注意：串口设备走同屏顺带解决"只有一个使用者"的问题（直连会被独占锁拒）。
    win = _snap_window(dev)
    if win:
        return _run_via_pane(dev, win, a)

    s = _open(dev)
    m = mirror.Mirror(dev["name"])
    ok_all = True
    try:
        for cmd in a.commands:
            k = gates.classify(cmd)
            if k != gates.READ_ONLY:
                print(f"{C['red']}✘ 已拒绝（{_risk_tag(k)}）: {cmd}{C['reset']}")
                sug = vrp_commands.hint(cmd)
                if k == gates.WRITE and sug and all(gates.classify(s) == gates.READ_ONLY for s in sug):
                    print(f"{C['yel']}   ↳ 这看起来像只读命令的拼写错误，建议重跑：" + " ; ".join(sug) + C['reset'])
                else:
                    print(f"{C['dim']}  提示：写操作请用 `netdev apply`（会先备份并要求确认）{C['reset']}")
                if k == gates.BLOCKED:
                    m.send(cmd, "blocked"); m.recv("黑名单命令，拒绝执行", ok=False)
                ok_all = False
                continue
            m.send(cmd, k)
            r = engine.run_smart(s, cmd)
            m.recv(r.text, r.ok)
            print(f"{C['blu']}▷ {cmd}{C['reset']}")
            print(r.text if r.text else "(无回显)")
            if not r.ok:
                ok_all = False
                print(f"{C['red']}✘ {r.error}{C['reset']}")
                _print_hint(cmd, r.text, dev["name"])
            print()
    finally:
        s.close(); m.close()
    print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
    return 0 if ok_all else 1


def _do_backup(s, m, name):
    out = {}
    for kind, cmd, suffix in (("运行配置", "display current-configuration", "run"),
                              ("flash 配置", "display saved-configuration", "flash")):
        m.send(cmd, "read_only")
        r = s.run(cmd, timeout=60) if isinstance(s, engine.NetmikoSession) else s.run(cmd, timeout=60)
        m.recv(r.text, r.ok)
        p = BACKUPS / f"{name}_{suffix}_{_ts()}.cfg"
        if r.ok and r.text.strip():
            p.write_text(r.text + "\n", encoding="utf-8")
            out[kind] = p
            print(f"{C['grn']}✔ {kind} → {_P.rel_to_home(p)} "
                  f"({p.stat().st_size} B){C['reset']}")
        else:
            print(f"{C['red']}✘ {kind} 备份失败: {r.error}{C['reset']}")
    return out


def cmd_backup(a):
    dev = resolve_target(a.device)
    s = _open(dev); m = mirror.Mirror(dev["name"])
    try:
        _do_backup(s, m, dev["name"])
    finally:
        s.close(); m.close()
    return 0


def cmd_apply(a):
    dev = resolve_target(a.device)
    lines = list(a.cmd or [])
    if a.file:
        lines += [ln.strip() for ln in pathlib.Path(a.file).read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise SystemExit("✘ 没有要下发的命令（--cmd / --file）")
    plan = engine.Plan(lines=lines, save=not a.no_save)
    g = gates.classify_plan(plan.lines)

    # 变更卡
    print(f"{C['bold']}⚠  即将下发变更到 {dev['name']}{C['reset']}")
    print(f"{'#':<4}{'命令':<52}{'风险':<8}回滚")
    rollback = []
    for i, c in enumerate(plan.lines, 1):
        k = gates.classify(c)
        rb = ""
        m_batch = re.match(r"^vlan\s+batch\s+([\d ]+)$", c, re.I)
        if m_batch:                             # `vlan batch 999 998` 的回滚是 `undo vlan 999 998`（不是 undo vlan batch …）
            rb = "undo vlan " + " ".join(m_batch.group(1).split())
        elif c.startswith("rule "):
            rb = f"undo {c.split()[0]} {c.split()[1]}"
        elif c.startswith(("acl ", "vlan ")):
            rb = f"undo {c}"
        rollback.append(rb)
        print(f"{i:<4}{c:<52}{_risk_tag(k):<8}{rb}")

    if g["blocked"]:
        print(f"\n{C['red']}✘ 计划中含黑名单命令，拒绝执行：{g['blocked']}{C['reset']}")
        return 2

    if not a.yes:
        if sys.stdin.isatty():
            if input(f"\n确认执行？(yes/否) ").strip().lower() not in ("yes", "y", "是"):
                print("已取消。"); return 3
        else:
            print(f"{C['red']}✘ 需要显式确认：加 --yes{C['reset']}"); return 3

    if not approval.ask(dev["name"], plan.lines, kind="apply"):
        print(f"{C['red']}✘ 人审未通过（拒绝/超时/无 GUI）——一条都没下发{C['reset']}")
        print(f"{C['dim']}  审批留档：~/netops/logs/approvals.log{C['reset']}")
        return 3
    win = _snap_window(dev)
    if win:
        return _apply_via_pane(dev, win, plan, a, rollback)   # 人机同屏：每条都打在你看得见的屏上
    s = _open(dev); m = mirror.Mirror(dev["name"])
    save_ok = None
    try:
        print(f"\n{C['bold']}① 强制备份{C['reset']}")
        bk = _do_backup(s, m, dev["name"])
        print(f"\n{C['bold']}② 逐条下发{C['reset']}")
        for c in plan.lines:
            m.send(c, gates.classify(c))
        r = s.push(plan.lines)
        m.recv(r.text, r.ok)
        print(r.text)
        if not r.ok:
            print(f"{C['red']}✘ 下发失败：{r.error}{C['reset']}")
            for c in plan.lines:
                _print_hint(c, r.text, dev["name"])
            todo = [x for x in rollback if x]
            if todo and a.rollback:
                print(f"{C['yel']}↩ 回滚已下发部分…{C['reset']}")
                rr = s.push(todo)
                print(rr.text)
            else:
                print(f"{C['yel']}⚠ 未自动回滚（可加 --rollback 开启；建议先人工确认）{C['reset']}")
            return 4
        print(f"{C['grn']}✔ 下发完成，回显无 Error{C['reset']}")
        print(f"\n{C['bold']}③ 校验{C['reset']}")
        for c in a.verify or []:
            m.send(c, "read_only")
            vr = s.run(c)
            m.recv(vr.text, vr.ok)
            print(f"{C['blu']}▷ {c}{C['reset']}\n{vr.text}\n")
        if plan.save:
            if a.yes or (sys.stdin.isatty() and input("配置已生效，是否 save 落盘？(yes/否) ").strip().lower() in ("yes", "y", "是")):
                m.send("save vrpcfg.zip", "write")
                sv = s.save() if hasattr(s, "save") else engine.Result("save", False, "", 0, "该通道不支持 save")
                m.recv(sv.text, sv.ok)
                print(f"{C['grn'] if sv.ok else C['red']}{'✔' if sv.ok else '✘'} save{' 完成' if sv.ok else ' 失败'}{C['reset']}")
                print(sv.text)
                save_ok = sv.ok
            else:
                print(f"{C['yel']}⚠ 未落盘：改动重启会丢失，记得稍后 `netdev save {dev['name']}`{C['reset']}")
    finally:
        s.close(); m.close()
    print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
    return 0 if save_ok is not False else 5


def cmd_save(a):
    dev = resolve_target(a.device)
    if not a.yes:
        if sys.stdin.isatty():
            if input(f"确认对 {dev['name']} 执行 save？(yes/否) ").strip().lower() not in ("yes", "y", "是"):
                print("已取消。"); return 3
        else:
            print(f"{C['red']}✘ 需要显式确认：加 --yes{C['reset']}"); return 3
    if not approval.ask(dev["name"], ["save (写入 flash)"], kind="save"):
        print(f"{C['red']}✘ 人审未通过——未执行 save{C['reset']}")
        return 3
    win = _snap_window(dev)
    if win:                                  # 有同屏会话 → save 也在屏上做（你能看见）
        m = mirror.Mirror(dev["name"])
        print(f"{C['dim']}· 走同屏会话 {win}（save 过程你能看见）{C['reset']}")
        ok, out = _pane_save(win, m)
        print(out[-800:])
        print(f"{C['grn'] if ok else C['red']}{'✔ save 完成（屏上可见）' if ok else '✘ save 未确认成功（看屏上回显）'}{C['reset']}")
        print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
        return 0 if ok else 1
    s = _open(dev); m = mirror.Mirror(dev["name"])
    try:
        m.send("save vrpcfg.zip", "write")
        r = s.save()
        m.recv(r.text, r.ok)
        print(r.text)
        print(f"{C['grn'] if r.ok else C['red']}{'✔ save 完成' if r.ok else '✘ save 失败'}{C['reset']}")
        return 0 if r.ok else 1
    finally:
        s.close(); m.close()


def cmd_diff(a):
    ta = pathlib.Path(a.file_a).read_text(encoding="utf-8", errors="replace")
    tb = pathlib.Path(a.file_b).read_text(encoding="utf-8", errors="replace")
    d = engine.diff_configs(ta, tb, a.file_a, a.file_b)
    if not d.strip():
        print("两份配置完全一致（无差异）"); return 0
    for ln in d.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            print(f"{C['grn']}{ln}{C['reset']}")
        elif ln.startswith("-") and not ln.startswith("---"):
            print(f"{C['red']}{ln}{C['reset']}")
        elif ln.startswith("@@"):
            print(f"{C['cyn']}{ln}{C['reset']}")
        else:
            print(ln)
    return 0


def cmd_ping(a):
    dev = resolve_target(a.device)
    cmd = f"ping -c {a.count} {a.target}" if not a.source else f"ping -a {a.source} -c {a.count} {a.target}"
    return cmd_run(argparse.Namespace(device=a.device, commands=[cmd]))


def _tmux(*args, check=False, timeout=8):
    t = shutil.which("tmux")
    if not t:
        raise SystemExit("未安装 tmux（brew install tmux）")
    try:
        return subprocess.run([t, *args], check=check, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        class _R:                      # 会话/窗格已死时 tmux 可能阻塞：绝不让它挂死整个命令
            returncode = 124
            stdout = ""
            stderr = "tmux 调用超时（会话可能已死）"
        return _R()


def _tmux_target(dev_name):
    return f"netops:{dev_name}"


def _shell_inner(dev):
    """各通道的交互命令。串口→raw 桥（无前缀键、全程留档）；SSH→系统 ssh。"""
    if dev.get("protocol") == "serial":
        port = dev.get("port") or engine.discover_serial_port() or ""
        if not port:
            raise SystemExit("没找到串口设备")
        logf = ROOT / "live" / f"{dev['name']}.screen.log"
        bs = dev.get("backspace", "auto")   # auto | bs | del | pass
        return (f'exec env SERIAL_BACKSPACE={bs or "auto"} SERIAL_DEVICE={dev["name"]} '
                f'NETDEV_DEVICE={shlex.quote(dev["name"])} '
                f'{sys.executable} {ROOT}/tools/serial_bridge.py '
                f'{port} {dev.get("baud", 9600)} {logf}')
    host = dev["host"]
    user = dev.get("username", "admin")
    port = dev.get("port", 22 if dev.get("protocol", "ssh") == "ssh" else 23)
    logf = ROOT / "live" / f"{dev['name']}.screen.log"
    if dev.get("protocol") == "telnet":
        # ⚠ 2026-09-27 修：这里原来【只传了 NETDEV_BACKSPACE，漏了设备名】。
        #   后果：telnet 桥拿不到设备名，只能从 argv[1]（IP）猜，
        #   而凭据是按设备名存的 → 连接簿里明明有用户名密码，却仍停在登录界面；
        #   另一个后果是 telnet 桥按 IP 找设备，密码也不落日志等判断全部失准。
        #   现在与 SSH 分支保持完全一致。
        return (f'exec env NETDEV_BACKSPACE={dev.get("backspace") or "bs"} '
                f'NETDEV_DEVICE={shlex.quote(dev["name"])} '
                f'{sys.executable} {ROOT}/tools/telnet_bridge.py {host} {port} {logf}')
    # SSH 也走桥：退格适配（0x7F↔0x08）/ 屏幕留档 / IP 高亮 / 窗口尺寸透传，
    # 与串口·telnet 完全一致。直连 `exec ssh` 没有退格适配，浏览器发 0x7F 设备不认。
    ssh_argv = ["ssh", "-o", f"KexAlgorithms=+{engine.LEGACY_KEX}"]
    # 设备控制台要一条“专属”连接：不要复用 ~/.ssh/config 里的 ControlMaster。
    # （复用会让窗格与别的 ssh 抢同一条设备会话；重启窗格时还会撞上残留 master socket，
    #   表现为 "mux_client_request_session: read from master failed" + 登录被断）
    ssh_argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if dev.get("sim"):
        # 本机模拟器：每次启动都换主机密钥，放宽检查（真机绝不加）
        ssh_argv += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    ssh_argv += ["-p", str(port), f"{user}@{host}"]
    quoted = " ".join(shlex.quote(x) for x in ssh_argv)
    return (f'exec env NETDEV_BACKSPACE={dev.get("backspace") or "auto"} NETDEV_DEVICE={dev["name"]} '
            f'{sys.executable} {ROOT}/tools/ssh_bridge.py {logf} -- {quoted}')


def _connect_lock():
    """接入临界区锁：网页点一下 + AI 同时接入时，避免两个进程同时 new-window 产生重复窗口。
    flock 随进程退出/文件关闭自动释放，不会有残留死锁。"""
    import fcntl
    lockdir = _P.state_dir()
    lockdir.mkdir(parents=True, exist_ok=True)
    fh = open(lockdir / "connect.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def _connect_unlock(fh):
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    except Exception:
        pass


def _snap_register(d, meta=None):
    """登记索引 + 导出交付级副本到 ~/Desktop/workbuddy。"""
    from lib import snapshot as S
    import json as _json
    if meta is None:
        try:
            meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta["id"] = d.name
    run = d / "running.cfg"
    sha = S.sha256_file(run) if run.exists() else ""
    S.index_record(meta, sha, "ok")
    if "恢复前自动" not in str(meta.get("tag", "")):        # 自动快照不往桌面堆
        dst = S.export_record_to_workbuddy(meta, run)
        if dst:
            print(f"  副本 : {_P.rel_to_home(dst)}（防误删的交付级备份）")


def _pane_pids():
    """当前所有 tmux 窗格进程 pid（用于区分"活会话"与"孤儿桥"）。"""
    if not shutil.which("tmux"):
        return set()
    try:
        out = _tmux("list-panes", "-a", "-F", "#{pane_pid}").stdout.split()
        return {x.strip() for x in out if x.strip()}
    except Exception:
        return set()


def _stale_serial_bridges(port):
    """占用该串口、但已不属于任何 tmux 窗格的"孤儿桥" pid（我们自己遗留的进程）。"""
    holders = engine.other_port_holders(port)
    if not holders:
        return []
    panes = _pane_pids()
    stale = []
    for h in holders:
        if h in panes:
            continue
        try:
            cmd = subprocess.run(["/bin/ps", "-p", h, "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cmd = ""
        if "serial_bridge.py" in cmd and port in cmd:
            stale.append(h)
    return stale


# ─────────────────────────────────────────────────────────────────────────────
#  配置快照：备份 / 对比 / 恢复（接客户设备的标准动作）
#    netdev snap save <设备> [--tag 客户] [--note 备注]   存快照
#    netdev snap list [设备]                              列快照
#    netdev snap import <cfg文件> --device X [--tag Y]    导入旧备份当快照
#    netdev snap diff <设备> [--from 快照]                差异预览（不动设备）
#    netdev snap restore <设备> [--from 快照] [--apply] [--yes] [--undo-extra]
# ─────────────────────────────────────────────────────────────────────────────
PROMPT_RE = re.compile(r"^\s*(<[^>]+>|\[[^\]]+\])\s*$")
MORE_RE = re.compile(r"-{2,}\s*More\s*-{2,}")


class PaneBusy(Exception):
    """同屏会话没能在超时内回到提示符（多半是设备还停在 `---- More ----` 分页）。"""


def _snap_window(dev):
    """找到能读这台设备的同屏窗口（优先设备名窗口，其次串口持有者窗口）；死窗口不算。"""
    if _tmux_holds(dev["name"]) and _pane_alive(dev["name"]):
        return dev["name"]
    if dev.get("protocol") == "serial":
        h = _serial_holder_window(dev.get("port") or "")
        if h and _pane_alive(h):
            return h
    return None


def _pane_alive(win):
    """同屏窗口是否还活着（桥进程还在）。死会话要快速失败，别挂在等提示符上。"""
    try:
        out = _tmux("list-panes", "-t", f"netops:{win}", "-F", "#{pane_dead} #{pane_current_command}").stdout.strip()
    except Exception:
        return False
    return bool(out) and out.split()[0] == "0"


def _pane_tail(win, n=12):
    """同屏窗格最后 n 行（含回滚）。"""
    return _tmux("capture-pane", "-p", "-J", "-t", f"netops:{win}", "-S", f"-{n}").stdout


def _pane_paging(win):
    """窗格此刻是否停在设备分页提示（`---- More ----`）上等按键。"""
    lines = [x for x in _pane_tail(win, 3).splitlines() if x.strip()]
    return bool(lines) and bool(MORE_RE.search(lines[-1]))


def _pager_drain(win, max_pages=120, gap=0.15):
    """把没翻完的分页翻到底（按空格，等同人按空格翻页），让窗格回到可用提示符。

    不做这件事的后果：设备停在 More 上，后面发的命令会被分页吃掉，
    抓到的配置截断，人的同屏会话也像“卡死”一样。
    """
    tgt = f"netops:{win}"
    n = 0
    while n < max_pages and _pane_paging(win):
        _tmux("send-keys", "-t", tgt, "Space")
        n += 1
        time.sleep(gap)
    return n


def _session_run(win, cmd, timeout=90, quiet=False):
    """在同屏会话里跑一条命令并取回输出（自动应答 More 分页）。会话已死会立刻报错。

    分页用 **空格**（一屏），不是 Enter（一行）——大配置下差 30 倍以上，
    以前用 Enter 翻页会把超时耗光，留下截断的配置和一个卡在 More 的窗格。
    """
    tgt = f"netops:{win}"
    if not _pane_alive(win):
        raise SystemExit(f"✘ 同屏会话「{win}」里的进程已退出（串口被拔 / 设备断开？）\n"
                         f"   重新接入：netdev shell {win}（或网页里点接入命令）\n"
                         f"   查可用串口：netdev serial-discover")
    if _pager_drain(win):                    # 上一轮没翻完的分页先翻完
        _tmux("send-keys", "-t", tgt, "C-c")  # 清掉可能残留的半行输入
        time.sleep(0.2)
    _tmux("send-keys", "-t", tgt, "C-u")      # 清空当前行：终端能力应答碎片（;2c / 0;276;0c）会漏进来当“前缀”
    time.sleep(0.15)
    _tmux("send-keys", "-t", tgt, "-l", cmd)
    _tmux("send-keys", "-t", tgt, "Enter")
    t0 = time.time()
    pages = 0
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        tail = _pane_tail(win, 12)
        if MORE_RE.search(tail):
            _tmux("send-keys", "-t", tgt, "Space")   # 翻页：空格 = 一屏
            pages += 1
            continue
        lines = [x for x in tail.splitlines() if x.strip()]
        if lines and PROMPT_RE.match(lines[-1]) and time.time() - t0 > 1.0:
            break
    else:
        left = _pager_drain(win)              # 超时了也别把窗格留在分页里
        raise PaneBusy(f"设备在 {timeout}s 内没回到提示符（已翻 {pages} 页"
                       + (f"，收尾又翻了 {left} 页" if left else "") + "）")
    full = _tmux("capture-pane", "-p", "-J", "-t", tgt, "-S", "-40000").stdout
    if not quiet:
        extra = f"，翻页 {pages} 次" if pages else ""
        print(f"  {C['dim']}· {cmd}  取自会话 {win}（{time.time()-t0:.1f}s{extra}）{C['reset']}")
    return full


def _after_echo(full, cmd):
    """截出「命令回显之后」的原文（不修剪）。"""
    lines = full.splitlines()
    start = 0
    for i, l in enumerate(lines):
        if l.strip().endswith(cmd.split("|")[0].strip()) or l.strip() == cmd.strip():
            start = i + 1
    return "\n".join(lines[start:])


def _trim_tail(text):
    """去掉正文首尾的噪声：空白行 / 提示符 / 分页标记 / return。

    头部噪声来源：设备重画分页那行时用的是光标回退（ESC[42D），窗格重建视图时
    会把上一行内容的残尾（如 `[V200R010C10SPC700]`，看着就像个视图提示符）
    留在回显后面；这种行不属于配置，夹在快照里会让差异对比多出幻影行。
    """
    body = [_l.rstrip() for _l in text.splitlines()]
    while body and (not body[0].strip() or PROMPT_RE.match(body[0])
                    or MORE_RE.search(body[0]) or body[0].strip() in ("return", "end")):
        body.pop(0)
    while body and (not body[-1].strip() or PROMPT_RE.match(body[-1])
                    or MORE_RE.search(body[-1]) or body[-1].strip() in ("return", "end")):
        body.pop()
    return "\n".join(body)


def _extract_cfg(full, cmd):
    """从屏文本里截出命令回显之后的配置正文。"""
    return _trim_tail(_after_echo(full, cmd))


def _looks_complete(cmd, raw_after):
    """配置类输出是否抓全了。

    判据：命令回显之后能看到 VRP 的收尾行 `return`。只有半截配置比没有配置更危险——
    它会被当成“客户原始状态”存下来。
    """
    if "configuration" not in cmd:
        return True
    lines = [l.strip() for l in raw_after.splitlines()]
    if not any(l in ("return", "end") for l in lines):
        return False
    # 收尾行只能出现在最后一处；正文里再夹一个 return = 窗格历史被分页重画搞成了两份
    return sum(1 for l in lines if l in ("return", "end")) == 1


def _pane_screen_length(win, value):
    """会话级分页设置（`temporary` 只影响当前会话，不改设备配置、不需审批）。

    为什么要关分页：设备分页时用光标回退重画 `---- More ----`，tmux 重建窗格历史会把
    同一段配置记成两份/残片；关掉分页后配置一次吐完，抓出来才是干净一份。
    netmiko 每次连接也是这么做的。
    """
    tgt = f"netops:{win}"
    _tmux("send-keys", "-t", tgt, "-l", f"screen-length {value} temporary")
    _tmux("send-keys", "-t", tgt, "Enter")
    time.sleep(0.6)


def _port_busy(port: str) -> bool:
    """串口是否被别的进程占着（排除自己）。"""
    if not port:
        return False
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-t", port], capture_output=True, text=True, timeout=5)
        others = [x for x in (r.stdout or "").split() if x != str(os.getpid())]
        return bool(others)
    except Exception:
        return False


def _cfg_sane(cur: str, ref: str = "") -> tuple[bool, str]:
    """抓到/读到的"当前配置"是否可信。

    2026-09-26 实测：restore 时抓到过不完整的当前配置（比快照少一大截），
    结果把设备本来就有的 user-interface / wlan ac 等全判成"缺失"，生成了十几步
    "补回"计划（幸好只是重复下发，无实际损害；若方向相反就会误删）。

    ⚠ 注意：传进来的 cur 已经过 _trim_tail（收尾 return 已被去掉），
      所以【不能】拿"有没有 return"当判据 —— 那会永远为假（曾因此误报成片失败）。
      改用两个真正可行的判据：
        ① 内容非空且至少比噪声多（>20 行）
        ② 与参照快照相比，行数不能少太多（少了就是抓漏了）
    """
    lines = [l for l in (cur or "").splitlines() if l.strip() and l.strip() != "#"]
    if not lines:
        return False, "当前配置为空"
    if len(lines) < 20:
        return False, f"当前配置只有 {len(lines)} 行（明显偏少，可能没抓全）"
    if ref:
        ref_n = len([l for l in ref.splitlines() if l.strip() and l.strip() != "#"])
        if ref_n and len(lines) < ref_n * 0.6:
            return False, (f"当前配置只有 {len(lines)} 行，而快照有 {ref_n} 行"
                           f"（不足 60%）—— 几乎肯定是抓漏了")
    return True, ""


def _capture_with_handoff(dev, cmd, win, timeout=90):
    """让同屏桥【临时让位】→ 直连抓 → 抓完自动把桥恢复。

    为什么不用读屏（原方案）：
        窗格是长期复用的，屏上堆着历史回显（以前跑过的 display、配置片段…）。
        实测：读屏抓到 182 行，直连抓到 246 行 —— 少了 64 行还混入残留。
        而快照是"客户原始状态"的依据，掺假的快照会让后续 diff/恢复全部错乱
        （会生成 undo wlan ac xxx 这种删正常配置的计划）。

    为什么"让位"而不是"默认直连"：
        串口物理独占，桥开着时直连根本打不开（multiple access）。
        所以必须让桥先退出，抓完再把它建回来，用户回来时屏还在。
    """
    name = dev["name"]
    proto = dev.get("protocol", "ssh")
    port = dev.get("port") or ""
    # 恢复时优先用【设备名】：这样窗格名与原来一致，用户那边的终端不会"改名"
    # （用 serial:/dev/... 这种 URI 会让 netdev 生成 serial--dev-cu-xxx 这种窗格名）
    target = name if name else (f"serial:{port}@{dev.get('baud') or 'auto'}" if proto == "serial" else name)

    # 关掉桥窗格
    _tmux("kill-window", "-t", f"netops:{win}")
    # 等串口释放（最多 15s）。宽松判定：一旦探测为空就继续；
    # 超时也往下走（直连自己会报"被占用"，比在这里干等更好定位）。
    for _ in range(38):
        if not _port_busy(port):
            break
        time.sleep(0.4)

    try:
        s = _open(dev, allow_popup=False)    # 此时没人占串口，直连能进
        try:
            r = s.run(cmd, timeout=timeout)
            return _trim_tail(r.text or ""), "直连（桥已暂让位）"
        finally:
            s.close()
    finally:
        # 把桥建回来（用户回来时那块屏还在）
        try:
            # 用 --restart：窗格若还留有"残壳"（进程死了但窗格在），普通 shell 会
            # 直接复用残壳而不重建（实测踩到）。--restart 强制重启里面的命令。
            subprocess.Popen([str(ROOT / "netdev"), "shell", target, "--restart"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            # ★ 等桥【真的能用】，而不是只等"串口被占"。
            #   踩过的坑：只看 lsof 就返回，那时桥刚打开串口、还没读通设备，
            #   紧接着的第二次让位就撞上“半开”状态 → diff 紧跟 save 时必错。
            #   判据改成：往窗格发个回车，屏上能出现提示符 = 人机同屏已就绪。
            time.sleep(2.0)                       # 先给它起来的窗口
            for _ in range(30):                   # 最多再等 12s
                try:
                    _tmux("send-keys", "-t", f"netops:{name}", "Enter")
                    time.sleep(0.9)
                    cap = _tmux("capture-pane", "-p", "-J", "-t", f"netops:{name}").stdout or ""
                    tail = [x for x in cap.splitlines() if x.strip()][-1:] or [""]
                    if PROMPT_RE.search(tail[0]) or tail[0].strip().endswith((">", "#", "]")):
                        break                     # 提示符回来了 → 真的就绪
                except Exception:
                    pass
                time.sleep(0.4)
        except Exception:
            pass


def _capture_via_screen(dev, cmd, win, timeout=90):
    """在同屏会话里抓配置 —— 清历史 → 发命令 → 读屏 → 切回显。

    这是最终方案（2026-09-26 定稿）。为什么是它：
      * 读屏柘染历史残渣（曾抓到 182 行 vs 真实 239 行）—— 但根因是“屏上有历史”，
        而不是“读屏”本身错。那把历史清掉就行。
      * “让桥暂让位→直连抓”虽然拿得到干净配置，但让位期间【屏没了】，
        用户看不到命令与输出 —— 这就砍掉了人机同屏这个立身之本。
      * 本方案：tmux clear-history 清掉滚动历史 → 发命令（用户实时看得见）
        → 读屏 → _after_echo 切掉命令回显之前的一切。
    实测：切完得到 239 行，与直连抓取完全一致，且全程同屏可见。
    """
    tgt = f"netops:{win}"
    try:
        _tmux("clear-history", "-t", tgt)      # 清滚动历史
        # 再把【可见屏】推空：clear-history 只管滚动历史，当前屏幕上的内容还在，
        # 会和本次命令的输出一起被读进来（实测混进过 display interface 的统计行）。
        for _ in range(60):
            _tmux("send-keys", "-t", tgt, "Enter")
    except Exception:
        pass
    time.sleep(0.6)
    raw = _session_run(win, cmd, timeout)
    body = _after_echo(raw, cmd)               # 切掉回显之前（含历史残渣）
    # ★ 再切掉收尾 return 之后的东西：配置天然以 return 结束，
    #   后面再出现的内容必然是别的命令的回显/残留（曾经混进过接口统计）。
    _ls = body.splitlines()
    for _i, _l in enumerate(_ls):
        if _l.strip() in ("return", "end"):
            body = "\n".join(_ls[:_i + 1])
            break
    return _trim_tail(body), f"同屏会话 {win}（已清历史）"


def _snap_capture(dev, cmd, timeout=90, allow_fallback=True):
    """优先走同屏会话（串口被占时也能读），否则走 netmiko。

    从同屏会话抓配置时若发现不完整（分页没走完），非串口设备自动改用直连重抓，
    绝不把截断的配置当快照存下来。
    """
    win = _snap_window(dev)
    if win:
        # ★ 2026-09-26 定稿：配置类命令【走同屏 + 先清历史】，不再让桥让位。
        #   让位方案会把屏关掉（用户看不到命令），违背人机同屏的初衷。
        #   清掉历史后再读屏，既干净又全程可见。
        if "configuration" in cmd:
            print(f"  {C['dim']}· 同屏会话「{win}」：先清历史再抓（你看得见命令）…{C['reset']}")
            try:
                got, _src2 = _capture_via_screen(dev, cmd, win, timeout)
                if got and len(got.splitlines()) > 20:
                    return got, _src2
                print(f"  {C['yel']}· 同屏抓取偏少（{len(got.splitlines())} 行）→ 走直连重抓{C['reset']}")
            except SystemExit:
                raise
            except Exception as e:
                print(f"  {C['yel']}· 同屏抓取失败（{type(e).__name__}: {e}）→ 走直连{C['reset']}")
        big = False
        try:
            raw = _after_echo(_session_run(win, cmd, timeout), cmd)
            if not _looks_complete(cmd, raw):
                n = len(_trim_tail(raw).splitlines())
                raise PaneBusy(f"回显之后 {n} 行但收尾不对")
            return _trim_tail(raw), f"会话 {win}"
        except PaneBusy as e:
            if not allow_fallback or dev.get("protocol") == "serial":
                raise SystemExit(f"✘ 从会话「{win}」抓「{cmd}」失败：{e}")
            print(f"  {C['yel']}· 同屏会话抓取不完整（{e}）→ 改用直连方式重抓{C['reset']}")
    # 直连抓取：加重试。
    # 为什么需要：串口读大配置靠"读到提示符/翻页"，时序上有概率漏页
    # （实测 netdev run 连抓 5 次有 1 次只回 94 行且无收尾 return）。
    # 策略：最多 3 次，取"行数最多且完整"的那份。
    best, best_src = "", "netmiko"
    s = _open(dev, allow_popup=False)
    try:
        for attempt in range(3):
            try:
                r = s.run(cmd, timeout=timeout)
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  {C['yel']}· 第 {attempt+1} 次直连抓取失败（{type(e).__name__}）→ 重试{C['reset']}")
                time.sleep(1.0)
                continue
            body = _trim_tail(r.text or "")
            has_tail = any(l.strip() in ("return", "end") for l in (r.text or "").splitlines())
            n = len(body.splitlines())
            if n > len(best.splitlines()):
                best = body
            # ★ 完整性判定统一走 _looks_complete：配置类命令要求 return/end 收尾；
            #   非配置命令（display version / display clock 等）有输出即算完整。
            #   原实现写死 "has_tail and n>=100" —— version(13行)/clock(3行) 永远
            #   不满足 → 每次白重试 3 遍（2026-10-01 实测，snap save 因此变慢）。
            if _looks_complete(cmd, r.text or "") and n > 0:
                return body, best_src
            if attempt < 2:
                print(f"  {C['yel']}· 第 {attempt+1} 次抓取不完整（{n} 行，收尾={has_tail}）→ 重试{C['reset']}")
                time.sleep(1.2)
        return best, best_src + "（重试后取最完整）"
    finally:
        s.close()


def _snap_meta(dev, tag, note, source):
    meta = {"device": dev["name"], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tag": tag, "note": note, "source": source, "protocol": dev.get("protocol", "")}
    try:
        ver, _ = _snap_capture(dev, "display version", 25)
        meta["version"] = " | ".join(x.strip() for x in ver.splitlines()[:3] if x.strip())
        clock, _ = _snap_capture(dev, "display clock", 20)
        meta["device_clock"] = " ".join(x.strip() for x in clock.splitlines()[:2] if x.strip())
    except Exception as e:
        meta["meta_error"] = f"{type(e).__name__}: {e}"
    return meta


def cmd_snap(a):
    from lib import snapshot as S
    act = a.action

    if act == "rm":
        refs = [x for x in [getattr(a, "device", None), getattr(a, "from_", None)] if x]
        refs += list(getattr(a, "refs", []) or [])
        if not refs:
            raise SystemExit("用法：netdev snap rm <编号|ID片段> [更多…] [--yes]（先看 netdev snap list）")
        targets, miss = [], []
        for r in refs:
            snap = S.find_snapshot("", r)
            (targets if snap else miss).append(snap or r)
        print(f"{C['bold']}将删除（移入回收区，不裸删）：{len(targets)} 份{C['reset']}")
        for sn in targets:
            print(f"  #{sn.get('idx')}  {sn['id']}  {C['dim']}{sn['at']} {sn['tag']}{C['reset']}")
        if miss:
            print(f"{C['yel']}找不到：{', '.join(str(x) for x in miss)}{C['reset']}")
        if not targets:
            return 1
        if getattr(a, "purge", False):
            if not getattr(a, "yes", False):
                raise SystemExit("✘ 物理删除属破坏性操作，必须加 --yes")
            for sn in targets:
                S.sha256_file  # noqa
                import shutil as _sh
                _sh.rmtree(sn["dir"])
                S.index_record({**{k: sn.get(k) for k in ("idx", "id", "at", "tag")}, "device": sn.get("device", "")},
                               "", "purged")
                print(f"  {C['red']}已物理删除{C['reset']} #{sn.get('idx')} {sn['id']}")
            return 0
        if not getattr(a, "yes", False):
            print(f"\n{C['yel']}这是预览。真要删除（进回收区）：加 --yes{C['reset']}")
            print(f"  netdev snap rm {' '.join(str(sn.get('idx')) for sn in targets)} --yes")
            return 0
        for sn in targets:
            dst = S.move_to_trash(sn, f"removed by netdev snap rm at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            S.index_record({**{k: sn.get(k) for k in ("idx", "id", "at", "tag")}, "device": sn.get("device", "")}, "", "removed")
            print(f"  {C['grn']}✔ 已移入回收区{C['reset']} #{sn.get('idx')} {sn['id']} → {_P.rel_to_home(dst)}")
        print(f"{C['dim']}回收区：{_P.rel_to_home(S.TRASH_ROOT)}　恢复回来：netdev snap unrm　彻底删除：netdev snap purge --yes{C['reset']}")
        return 0

    if act == "trash":
        items = S.list_trash()
        if not items:
            print(f"{C['dim']}回收区是空的{S and ''}{C['reset']}")
            return 0
        print(f"{C['bold']}回收区（{len(items)} 项）· {_P.rel_to_home(S.TRASH_ROOT)}{C['reset']}")
        for d_ in items:
            print(f"  {d_.name}   {C['dim']}{d_.stat().st_size}B{C['reset']}")
        print(f"  {C['dim']}恢复回来：netdev snap unrm　彻底删除：netdev snap purge --yes{C['reset']}")
        return 0

    if act == "unrm":
        items = S.list_trash()
        if not items:
            print(f"{C['dim']}回收区是空的{C['reset']}")
            return 0
        want = (getattr(a, "from_", "") or getattr(a, "device", "") or "").strip()
        pick = next((d_ for d_ in items if want and want in d_.name), items[0] if not want else None)
        if not pick:
            print(f"{C['yel']}没找到匹配的回收项：{want}{C['reset']}")
            return 1
        import shutil as _sh
        orig = pick.name.replace("_removed-" + pick.name.split("_removed-")[-1], "")
        dst = S.SNAP_ROOT / orig
        S.SNAP_ROOT.mkdir(parents=True, exist_ok=True)
        _sh.move(str(pick), str(dst))
        _snap_register(dst)
        print(f"{C['grn']}✔ 已从回收区取回：{orig}{C['reset']}")
        return 0

    if act == "purge":
        items = S.list_trash()
        if not items:
            print(f"{C['dim']}回收区是空的，无需清理{C['reset']}")
            return 0
        if not getattr(a, "yes", False):
            print(f"{C['yel']}回收区有 {len(items)} 项，物理删除不可恢复。确认：netdev snap purge --yes{C['reset']}")
            for d_ in items[:10]:
                print(f"  {d_.name}")
            return 0
        import shutil as _sh
        for d_ in items:
            _sh.rmtree(d_)
            print(f"  {C['red']}已删除{C['reset']} {d_.name}")
        return 0

    if act == "list":
        snaps = S.list_snapshots(getattr(a, "device", "") or "")
        if getattr(a, "json", False):
            import json as _json
            print(_json.dumps([{k: v for k, v in x.items() if k != "dir"} for x in snaps], ensure_ascii=False))
            return 0
        gone = S.missing_from_index()
        if not snaps:
            print(f"{C['dim']}磁盘上还没有快照。存一份：netdev snap save <设备> --tag 客户名{C['reset']}")
            if gone:
                print(f"{C['yel']}⚠ 但索引里有 {len(gone)} 份快照已不在磁盘上（被删了？）："
                      + ", ".join(f"#{g['idx']} {g['tag'] or g['id'][:24]}" for g in gone[:6]) + f"{C['reset']}")
                print(f"{C['dim']}   可从交付级副本重新导入：netdev snap import ~/Desktop/workbuddy/<…>.txt --device <设备>{C['reset']}")
            return 0
        print(f"{C['bold']}配置快照（{len(snaps)} 份）· ~/netops/backups/snapshots/{C['reset']}")
        print(f"  {'编号':<6}{'快照 ID':<44}{'行数':>6}{'大小':>9}  标签/备注")
        for x in snaps:
            no = f"#{x.get('idx')}" if x.get("idx") else "-"
            print(f"  {no:<6}{x['id']:<44}{x['lines']:>6}{x['size']:>8}B  {x['tag']} {x['note']}"[:120])
        print(f"  {C['dim']}恢复时直接用编号：netdev snap restore <设备> --from 4  或  --from #4{C['reset']}")
        if gone:
            print(f"  {C['yel']}⚠ 索引里有 {len(gone)} 份快照在磁盘上已不存在（被删了？）："
                  + ", ".join(f"#{g['idx']}" for g in gone[:8]) + f"{C['reset']}")
            print(f"  {C['dim']}   可从交付级副本重新导入：netdev snap import ~/Desktop/workbuddy/<…>.txt --device <设备>{C['reset']}")
        return 0

    if act == "import":
        src = pathlib.Path(a.file or a.device or "").expanduser()
        if not src.exists():
            raise SystemExit(f"✘ 文件不存在: {src}")
        dev_name = (getattr(a, "dev_name", "") or
                    (a.device if a.device and not str(a.device).endswith((".cfg", ".txt")) else "")
                    or src.stem.split("_")[0])
        sid = S.snap_id(dev_name, a.tag)
        d = S.SNAP_ROOT / sid
        S.write_snapshot(d, src.read_text(encoding="utf-8"), "",
                         {"device": dev_name, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "tag": a.tag, "note": a.note or f"导入自 {src.name}",
                          "source": f"import:{src}"})
        _snap_register(d)
        print(f"{C['grn']}✔ 已导入为新快照：{sid}{C['reset']}  ({_P.rel_to_home(d)})")
        return 0

    dev = resolve_target(a.device)
    if dev.get("protocol") == "serial":
        node = dev.get("port") or ""
        if node and not pathlib.Path(node).exists():
            raise SystemExit(f"✘ 串口设备不存在：{node}\n"
                             f"   多半是 USB-Console 线被拔了/掉线了。查当前可用串口：netdev serial-discover\n"
                             f"   （换了线就用新口重建会话：netdev shell serial:<新口>@auto，或 netdev device-add 登记）")
    if act == "save":
        print(f"{C['bold']}存快照 · {dev['name']}{C['reset']}")
        print(f"{C['dim']}  抓「display current-configuration」中（大配置十几秒，别关这个终端）…{C['reset']}")
        running, src = _snap_capture(dev, "display current-configuration")
        if not running.strip():
            raise SystemExit("✘ 没读到运行配置（设备没响应？）")
        if not _looks_complete("display current-configuration",
                               running + "\nreturn"):        # _trim_tail 会去掉收尾的 return
            raise SystemExit("✘ 抓到的运行配置不完整（看不到收尾的 return）——不写快照，"
                             "避免把半截配置当“客户原始状态”。\n"
                             "   重试一次即可；仍不完整就用直连方式抓：netdev run <设备> \"display current-configuration\"")
        # ★ 二次校验：除了"有收尾 return"，还要和"已保存配置"比比行数。
        #   实测出现过只有 182 行（真实 239 行）却因为含 return 而被判"完整"存下来 ——
        #   这种半截快照正是 diff/restore 出错的源头。
        _n_run = len([l for l in running.splitlines() if l.strip() and l.strip() != "#"])
        _n_ref = 0
        try:
            _sf = S.find_snapshot(dev["name"], "")
            if _sf:
                _ref_txt = (_sf["dir"] / "running.cfg").read_text(encoding="utf-8")
                _n_ref = len([l for l in _ref_txt.splitlines() if l.strip() and l.strip() != "#"])
        except Exception:
            _n_ref = 0
        if _n_ref and _n_run < _n_ref * 0.6:
            raise SystemExit(
                f"✘ 抓到的运行配置明显偏少（{_n_run} 行，历史快照有 {_n_ref} 行）—— 不写快照。\n"
                f"   几乎肯定是抓取时被截断了（串口读大配置的已知难点）。\n"
                f"   处理：重试一次；仍偏少就 netdev shell {dev['name']} 用同屏会话抓。"
            )
        print(f"  {C['grn']}✓ 配置完整（{len(running.splitlines())} 行，已到收尾）{C['reset']}")
        saved = ""
        try:
            saved, _ = _snap_capture(dev, "display saved-configuration")
        except Exception as e:
            print(f"  {C['yel']}（已保存配置没读到：{e}，只存运行配置）{C['reset']}")
        meta = _snap_meta(dev, a.tag, a.note, src)
        sid = S.snap_id(dev["name"], a.tag)
        d = S.SNAP_ROOT / sid
        S.write_snapshot(d, running, saved, meta)
        n = len(running.splitlines())
        saved_meta = {}
        try:
            import json as _json
            saved_meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        num = f"#{saved_meta.get('idx')}" if saved_meta.get("idx") else ""
        print(f"{C['grn']}✔ 快照已存：{num} {sid}{C['reset']}")
        print(f"  目录 : {_P.rel_to_home(d)}")
        print(f"  内容 : running.cfg（{n} 行）+ {'saved.cfg + ' if saved else ''}meta.json + SHA256SUMS")
        if meta.get("version"):
            print(f"  设备 : {meta['version']}")
        _snap_register(d, saved_meta)
        print(f"  记录 : 配置记录_#{saved_meta.get('idx','-')}.txt（同目录，人可直接看/发客户）")
        print(f"  {C['dim']}恢复：netdev snap restore {dev['name']} --from {saved_meta.get('idx') or ''}{C['reset']}")
        return 0

    snap = S.find_snapshot(dev["name"], a.from_)
    if not snap:
        avail = S.list_snapshots(dev["name"])
        tip = "，".join(f"#{x['idx']}" for x in avail[:8]) if avail else "（这台设备还没有快照，先 netdev snap save）"
        raise SystemExit(f"✘ 没找到对应快照（可用编号：{tip}）\n"
                         f"   列出全部：netdev snap list    ｜ 用法：--from 4 或 --from #4 或 --from <ID片段>")
    old = (snap["dir"] / "running.cfg").read_text(encoding="utf-8")
    cur, src = _snap_capture(dev, "display current-configuration")
    # ★ 先确认"当前配置"可信，再做差异 —— 否则会把正常配置判成"缺失"（实测）
    _ok, _why = _cfg_sane(cur, old)
    if not _ok:
        raise SystemExit(
            f"✘ 抓到的当前配置不可信：{_why}\n"
            f"   已中止（不生成任何计划）——避免把设备正常配置误判为差异。\n"
            f"   建议：重试一次；若反复如此，先 netdev shell {dev['name']} 重建会话再试。"
        )
    mode = "add" if getattr(a, "only_add", False) else "full"
    steps, warns, rep = S.plan_restore(old, cur, mode=mode)

    # ★ 防误删保护（2026-09-26 加）：
    #   实测教训 —— diff 一旦误判（把未变更配置算成"新增"），计划会变成
    #   "undo 一大堆设备本来正常运行的配置"（曾出现 undo wlan ac xxx 十余条）。
    #   这里按"undo 数与真实差异数的比例"兜底，宁可拦下来让人复核。
    try:
        _miss, _ext, _same = S.diff_cfg(old, cur)
    except Exception:
        _miss, _ext = [], []
    _danger, _why = S.plan_is_dangerous(steps, len(_miss), len(_ext))
    if _danger and not getattr(a, "force_danger", False):
        raise SystemExit(
            f"✘ 还原计划被【防误删保护】拦下：\n"
            f"   {_why}\n"
            f"   \n"
            f"   常见原因：快照或当前配置抓取时混入了别的内容，导致差异解析偏差。\n"
            f"   建议：① netdev snap list 看看快照是否完整\n"
            f"         ② 重新 netdev snap save 存一份干净快照再对比\n"
            f"         ③ 若确认计划无误，可加 --force-danger 强制执行（风险自负）"
        )
    elif _danger:
        print(f"  {C['yel']}⚠ 防误删保护本会拦下此计划（{_why}），因 --force-danger 放行{C['reset']}")

    print(f"{C['bold']}配置对比 · 快照 #{snap.get('idx')} {snap['id']}（{snap['at']} {snap['tag']}）⟷ 设备当前{C['reset']}")
    items = S.report_lines(rep)
    if not items:
        print(f"  {C['grn']}✔ 完全一致，没有任何差异{C['reset']}")
    for kind, txt in items:
        print(("  " + C['grn'] + "＋ " + C['reset']) if kind == "add" else ("  " + C['yel'] + "−  " + C['reset']), end="")
        print(txt)
    for w in warns:
        print(f"  {C['dim']}! {w}{C['reset']}")

    if act == "diff":
        print(f"\n{C['bold']}── 还原计划（{len(steps)} 步，{ '双向：撤销多出 + 补回缺失' if mode=='full' else '只补回缺失'}）──{C['reset']}")
        for st in steps:
            print(f"  [{st['view'] or '系统视图'}] {st['cmd']}    {C['dim']}{st['why']}{C['reset']}")
        print(f"\n{C['dim']}执行：netdev snap restore {dev['name']} --from {snap['id']} --apply --yes"
              f"（只补不撤加 --only-add）{C['reset']}")
        return 0

    if not steps:
        print(f"\n{C['grn']}✔ 不需要任何改动{C['reset']}")
        return 0
    print(f"\n{C['bold']}── 还原计划（{len(steps)} 步）──{C['reset']}")
    for st in steps:
        print(f"  [{st['view'] or '系统视图'}] {st['cmd']}    {C['dim']}{st['why']}{C['reset']}")
    if not getattr(a, "apply", False):
        print(f"\n{C['yel']}这是预览（没有动设备）。确认无误后执行：{C['reset']}")
        print(f"  netdev snap restore {dev['name']} --from {snap['id']} --apply --yes")
        return 0
    if not getattr(a, "yes", False):
        raise SystemExit("✘ 恢复属写操作，必须加 --yes 二次确认（先看上面的预览）")

    # ① 自动保存"恢复前"状态
    pre_sid = S.snap_id(dev["name"], "恢复前自动")
    S.write_snapshot(S.SNAP_ROOT / pre_sid, cur, "",
                     {"device": dev["name"], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "tag": "恢复前自动", "note": f"before restore {snap['id']}", "source": src})
    print(f"{C['dim']}· 已自动保存恢复前状态：{pre_sid}{C['reset']}")

    win = _snap_window(dev)
    if not win:
        raise SystemExit("✘ 这台设备没有同屏会话，无法稳定下发（先 netdev shell <设备> 建会话）")
    ok = skip = 0
    bad = None
    for st in steps:
        _vrp_goto(win, st["view"])
        out = _vrp_run(win, st["cmd"])
        low = out.lower()
        if any(b in out for b in ("Unrecognized", "Wrong parameter", "Too many parameters", "Incomplete command")) or "Error:" in out:
            first = next((x.strip() for x in out.splitlines() if "Error" in x or "Unrecognized" in x or "Wrong" in x), out.strip()[:80])
            bad = (st, first)
            print(f"  {C['red']}✘ [{st['view'] or '系统视图'}] {st['cmd']} → {first[:90]}{C['reset']}")
            break
        if any(b in low for b in ("does not exist", "not exist", "already exist")):
            skip += 1
            print(f"  {C['dim']}· {st['cmd']}（{first_note(out)}，跳过）{C['reset']}")
        else:
            ok += 1
            print(f"  {C['grn']}✔{C['reset']} {st['cmd']}")
    _vrp_goto(win, "")

    print(f"\n{C['bold']}执行完毕：成功 {ok} 条，跳过 {skip} 条" + (f"，失败 1 条{C['reset']}" if bad else f"{C['reset']}"))
    if bad:
        print(f"{C['yel']}停在：{bad[0]['cmd']} → {bad[1][:80]}{C['reset']}")
        print(f"{C['dim']}可用快照 {pre_sid} 回滚（恢复到执行前的状态）{C['reset']}")
        return 1

    # ② 恢复后自检：再抓一次配置，看还剩多少差异
    print(f"\n{C['bold']}自检中…{C['reset']}")
    after, _after_src = "", ""
    try:
        after, _after_src = _snap_capture(dev, "display current-configuration")
    except BaseException as e:                     # 快照抓不全时别把已完成的恢复报成失败
        print(f"{C['yel']}⚠ 自检抓配置失败：{e}{C['reset']}")
        print(f"{C['yel']}  恢复命令已经下发，请手工核对设备当前配置再决定要不要 save{C['reset']}")
        return 0
    rep2 = S.diff_report(old, after)
    left = S.report_lines(rep2)
    if not left:
        print(f"{C['grn']}✔ 已与快照完全一致（0 处差异）{C['reset']}")
        print(f"{C['yel']}别忘了落盘：netdev save {dev['name']}{C['reset']}")
        return 0
    print(f"{C['yel']}⚠ 仍有 {len(left)} 处差异：{C['reset']}")
    for kind, txt in left:
        print(("  ＋ " if kind == "add" else "  − ") + txt)
    print(f"{C['dim']}（多为不可自动撤销的项，请人工确认后再 save）{C['reset']}")
    return 0


def first_note(out):
    for x in out.splitlines():
        if "exist" in x.lower():
            return x.strip()[:60]
    return "无需处理"


def _vrp_prompt(win):
    """读当前提示符（<...> 用户视图 / [...] 系统或子视图）。"""
    tail = _tmux("capture-pane", "-p", "-J", "-t", f"netops:{win}", "-S", "-8").stdout
    for ln in reversed([x for x in tail.splitlines() if x.strip()]):
        m = re.search(r"([<\[])([^>\]]+)[>\]]\s*$", ln)
        if m:
            return ln.strip()[-40:]
    return ""


def _vrp_run(win, cmd, timeout=25):
    """下发一条命令并取回输出（自动翻页）。"""
    return _extract_cfg(_session_run(win, cmd, timeout, quiet=True), cmd)


def _is_view_entry(cmd):
    """这条命令是不是“进某个视图”（apply 的同屏执行器用它决定要不要先 goto）。

    注：gates.classify 把 `vlan 456` 定为 **write**（因为它在系统视图下会创建 VLAN）——
    所以不能直接拿 VIEW_NAV 当判据，这里单独认“入口类”命令。
    """
    c = cmd.strip()
    if not c:
        return False
    if c.lower() in ("system-view", "sys"):
        return True
    if c.lower() in ("return", "quit", "end"):
        return False
    return bool(re.match(
        r"^(interface\s+\S+|vlan\s+[\d ]+$|vlan\s+batch\s+[\d ]+$|acl\s+(number\s+)?\S+"
        r"|acl\s+ipv6\s+\S+|user-interface\s+\S+|aaa$|ip\s+pool\s+\S+"
        r"|radius-server\s+template\s+\S+|ssl\s+policy\s+\S+|pki\s+realm\s+\S+"
        r"|ike\s+proposal\s+\S+|ipsec\s+proposal\s+\S+|wlan$|wlan\s+ac$"
        r"|\S*-profile\s+name\s+\S+|free-rule-template\s+\S+"
        r"|authentication-profile\s+name\s+\S+)", c, re.I))


def _pane_backup(dev, win, m):
    """在同屏会话里抓运行配置 + flash 配置并存档（人在屏上能看到这两条命令）。"""
    out = {}
    for kind, cmd, suffix in (("运行配置", "display current-configuration", "run"),
                              ("flash 配置", "display saved-configuration", "flash")):
        m.send(cmd, "read_only")
        try:
            text = _vrp_run(win, cmd, timeout=90)
        except BaseException as e:
            m.recv(f"（失败：{type(e).__name__}: {e}）", ok=False)
            print(f"{C['red']}✘ {kind} 备份失败：{e}{C['reset']}")
            return None
        m.recv(text, True)
        p = BACKUPS / f"{dev['name']}_{suffix}_{_ts()}.cfg"
        if text.strip():
            p.write_text(text + "\n", encoding="utf-8")
            out[kind] = p
            print(f"{C['grn']}✔ {kind} → {_P.rel_to_home(p)} ({p.stat().st_size} B){C['reset']}")
        else:
            print(f"{C['red']}✘ {kind} 没抓到内容{C['reset']}")
            return None
    return out


def _pane_after(win, marker, lines=300):
    """窗格历史里**最后一次出现 marker 之后**的那段文本。

    save 的 (y/n)/成功字样就看这段，而不是读窗格日志文件——日志靠 pipe-pane 的 cat 管道，
    respawn-pane 之后那个 cat 会死（曾因此漏看 (y/n)，没回 y，保存被当成取消）。
    """
    txt = _tmux("capture-pane", "-p", "-J", "-t", f"netops:{win}", "-S", f"-{lines}").stdout
    i = txt.rfind(marker)
    return txt[i + len(marker):] if i >= 0 else ""


def _pane_save(win, m=None, timeout=240):
    """在同屏会话里执行 save（自动应答 (y/n)），全程留在屏上。

    防坑（都踩过）：
      * save 必须在**用户视图**下（子视图里会 Unrecognized）；
      * (y/n) 与成功字样只看「我们这条命令之后」的屏内容；
      * 不依赖窗格日志文件（respawn 后管道会死）；
      * 最后用 `dir flash:/vrpcfg.zip` 复核（和串口 / netmiko 引擎一致）。
    """
    tgt = f"netops:{win}"
    if _vrp_prompt(win).startswith("["):
        _vrp_run(win, "return", timeout=15)
    _pager_drain(win)
    marker = "save vrpcfg.zip"
    _tmux("send-keys", "-t", tgt, "-l", marker)
    _tmux("send-keys", "-t", tgt, "Enter")
    t0, answered, seg = time.time(), 0, ""
    while time.time() - t0 < timeout:
        time.sleep(0.8)
        seg = _pane_after(win, marker)
        low = seg.lower()
        if "saved successfully" in low:
            break
        if ("(y/n)" in low or "[y/n]" in low) and answered < 3:
            tail_now = _pane_tail(win, 4)                  # 只当**当前屏上**真在等 (y/n) 时才回 y
            if "(y/n)" not in tail_now and "[y/n]" not in tail_now:
                time.sleep(0.4)
                continue
            _tmux("send-keys", "-t", tgt, "y")
            _tmux("send-keys", "-t", tgt, "Enter")
            answered += 1
            time.sleep(0.6)
            continue
        lines = [x for x in _pane_tail(win, 6).splitlines() if x.strip()]
        if answered and lines and PROMPT_RE.match(lines[-1]) and "(y/n)" not in low:
            break
    try:
        verify = _vrp_run(win, "dir flash:/vrpcfg.zip", timeout=25)
    except BaseException:
        verify = ""
    ok = ("saved successfully" in seg.lower()) and ("vrpcfg" in verify)
    if m:
        m.recv((seg or verify)[-800:], ok)
    return ok, (seg + "\n" + verify)


def _run_via_pane(dev, win, a):
    """run 的"人机同屏"执行器：每条只读命令都打在用户看得见的屏上。

    与 _apply_via_pane 的区别：这里只允许只读命令（闸门拦写），
    不需要备份/回滚，但仍然逐条下发、逐条留档 —— 用户能看到 AI 敲了什么。
    """
    m = mirror.Mirror(dev["name"])
    ok_all = True
    try:
        for cmd in a.commands:
            k = gates.classify(cmd)
            if k != gates.READ_ONLY:
                print(f"{C['red']}✘ 已拒绝（{_risk_tag(k)}）: {cmd}{C['reset']}")
                print(f"{C['dim']}  提示：写操作请用 netdev apply（会先备份并要求确认）{C['reset']}")
                m.send(cmd, k)
                m.recv("run 通道只允许只读命令", ok=False)
                ok_all = False
                continue
            m.send(cmd, k)
            try:
                text = _session_run(win, cmd, timeout=90)
            except SystemExit as e:
                m.recv(str(e), ok=False)
                print(f"{C['red']}✘ {e}{C['reset']}")
                return 1
            body = _trim_tail(_after_echo(text, cmd)) if text else ""
            m.recv(body, ok=True)
            print(f"{C['blu']}▷ {cmd}{C['reset']}")
            print(body if body else "(无回显)")
            print()
    finally:
        m.close()
    print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
    return 0 if ok_all else 1


def _apply_via_pane(dev, win, plan, a, rollback):
    """apply 的“人机同屏”执行器：备份 → 逐条下发 → 校验 → save，全部发生在同一块窗格里。

    为什么要有这条路径：以前 apply 走 netmiko 直连（另开一条会话），你在屏幕上什么都看不到，
    违背了“人机同屏：AI 做什么你都看得见”的设计。现在只要有同屏会话就走这里。
    """
    m = mirror.Mirror(dev["name"])
    save_ok = None
    print(f"{C['dim']}· 走同屏会话 {win}（每一步都打在你眼前这块屏上）{C['reset']}")
    try:
        print(f"\n{C['bold']}① 强制备份（取自同屏会话）{C['reset']}")
        if _pane_backup(dev, win, m) is None:
            return 4

        print(f"\n{C['bold']}② 逐条下发（屏上可见）{C['reset']}")
        _vrp_goto(win, "")                       # 先归位并进系统视图（等价于 netmiko 的 config 模式）
        for c in plan.lines:
            m.send(c, gates.classify(c))
            try:
                if _is_view_entry(c):
                    _vrp_goto(win, "" if c.strip().lower() in ("system-view", "sys") else c)
                    out = ""
                else:
                    out = _vrp_run(win, c)
            except BaseException as e:
                m.recv(f"（失败：{type(e).__name__}: {e}）", ok=False)
                print(f"{C['red']}✘ {c} 执行失败：{e}{C['reset']}")
                return 4
            bad = any(x in out for x in ("Unrecognized", "Wrong parameter", "Too many parameters",
                                        "Incomplete command", "Error:"))
            m.recv(out, not bad)
            if bad:
                first = next((x.strip() for x in out.splitlines()
                              if any(k in x for k in ("Error", "Unrecognized", "Wrong", "Incomplete"))), out.strip()[:80])
                print(f"{C['red']}✘ [{_vrp_prompt(win)}] {c} → {first[:90]}{C['reset']}")
                _print_hint(c, out, dev["name"])
                todo = [x for x in rollback if x]
                if todo and getattr(a, "rollback", False):
                    print(f"{C['yel']}↩ 回滚已下发部分…{C['reset']}")
                    for rb in todo:
                        try:
                            _vrp_goto(win, "")
                            _vrp_run(win, rb)
                            print(f"  {C['dim']}已回滚：{rb}{C['reset']}")
                        except BaseException as e:
                            print(f"  {C['red']}回滚失败：{rb}（{e}）{C['reset']}")
                else:
                    print(f"{C['yel']}⚠ 未自动回滚（可加 --rollback；建议先人工确认）{C['reset']}")
                return 4
            print(f"{C['grn']}✔{C['reset']} {c}")
        print(f"{C['grn']}✔ 下发完成，回显无 Error{C['reset']}")

        print(f"\n{C['bold']}③ 校验{C['reset']}")
        if _vrp_prompt(win).startswith("["):      # 先回到用户视图再校验（否则在子视图里看/取都不稳）
            _vrp_run(win, "return", timeout=15)
        for c in a.verify or []:
            m.send(c, "read_only")
            out = _vrp_run(win, c, timeout=40)
            m.recv(out, True)
            print(f"{C['blu']}▷ {c}{C['reset']}\n{out}\n")

        if plan.save:
            if a.yes or (sys.stdin.isatty() and input("配置已生效，是否 save 落盘？(yes/否) ").strip().lower() in ("yes", "y", "是")):
                print(f"\n{C['bold']}④ save 落盘（屏上可见）{C['reset']}")
                save_ok, out = _pane_save(win, m)
                print(out[-600:])
                print(f"{C['grn'] if save_ok else C['red']}{'✔' if save_ok else '✘'} save{' 完成' if save_ok else ' 失败/未确认'}{C['reset']}")
            else:
                print(f"{C['yel']}⚠ 未落盘：改动重启会丢失，记得稍后 `netdev save {dev['name']}`{C['reset']}")
    finally:
        try:
            _pager_drain(win)
            _vrp_run(win, "return", timeout=15)       # 设备留在用户视图，方便你接着敲
        except BaseException:
            pass
        m.close()
    print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
    return 0 if save_ok is not False else 5


def _vrp_goto(win, view):
    """导航到目标视图：view='' 表示系统视图；否则先到系统视图再进该视图。

    关键：设备可能停在任意子视图（例如 [Huawei-vlan18]），此时直接发 system-view
    会报 "Unrecognized command"，所以必须先 return 归位。
    """
    cur = _vrp_prompt(win)
    want = "[]" if view == "" else view
    if view == "" and cur.startswith("["):
        # 已在系统/子视图：先 return 到用户视图，再进系统视图（幂等且最稳）
        _vrp_run(win, "return")
    elif view and cur.startswith("[") and cur[1:-1] == view:
        return True
    if cur.startswith("<") or view == "":
        _vrp_run(win, "system-view")
        if view:
            _vrp_run(win, view)
    else:
        _vrp_run(win, "return")
        _vrp_run(win, "system-view")
        _vrp_run(win, view)
    return True


def _serial_holder_window(port):
    """哪个同屏窗口正持有这个串口（返回窗口名 / None）。tmux 窗格 pid + lsof 判定。"""
    if not port or not shutil.which("tmux"):
        return None
    try:
        if _tmux("has-session", "-t", "netops").returncode != 0:
            return None
        panes = _tmux("list-panes", "-t", "netops", "-F", "#{window_name}\t#{pane_pid}").stdout
        holders = set(engine.other_port_holders(port))
        if not holders:
            return None
        for ln in panes.strip().splitlines():
            if "\t" not in ln:
                continue
            win, pid = ln.split("\t", 1)
            if pid.strip() in holders:
                return win
    except Exception:
        return None
    return None


def _tmux_input_fixes():
    """同屏会话的输入手感修复（幂等，可反复调用）：

    1) **回看模式（copy-mode）能回到输入**：tmux 默认在 copy-mode 里 Enter/BSpace/DC
       根本没绑动作 —— 滚轮上滚进了回看模式后，打字全被吞（退格、删除也没反应），
       只有 q/Esc 能出来，用户会以为“输入死了、退格坏了”。这里把它们绑成退出回看。
       退格/删除故意不在这里转发：退出后用户再按一下就会走下面的第 2 条适配。

    2) **退格字节适配**：浏览器/macOS 终端退格发 0x7F(DEL)，而网络设备（华为 VRP）
       普遍只认 0x08(BS) —— 收到 0x7F 只会蜂鸣、不删字符。串口/telnet 的桥自己会按
       devices.toml 的 backspace 设置翻译，但“直连 ssh”的窗格没有桥（pane_current_command=ssh），
       所以在这里按窗格类型翻一下；桥的窗格保持原样透传，免得两边翻译打架。

    注：绑定文本必须**整条作为一个参数**传（tmux 把单独的";"参数当命令分隔符，
    会把后面的 send-keys 当场执行而不是存进绑定）。
    """
    cancel = "send-keys -X cancel"
    for table in ("copy-mode", "copy-mode-vi"):
        for key in ("Enter", "Escape", "BSpace", "DC"):
            _tmux("bind-key", "-T", table, key, cancel)
    _tmux("bind-key", "-n", "BSpace",
          "if-shell -F '#{==:#{pane_current_command},ssh}' { send-keys -H 08 } { send-keys -H 7f }")
    # 物理 Delete 键同理（设备对 ESC[3~ 也基本不认）：直连 ssh 窗格当成退格用，其它窗格原样透传
    _tmux("bind-key", "-n", "DC",
          "if-shell -F '#{==:#{pane_current_command},ssh}' { send-keys -H 08 } { send-keys -H 1b 5b 33 7e }")


VIEW_PREFIX = "view-"          # 每个网页终端自己的“视图会话”（里面用链接窗口指向设备窗格）


def _tty_tag():
    """当前终端的稳定短名（网页终端 = 一个 pty：/dev/ttys009 → ttys009）。"""
    try:
        t = os.path.basename(os.ttyname(sys.stdin.fileno()))
    except Exception:
        t = ""
    return re.sub(r"[^A-Za-z0-9]+", "", t) or f"pid{os.getpid()}"


def _tty_in_use(tag):
    """该 pty 还有进程开着吗（= 这个网页终端还活着）。

    不能用 pathlib.Path("/dev/"+tag).exists() 判断：macOS 上 /dev/ttysNNN 设备节点是常驻的，
    永远存在，会把活会话误判成死会话。用 lsof 看有没进程持着这个 tty 才准。
    """
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = subprocess.run([lsof, "-t", f"/dev/{tag}"], capture_output=True, text=True, timeout=4)
        return bool(r.stdout.strip())
    except Exception:
        return True                     # 查不到就当还活着（宁可不删）


def _prune_view_sessions():
    """清掉「终端已经关掉」的视图会话（按对应 pty 还有没有进程在用），不动还活着的终端。"""
    for s in _tmux("list-sessions", "-F", "#{session_name}").stdout.split():
        if s.startswith(VIEW_PREFIX) and not _tty_in_use(s[len(VIEW_PREFIX):]):
            _tmux("kill-session", "-t", s)


def _view_session(dev_win):
    """准备「只属于当前这个终端」的 tmux 会话，返回 (会话名, attach 目标)。

    语义：**1 个终端 = 1 条连接** —— 同一个终端再接入另一台设备时，把旧链接窗口摘掉、
    只留新设备（不堆成 2-3 个窗口），状态栏也写明“本终端接了哪台”。

    为什么需要独立会话：tmux **同一个会话的多个 client 共用“当前窗口”** —— 两个网页终端都接
    netops 会互相抢屏（终端1 切窗口，终端2 跟着变）。这里给每个终端建自己的会话，再用
    link-window 把 netops 里的设备窗格链接过来：各终端各有各的当前窗口，而设备窗格仍住在
    netops 里，所以 AI 侧 screen-read / screen-send / 快照 / apply 全部不变。
    """
    tmx = shutil.which("tmux")
    if not tmx:
        raise SystemExit("未安装 tmux（brew install tmux）")
    if _tmux("has-session", "-t", "netops").returncode != 0:
        raise SystemExit(f"✘ netops 会话不存在（设备窗格都不在）——先 netdev shell {dev_win}")
    if dev_win not in _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split():
        raise SystemExit(f"✘ netops 里没有窗口「{dev_win}」——先 netdev shell {dev_win}")
    _prune_view_sessions()
    sess = VIEW_PREFIX + _tty_tag()

    def _wins():
        return [l.split("\t") for l in
                _tmux("list-windows", "-t", sess, "-F", "#{window_index}\t#{window_name}").stdout.strip().splitlines()
                if l.strip()]

    if _tmux("has-session", "-t", sess).returncode != 0:
        # 注意：不能给这个会话设 destroy-unattached —— client 还没接上就被当成“无人附着”销毁，attach 会扑空。
        _tmux("new-session", "-d", "-s", sess, "-n", "tmp", "-x", "210", "-y", "60")
        _tmux("set-option", "-t", sess, "mouse", "on")
        _tmux("set-option", "-t", sess, "history-limit", "50000")
    if dev_win not in [w[1] for w in _wins()]:
        _tmux("link-window", "-s", f"netops:{dev_win}", "-t", f"{sess}:")   # 链接过来，沿用窗口名
    for idx, nm in _wins():          # 只留当前设备：临时窗口 kill，旧设备窗口 unlink（netops 里仍在）
        if nm == dev_win:
            continue
        _tmux("kill-window" if nm == "tmp" else "unlink-window", "-t", f"{sess}:{idx}")
    idx = next((i for i, nm in _wins() if nm == dev_win), "0")
    # 状态栏写明“本终端接了哪台”+ 别的终端怎么接同一台，一眼就能确认分开
    _tmux("set-option", "-t", sess, "status-left-length", "40")
    _tmux("set-option", "-t", sess, "status-left", f" [#{{session_name}}] {dev_win} ")
    _tmux("set-option", "-t", sess, "status-right-length", "60")
    _tmux("set-option", "-t", sess, "status-right", f" netdev attach {dev_win} ｜ Ctrl+B D 脱离 ")
    _tmux("select-window", "-t", f"{sess}:{idx}")
    return sess, f"{sess}:{idx}"


def cmd_attach(a):
    """把一个设备同屏窗口接到「当前这个终端」自己的会话里（多终端互不抢屏）。

    用法：netdev attach <设备名>   （也接受 netops:<窗口名> 写法）
    """
    win = (getattr(a, "window", "") or "").strip()
    if win.startswith("netops:"):
        win = win.split(":", 1)[1]
    if not win:
        raise SystemExit("用法：netdev attach <设备名>")
    sess, target = _view_session(win)
    tmx = shutil.which("tmux")
    if not sys.stdin.isatty():
        print(f"{C['dim']}（非交互环境：接入用 {tmx} attach-session -t {target}）{C['reset']}")
        return 0
    print(f"{C['dim']}· 本终端的独立会话：{sess}（链接到 netops:{win}）"
          f"｜ 脱离：Ctrl+B 再按 D（设备窗格留在 netops，AI 仍可读）{C['reset']}")
    os.execv(tmx, [tmx, "attach-session", "-t", target])


def cmd_shell(a):
    """建/连一条可被 attach 的 tmux 会话（人机同屏：你看得见 AI 敲的，也能自己接管）。"""
    dev = resolve_target(a.device)
    if not shutil.which("tmux"):
        print(f"{C['yel']}⚠ 未安装 tmux，回退为直接连接（装法：brew install tmux）{C['reset']}")
        subprocess.run(_shell_inner(dev), shell=True)
        return 0
    name = dev["name"]
    # 可达性预检：SSH/Telnet 先探 TCP，避免开出一个永远连不上的死窗口
    proto = dev.get("protocol", "ssh")
    if proto in ("ssh", "telnet"):
        import socket
        host, port = dev.get("host"), int(dev.get("port", 22 if proto == "ssh" else 23))
        try:
            with socket.create_connection((host, port), timeout=2.5):
                pass
        except Exception as e:
            raise SystemExit(f"✘ {name} 的 {proto.upper()}（{host}:{port}）连不上：{type(e).__name__}\n"
                             f"   先确认：① 地址/端口对不对 ② 设备是否在线 ③ 是否只是没插网线\n"
                             f"   （想强制开窗也可以：把 TCP 通了再来；或先 netdev ping {name} <目标>）")
    # ☆ 串口防双桥：这个口已经在某个同屏窗口里用着 → 直接切过去，
    #   绝不新开第二个桥（两个进程抢同一个串口会让对方 read 抛异常而崩）。
    if dev.get("protocol") == "serial":
        _sport = dev.get("port") or engine.discover_serial_port() or ""
        # ☆ 先收拾"孤儿桥"：窗格已销毁但进程还在握着串口（会让后续接入打不开/互抢）
        _stale = _stale_serial_bridges(_sport)
        for _pid in _stale:
            try:
                os.kill(int(_pid), signal.SIGTERM)
                print(f"{C['yel']}⚠ 发现遗留的串口桥进程 {_pid}（窗口已不存在却仍占着 {_sport}）→ 已终止并释放{C['reset']}")
            except Exception as _e:
                print(f"{C['yel']}⚠ 遗留桥 {_pid} 终止失败：{_e}（可手工 kill {_pid}）{C['reset']}")
        if _stale:
            time.sleep(0.8)
        _holder = _serial_holder_window(_sport)
        if _holder and _holder != name:
            if not _pane_alive(_holder):
                print(f"{C['yel']}⚠ 窗口 {_holder} 存在但桥已退出（设备被拔/断开或已退出）{C['reset']}")
                print(f"  {C['dim']}先清掉死窗口：tmux kill-window -t netops:{_holder}，再重新接入{C['reset']}")
                return 1
            print(f"{C['yel']}⚠ 该串口已被同屏窗口「{_holder}」占用（{_sport}）{C['reset']}")
            print(f"  {C['dim']}不再新开第二个桥（两个进程读同一个串口会互相抢字节并把对方打崩）{C['reset']}")
            print(f"  直接用那个窗口：{C['bold']}netdev screen-read {_holder}{C['reset']} ｜ "
                  f"{C['bold']}netdev screen-send {_holder} \"…\"{C['reset']}")
            print(f"  {C['dim']}要换速率/重连：先断掉它 → netdev screen-ls 看窗口，或在里面按 Ctrl+] 退出{C['reset']}")
            if sys.stdin.isatty() and shutil.which("tmux"):
                _tmux("select-window", "-t", f"netops:{_holder}")
                subprocess.run([shutil.which("tmux"), "attach-session", "-t", "netops"])
            return 0

    inner = _shell_inner(dev)
    # ── 回看历史：大 history-limit + 鼠标滚轮进 copy-mode ─────────────────────
    # history-limit 是「窗格创建时」生效的，所以必须在 new-session/new-window 之前设。
    # mouse 是会话选项，对已存在的窗口也立即生效。
    _tmux("start-server")            # 必须先有服务端，否则下面的 set-option 会落空
    _tmux("set-option", "-g", "history-limit", "50000")
    _tmux("set-option", "-g", "mouse", "on")
    _tmux_input_fixes()             # 回看模式能回到输入 + 退格字节适配（幂等）
    _lock = _connect_lock()          # 从这里到 pipe-pane 结束是临界区（attach 之前必须释放）
    has = _tmux("has-session", "-t", "netops").returncode == 0
    if not has:
        _tmux("new-session", "-d", "-s", "netops", "-n", name, "-x", "210", "-y", "60")
        # 命令秒退（认证失败/主机密钥变更等）时保留窗口，否则 tmux 会话会直接蒸发
        _tmux("set-option", "-t", f"netops:{name}", "remain-on-exit", "on")
        time.sleep(0.9)
        _tmux("send-keys", "-t", _tmux_target(name), f"clear; {inner}", "Enter")
        print(f"{C['grn']}✔ 已创建 tmux 会话 netops（窗口 {name}）{C['reset']}")
    else:
        wins = _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split()
        if name not in wins:
            _tmux("new-window", "-t", "netops", "-n", name)
            _tmux("set-option", "-t", f"netops:{name}", "remain-on-exit", "on")
            time.sleep(0.9)
            _tmux("send-keys", "-t", _tmux_target(name), f"clear; {inner}", "Enter")
            print(f"{C['grn']}✔ 已在 netops 中新建窗口 {name}{C['reset']}")
        else:
            cur = _tmux("list-panes", "-t", f"netops:{name}", "-F", "#{pane_current_command}").stdout.strip()
            dead = _tmux("list-panes", "-t", f"netops:{name}", "-F", "#{pane_dead}").stdout.strip() == "1"
            if getattr(a, "restart", False) or dead or cur in ("zsh", "bash", "sh", "-zsh", "-bash"):
                # 窗格死了（设备 20 分钟空闲超时会掉线）/ 里面的命令已退出 / 明要 --restart → 重开
                why = "窗格已死" if dead else f"命令已退出（{cur}）"
                _tmux("respawn-pane", "-k", "-t", _tmux_target(name), f"clear; {inner}")
                print(f"{C['grn']}✔ 窗口 {name}：{why} → 已重开{C['reset']}")
                time.sleep(0.9)
            else:
                print(f"{C['dim']}· 窗口 {name} 已存在（{cur}），直接使用{C['reset']}")
                if cur == "ssh":
                    print(f"{C['yel']}  提示：这个窗口是“直连 ssh”——没有退格适配（浏览器发 0x7F，设备只认 0x08）、"
                          f"也没有屏幕留档{C['reset']}")
                    print(f"{C['dim']}  换成带适配的 ssh 桥： {C['bold']}netdev shell {name} --restart{C['reset']}"
                          f"{C['dim']}（会重连一次，需重新输设备密码）{C['reset']}")
    # 开日志（pane 的原始流）
    logf = ROOT / "live" / f"{name}.pane.log"
    _tmux("pipe-pane", "-t", _tmux_target(name), f"cat >> {logf}")   # 不加 -o：respawn 后旧管道已死，-o 会拒绍重建
    _connect_unlock(_lock)           # 会话已就绪，后面 attach 可能长时间占用，先放锁
    _remember_target(dev, name, getattr(a, "device", None))
    if _tmux("has-session", "-t", "netops").returncode == 0:
        _tmux("set-option", "-t", "netops", "mouse", "on")   # 已存在的会话也立即生效
    if dev.get("_baud_auto"):
        print(f"  {C['dim']}· 波特率自动探测 → {dev['_baud_auto']}（设备回读：{dev.get('_baud_ev','')}）{C['reset']}")
    print(f"  窗口尺寸 210x60（AI 读屏一次能拿很多行）｜ 镜像日志 {_P.rel_to_home(logf)}")
    print(f"  回看历史：{C['bold']}鼠标滚轮上滚{C['reset']}（进回看模式）；回到输入：{C['bold']}回车 / q / Esc{C['reset']}"
          f"（按退格也会自动回到最下端）｜退格已适配成设备认的 0x08")
    print(f"  多终端：{C['bold']}netdev attach <设备>{C['reset']}"
          f"{C['dim']}（每个终端一个独立会话：终端1 接 SSH-A、终端2 接 SSH-B、终端3 接串口 互不抢屏）{C['reset']}")
    print(f"  你的操作：{C['bold']}tmux attach -t netops{C['reset']}；切换/进出用 Ctrl+B 方向键 / Ctrl+B D；退出串口桥按 Ctrl+]")
    if sys.stdin.isatty() and not os.environ.get("NETDEV_NO_ATTACH"):
        sess, target = _view_session(name)     # 每个终端一个独立会话（链接到 netops:name）
        os.execv(shutil.which("tmux"), [shutil.which("tmux"), "attach-session", "-t", target])
    else:
        print(f"  {C['dim']}（不自动 attach：接入用 netdev attach {name}）{C['reset']}")
    return 0


def cmd_quick_shell(a):
    """临时目标直接进同屏会话：netdev telnet 1.2.3.4 / netdev ssh admin@1.2.3.4:22"""
    if a.proto == "telnet":
        spec = f"telnet://{a.username + '@' if a.username else ''}{a.host}"
        if a.port:
            spec += f":{a.port}"
    else:
        spec = f"ssh://{a.target}"
    return cmd_shell(argparse.Namespace(device=spec))


def _conn_window(entry):
    """连接簿条目 → netdev shell 会创建的 tmux 窗口名（与 resolve_target 保持一致）。"""
    if entry.get("protocol") == "serial":
        return _sanitize("serial-" + (entry.get("device") or "auto"))
    return _sanitize(f"{entry.get('protocol')}-{entry.get('host')}")


def cmd_conn(a):
    """连接簿：SSH / Telnet / 串口（含端口）的增删查与一键接入。"""
    from lib import conn_store as cs
    act = a.action
    try:
        if act == "list":
            items = cs.load()
            if getattr(a, "json", False):
                import json as _json
                wins = []
                if shutil.which("tmux") and _tmux("has-session", "-t", "netops").returncode == 0:
                    wins = _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split()
                out = []
                for x in items:
                    y = dict(x)
                    y["uri"] = cs.uri(x)
                    y["window"] = _conn_window(x)
                    y["connected"] = y["window"] in wins
                    out.append(y)
                print(_json.dumps(out, ensure_ascii=False))
                return 0
            if not items:
                print(f"{C['dim']}连接簿为空。添加示例：{C['reset']}")
                print("  netdev conn add ssh 192.168.1.10 --name hw-core-01 -u admin")
                print("  netdev conn add telnet 192.168.1.7 --port 2323 --name old-sw-07")
                print("  netdev conn add serial --device /dev/cu.usbserial-XXXX --name console-1")
                return 0
            print(f"{C['bold']}连接簿（{len(items)} 条）· {_P.rel_to_home(cs.FILE)}{C['reset']}")
            print(f"{'ID':<22}{'协议':<8}{'地址（含端口）':<34}{'备注'}")
            for x in items:
                print(f"{x['id']:<22}{x['protocol']:<8}{x['address']:<34}{x.get('note','')}")
            print(f"\n{C['dim']}接入：netdev conn connect <ID>   升级为正式设备：netdev conn promote <ID>{C['reset']}")
            return 0

        if act == "add":
            proto = (a.proto_or_key or "").lower()
            e = cs.add(proto, host=a.host or "", port=a.port, username=a.username,
                       name=a.name, device=a.device or "", baud=a.baud,
                       note=a.note, platform=a.platform)
            print(f"{C['grn']}✔ 已加入连接簿：{e['name']} ｜ {e['protocol']} ｜ {e['address']}{C['reset']}")
            print(f"  接入：netdev conn connect {e['id']}")
            return 0

        if act == "rm":
            e = cs.remove(a.proto_or_key)
            print(f"{C['grn']}✔ 已移除：{e['name']}（{e['address']}）{C['reset']}")
            return 0

        if act == "connect":
            e = cs.find(a.proto_or_key)
            if not e:
                raise SystemExit(f"✘ 连接簿里没有: {a.proto_or_key}（先 netdev conn list）")
            u = cs.uri(e)
            print(f"{C['dim']}· {e['name']} → {u}{C['reset']}")
            return cmd_shell(argparse.Namespace(device=u))

        if act == "promote":
            e = cs.find(a.proto_or_key)
            if not e:
                raise SystemExit(f"✘ 连接簿里没有: {a.proto_or_key}")
            # 查重：协议相同且地址相同（串口比设备路径；网络比 host:port）已存在 → 拒绝
            for d in engine.load_devices().values():
                same_proto = d.get("protocol", "ssh") == e["protocol"]
                if not same_proto:
                    continue
                if e["protocol"] == "serial":
                    if (d.get("port") or "") == (e.get("device") or ""):
                        raise SystemExit(f"✘ 该串口已登记为设备 '{d['name']}'（同一个 {e['device']}）——"
                                         f"无需重复加；要改名字用 netdev device-add --name 新名，"
                                         f"或先编辑 ~/netops/devices.toml")
                else:
                    if d.get("host") == e.get("host") and int(d.get("port", 23 if e["protocol"] == "telnet" else 22)) == int(e["port"]):
                        raise SystemExit(f"✘ 该地址已登记为设备 '{d['name']}'（{e['address']}）——无需重复加")
            ns = argparse.Namespace(
                name=e["id"], protocol=e["protocol"], host=e.get("host"),
                port=e.get("port"), device_port=e.get("device"), auto=False,
                baud=e.get("baud", 9600), username=e.get("username") or "",
                platform=e.get("platform") or "huawei_vrp", esn=None,
                tags="连接簿", file=None, dry_run=False)
            return cmd_device_add(ns)
    except ValueError as ex:
        raise SystemExit(f"✘ {ex}")
    raise SystemExit(f"✘ 未知动作: {act}")


def cmd_screen_ls(a):
    """列出可人机同屏的会话。

    只列 netops 里的设备窗格：`view-*` 是“某个网页终端自己的视图会话”（里面是 link-window
    链接过去的同一块窗格），不重复计数。
    """
    if not shutil.which("tmux"):
        print("未安装 tmux"); return 1
    if getattr(a, "json", False):
        import json as _json
        r = _tmux("list-panes", "-a", "-F",
                  "#{session_name}\t#{window_name}\t#{pane_width}x#{pane_height}\t#{pane_current_command}\t#{pane_dead}")
        out = []
        for ln in r.stdout.strip().splitlines():
            f = ln.split("\t")
            if len(f) >= 5 and not f[0].startswith(VIEW_PREFIX):
                out.append({"session": f[0], "window": f[1], "target": f"{f[0]}:{f[1]}",
                            "size": f[2], "command": f[3], "dead": f[4] == "1"})
        print(_json.dumps(out, ensure_ascii=False))
        return 0
    r = _tmux("list-panes", "-a", "-F", "#{session_name}:#{window_name}  #{pane_width}x#{pane_height}  #{pane_current_command}  #{pane_dead}")
    rows = [ln for ln in r.stdout.strip().splitlines() if not ln.startswith(VIEW_PREFIX)]
    if not rows:
        print("当前没有同屏会话。用 `netdev shell <设备>` 创建。"); return 0
    print(f"{C['bold']}同屏会话（设备窗格，住在 netops 里）{C['reset']}：")
    for ln in rows:
        print("  " + ln)
    views = [ln for ln in r.stdout.strip().splitlines() if ln.startswith(VIEW_PREFIX)]
    if views:
        print(f"\n{C['bold']}各网页终端自己的会话（1 终端 = 1 连接）{C['reset']}：")
        for ln in views:
            parts = ln.split()
            sess, win = (parts[0].split(":") + ["?"])[:2]
            cl = _tmux("display-message", "-p", "-t", sess, "#{session_attached}").stdout.strip() or "0"
            print(f"  终端 {sess[len(VIEW_PREFIX):]:<12} → 设备 {win:<20} （已挂终端 {cl} 个）")
    print(f"\n{C['dim']}你接入：netdev attach <设备>（每个终端一个独立会话，互不抢屏）{C['reset']}")
    return 0


def _require_pane(dev_name):
    if not shutil.which("tmux"):
        raise SystemExit("未安装 tmux")
    if _tmux("has-session", "-t", "netops").returncode != 0 or \
       dev_name not in _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split():
        raise SystemExit(f"没有 {dev_name} 的同屏会话。先跑：netdev shell {dev_name}")


def cmd_screen_send(a):
    """AI 往同屏会话里发文本（你在屏幕上能看见）。

    安全闸门：黑名单永不代发；**写操作需要确认**（--yes 或交互输入 yes）——
    否则 AI 可以借"打字"绕过 apply 的"先备份+逐条校验"。
    """
    dev = resolve_target(a.device)
    _require_pane(dev["name"])
    risk, who = gates.classify_text(getattr(a, "text", "") or "")
    if risk == gates.BLOCKED:
        raise SystemExit(f"✘ 拒绝代发黑名单命令：{who}（reload/format/delete 这类只能你亲手敲）")
    if risk == gates.WRITE and not getattr(a, "yes", False):
        print(f"{C['yel']}⚠ 这段文本里含**写操作**：{who}{C['reset']}")
        print(f"{C['dim']}  netdev 的规矩：改配置要走 `netdev apply`（先备份 → 逐条 → 校验 → 出错即停）。{C['reset']}")
        print(f"{C['dim']}  若确实要直接打进控制台（例如交互式应答/救急），加 --yes 再跑一次。{C['reset']}")
        if sys.stdin.isatty():
            if input("确认直接发到设备？(yes/否) ").strip().lower() not in ("yes", "y", "是"):
                print("已取消，什么都没发。"); return 3
        else:
            return 3
    if risk == gates.WRITE:
        # 人审：即使带了 --yes，也要人在 Mac 上点一下（fail closed）
        if not approval.ask(dev["name"], [a.text], kind="screen-send"):
            print(f"{C['red']}✘ 人审未通过（拒绝/超时/无 GUI）——什么都没发{C['reset']}")
            print(f"{C['dim']}  审批留档：~/netops/logs/approvals.log{C['reset']}")
            return 3
    m = mirror.Mirror(dev["name"])
    text = a.text
    _tmux("send-keys", "-t", _tmux_target(dev["name"]), "-l", text)
    m.send(f"[同屏] {text}", gates.classify(text))
    if not a.no_enter:
        _tmux("send-keys", "-t", _tmux_target(dev["name"]), "Enter")
    time.sleep(a.wait)
    out = _tmux("capture-pane", "-p", "-J", "-t", _tmux_target(dev["name"]), "-S", f"-{a.lines}").stdout
    tail = "\n".join([l for l in out.splitlines() if l.strip()][-12:])   # 观察口只留尾部，避免刷屏
    m.recv(tail, True)
    m.close()
    print(colorize.paint_str(out.rstrip()) if colorize.enabled() else out.rstrip())
    print(f"{C['dim']}留档: {m.log_path}{C['reset']}")
    return 0


def cmd_screen_read(a):
    """读同屏会话的当前屏幕（AI 看人做了什么、人看 AI 做了什么，同一个屏）。"""
    dev = resolve_target(a.device)
    _require_pane(dev["name"])
    out = _tmux("capture-pane", "-p", "-J", "-t", _tmux_target(dev["name"]), "-S", f"-{a.lines}").stdout
    print(colorize.paint_str(out.rstrip()) if colorize.enabled() else out.rstrip())
    return 0


def cmd_watch(a):
    mirror.tail_live(a.device, follow=not a.no_follow, lines=a.lines)
    return 0


def cmd_cmds(a):
    """厂商常用命令速查（netdev cmds [华为|H3C|锐捷|思科]）。"""
    v = vrp_commands.resolve_vendor(a.vendor)
    print(f"{C['bold']}常用命令速查 · {v.upper()}{C['reset']}\n")
    for title, items in vrp_commands.CHEATSHEET[v]:
        print(f"{C['cyn']}【{title}】{C['reset']}")
        for it in items:
            print(f"   {it}")
        print()
    print(f"{C['dim']}提示：参数不知道填什么，先把命令行敲到一半，用 `netdev hint <设备> \"前缀 \"` 问设备本人。{C['reset']}")
    return 0


def cmd_hint(a):
    """向设备要补全提示：把设备的 '?' 帮助取回来。"""
    dev = resolve_target(a.device)
    s = _open(dev)
    m = mirror.Mirror(dev["name"])
    try:
        r = s.hint(a.prefix or "")
        m.send((a.prefix or "") + "?", "read_only")
        m.recv(r.text, r.ok)
        print(f"{C['blu']}▷ {a.prefix}?{C['reset']}")
        print(r.text or "(设备没返回帮助)")
        if not r.ok:
            print(f"{C['red']}✘ {r.error}{C['reset']}")
        sug = vrp_commands.hint(a.prefix)
        if sug and not r.text.strip():
            print(f"{C['yel']}本地建议：" + " ; ".join(sug) + C['reset'])
        return 0 if r.ok else 1
    finally:
        s.close(); m.close()


def cmd_login(a):
    """登录（先问设备要什么，再决定弹哪个框）并可选存入本地凭据文件。"""
    dev = resolve_target(a.device)
    if getattr(a, "probe_only", False):
        if dev.get("protocol") != "serial":
            print("（只有串口需要 probe；SSH/Telnet 直接用凭据登录）")
            return 0
        s = _open_serial_soft(dev)
        try:
            kind = s.probe_login()
            label = {"user_pass": "要用户名 + 密码（会先弹用户名框，再弹密码框）",
                     "pass_only": "只要密码（只弹一个密码框）",
                     "none": "无需认证（直接进）",
                     "unknown": "没任何回显（设备可能未上电 / 线不在 Console 口 / 波特率不对）"}[kind]
            print(f"{C['bold']}{dev['name']}（{s.port} @ {dev.get('baud',9600)}）{C['reset']}")
            print(f"  设备要求：{label}")
            print(f"{C['dim']}  我没有发送任何凭据、没有修改任何配置{C['reset']}")
        finally:
            s.close()
        return 0
    if dev.get("protocol") == "serial":
        s = _serial_open(dev, allow_popup=True, store=not a.no_store,
                         retries=getattr(a, "retries", 2))
    else:
        user, usrc = creds.get_username(dev, allow_popup=True, prompt_hint="设备登录")
        pw, src = _pw(dev, allow_popup=True)
        if not pw and not dev.get("allow_no_credential"):
            print(f"{C['red']}✘ 没拿到密码{C['reset']}"); return 1
        s = engine.connect(dev, password=pw, username=user or "")
        print(f"{C['grn']}✔ {dev['name']} 登录成功{C['reset']}（用户名 {user}，来源 {usrc}/{src}）")
        if not a.no_store and pw:
            svc = creds.service_of(dev)
            if creds.store_credential(svc, user or "", pw):
                print(f"{C['dim']}· 已存入本地凭据文件：{svc}{C['reset']}")
    try:
        m = mirror.Mirror(dev["name"])
        r = s.run("display clock")
        m.send("display clock", "read_only")
        m.recv(r.text, r.ok)
        m.close()
        print(f"{C['blu']}▷ display clock{C['reset']}\n{r.text}")
        return 0
    finally:
        s.close()


def cmd_keys(a):
    """查看/重测某台设备的退格键适配模式。"""
    from lib import keys as keymod
    dev = resolve_target(a.device)
    if a.set:
        keymod.remember(dev["name"], a.set, "人工指定", dev.get("port", ""))
        print(f"{C['grn']}✔ 已强制设为 {a.set}（{keymod.MODE_DESC.get(a.set, '')}）{C['reset']}")
        return 0
    cache = keymod.load_cache().get(dev["name"])
    if cache and not a.redo:
        print(f"{C['bold']}{dev['name']} 退格键适配：{cache['mode']}{C['reset']}")
        print(f"  依据: {cache['evidence']}")
        print(f"  探测时间: {cache['detected_at']}   串口: {cache.get('port','')}")
        print(f"  {C['dim']}说明: {keymod.MODE_DESC.get(cache['mode'],'')}{C['reset']}")
        print(f"  {C['dim']}重测：netdev keys {dev['name']} --redo ；强制指定：--set bs|del|pass{C['reset']}")
        return 0
    if dev.get("protocol") != "serial":
        print("（只有串口需要）；当前清单值:", dev.get("backspace", "auto")); return 0
    if _tmux_holds(dev["name"]):
        raise SystemExit(f"✘ {dev['name']} 正在同屏会话中，串口被占。先 Ctrl+] 退出桥，或直接重开会话让它自动探测。")
    s = _open_serial_soft(dev)
    try:
        mode, ev = keymod.detect(s.ser)
        if mode == "unknown":
            print(f"{C['yel']}⚠ 未能识别：{ev}{C['reset']}（沿用默认 bs）")
        else:
            keymod.remember(dev["name"], mode, ev, s.port)
            print(f"{C['grn']}✔ 探测结果：{mode}{C['reset']}  ({ev})")
            print(f"  说明: {keymod.MODE_DESC.get(mode, '')}")
            print(f"  已缓存到 {_P.rel_to_home(keymod.CACHE)}")
        return 0
    finally:
        s.close()


def cmd_device_add(a):
    """安全地往设备清单里加一台设备（自动备份 + 语法校验 + 查重）。"""
    import tomllib
    conf = pathlib.Path(a.file or (_P.cfg("devices.toml")))
    if not conf.exists():
        print(f"✘ 找不到清单文件: {conf}"); return 1
    raw = conf.read_text(encoding="utf-8")
    cur = tomllib.loads(raw)
    names = [d.get("name") for d in cur.get("device", [])]
    if a.name in names:
        print(f"✘ 名称已存在: {a.name}（清单里现有：{', '.join(names)}）"); return 1
    proto = (a.protocol or "ssh").lower()
    if proto not in ("ssh", "telnet", "serial"):
        print(f"✘ protocol 只能是 ssh / telnet / serial，收到 {proto}"); return 1
    if proto == "serial":
        if not a.device_port and not a.auto:
            print("✘ 串口设备需要 --device-port（如 /dev/cu.usbserial-XXXX），或 --auto 自动发现"); return 1
    else:
        if not a.host:
            print(f"✘ {proto} 设备需要 --host"); return 1

    lines = [f'\n# 由 netdev device-add 于 {_ts()} 添加',
             "[[device]]",
             f'name     = "{a.name}"',
             f'protocol = "{proto}"']
    if proto == "serial":
        lines.append(f'port     = "{a.device_port or "auto"}"')
        lines.append(f'baud     = {a.baud or 9600}')
        lines.append('backspace = "auto"')
    else:
        lines.append(f'host     = "{a.host}"')
        lines.append(f'port     = {a.port or (22 if proto == "ssh" else 23)}')
    if a.username:
        lines.append(f'username = "{a.username}"')
    lines.append(f'platform = "{a.platform or ("huawei_vrp" if proto != "serial" else "huawei_vrp")}"')
    if proto != "serial":
        lines.append(f'password_keychain = "netdev-{a.name}"')
    else:
        lines.append(f'password_keychain = "netdev-{a.name}"')
    if a.esn:
        lines.append(f'expected_esn = "{a.esn}"')
    tags = a.tags.split(",") if a.tags else []
    lines.append('tags     = [' + ", ".join(f'"{t.strip()}"' for t in tags if t.strip()) + ']' if tags else 'tags     = []')
    block = "\n".join(lines) + "\n"

    new = raw.rstrip("\n") + "\n" + block
    try:                      # ① 先校验语法
        tomllib.loads(new)
    except Exception as e:
        print(f"✘ 生成的 TOML 语法有误，未写入：{e}"); return 1
    if a.dry_run:
        print("【dry-run】将追加以下内容：")
        print(block)
        print(f"（没有写文件；去掉 --dry-run 即生效）")
        return 0
    bak = conf.with_name(conf.name + ".bak-" + _ts())
    shutil.copy2(conf, bak)
    conf.write_text(new, encoding="utf-8")
    print(f"{C['grn']}✔ 已加入设备 {a.name}{C['reset']}（{proto}）")
    print(f"  清单: {conf}   备份: {bak.name}")
    print(f"  下一步：")
    if proto == "serial":
        print(f"    1) netdev shell {a.name}          # 建同屏串口会话（登录时手输账号密码）")
        print(f"    2) netdev identify {a.name}       # 读 ESN 验明身份")
    else:
        print(f"    1) security add-generic-password -a \"$USER\" -s netdev-{a.name} -w -U")
        print(f"    2) netdev login {a.name}          # 首次登录（用户名走清单/本地凭据文件，密码从本地凭据文件取）")
    return 0


def cmd_serial_discover(a):
    import glob
    # 所有 /dev/cu.*（排除蓝牙/调试假串口）——换任何 USB 转串口线都能认出来
    skip_suffix = ("-Incoming-Port", "-Modem")
    found = sorted(p for p in glob.glob("/dev/cu.*")
                   if not p.endswith(skip_suffix) and not p.endswith("cu.debug-console"))
    if not found:
        print("未发现串口设备。检查：USB 转 Console 线是否插好 / 驱动是否装（FTDI·CH340·CP210x）")
        return 1

    # 1) 哪些 netdev 设备用了这些串口
    used = {}
    for d in engine.load_devices().values():
        if d.get("protocol") == "serial" and d.get("port"):
            used[d["port"]] = d["name"]

    # 2) USB 适配器硬件信息（分辨“哪个物理适配器”）
    usb = _usb_serial_info()

    print(f"{C['bold']}发现 {len(found)} 个串口端点：{C['reset']}")
    for p in found:
        mark = "✔ 日常用" if p.startswith("/dev/cu.") else "✘ 一般不用"
        owner = f"   ← netdev 设备: {used[p]}" if p in used else ""
        flag = ""
        try:
            import serial
            s = serial.Serial(p, 9600, timeout=0.2); s.close()
            flag = f"{C['grn']}可打开（9600-8N1）{C['reset']}"
        except Exception as e:
            flag = f"{C['yel']}被占用/打不开: {type(e).__name__}{C['reset']}"
        print(f"  {p}  {flag}  [{mark}]{owner}")
        info = usb.get(p)
        if info:
            print(f"      硬件: {info.get('vendor','?')} {info.get('product','?')}"
                  f"  序列号: {info.get('serial','?')}")
    if not usb:
        print(f"{C['dim']}（未能读到 USB 适配器信息）{C['reset']}")
    print(f"\n{C['dim']}提示：/dev/cu.* 与 /dev/tty.* 是同一条线的两个节点，日常用 cu.*{C['reset']}")
    print(f"{C['dim']}提示：拿不准对面是哪台设备？登录后跑 netdev identify <设备名> 读 ESN 对账{C['reset']}")
    return 0


def _usb_serial_info():
    """读“/dev 节点 ← USB 适配器”对应关系（芯片/厂商/序列号）。

    先试 system_profiler（结构化、准），失败再回退 ioreg。
    """
    import subprocess, json as _json, re as _re
    by_serial = {}
    try:
        r = subprocess.run(["/usr/sbin/system_profiler", "SPUSBDataType", "-json"],
                           capture_output=True, text=True, timeout=25)
        data = _json.loads(r.stdout or "{}")
    except Exception:
        data = {}

    def walk(items):
        for it in items or []:
            sn = (it.get("serial_num") or "").strip()
            name = (it.get("_name") or "").strip()
            man = (it.get("manufacturer") or "").strip()
            if sn:
                by_serial[sn.upper()] = {"product": name, "vendor": man, "serial": sn}
            walk(it.get("_items"))

    walk((data.get("SPUSBDataType") or []))

    # 把 /dev 节点映射到 USB 记录（FTDI 的节点后缀就是适配器序列号）
    out = {}
    for dev_node, pat in (("/dev/cu.usbserial-", r"usbserial-(.+)$"),
                          ("/dev/cu.usbmodem", r"usbmodem(.+)$"),
                          ("/dev/cu.wchusbserial", r"wchusbserial(\w+)$")):
        import glob
        for p in glob.glob(dev_node + "*"):
            m = _re.search(pat, p)
            if not m:
                continue
            key = m.group(1).upper()
            hit = by_serial.get(key)
            if not hit:
                hit = next((v for k, v in by_serial.items() if key and (key in k or k in key)), None)
            if hit:
                out[p] = hit
    return out


def cmd_onboard(a):
    """新设备上架：第 1~2 步（只读清点 + 出厂配置双备份）。"""
    dev = resolve_target(a.device)
    cmds = ["display version", "display esn", "display device", "display interface brief",
            "display ip interface brief", "display vlan", "display current-configuration | include local-user|stelnet|telnet|vty"]
    print(f"{C['bold']}== 新设备上架 · 清点阶段（全部只读）=={C['reset']}")
    rc = cmd_run(argparse.Namespace(device=a.device, commands=cmds))
    print(f"\n{C['bold']}== 出厂配置双备份 =={C['reset']}")
    cmd_backup(argparse.Namespace(device=a.device))
    print(f"\n{C['grn']}✔ 清点完成。下一步（定基线/开 SSH）需人工确认后执行，见设计方案 §U6。{C['reset']}")
    return rc


def cmd_selftest(a):
    """离线端到端自检：启动本机模拟器 → 跑 run/apply/save/backup/gates。"""
    # 2026-10-03：全新克隆时还没有 devices.toml，原来直接 engine.get_device("mock-hw")
    # 抛 KeyError traceback，对新手极不友好。改成先检查并给一条可照抄的命令。
    if not _P.cfg("devices.toml").exists():
        print(f"{C['red']}✘ 还没有设备清单{_P.rel_to_home(_P.cfg('devices.toml'))}{C['reset']}")
        print(f"{C['dim']}   自检要打仓库自带的本机模拟器，所以先得有清单。执行这一行：{C['reset']}")
        print(f"{C['bold']}     cp {_P.rel_to_home(ROOT)}/config/devices.toml.example "
              f"{_P.rel_to_home(ROOT)}/config/devices.toml{C['reset']}")
        return 2
    sim = ROOT / "tests" / "mock_vrp.py"
    port = engine.get_device("mock-hw").get("port", 20037)
    proc = subprocess.Popen([sys.executable, str(sim), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.2)
    fails = []
    # 自检打的是本机模拟器（mock-hw，sim=true + 回环地址），没有真人可点弹窗。
    # 打开这条窄豁免，让 selftest 能在 CI（无 GUI）里跑通；finally 里必定清掉。
    os.environ["NETDEV_SELFTEST"] = "1"
    try:
        # 注：这里原来有一句 `import os`。模块顶层已经 import 了 os，
        # 这句会让 os 变成**函数局部变量**，导致上面 os.environ[...] 报
        # UnboundLocalError（实测踩到）。已删除。
        os.environ.setdefault("NETDEV_MOCK_PASSWORD", "admin")
        print(f"\n{C['bold']}① 只读命令{C['reset']}")
        rc = cmd_run(argparse.Namespace(device="mock-hw", commands=["display version", "display clock"]))
        if rc != 0:
            fails.append("run 只读命令")
        print(f"\n{C['bold']}② 闸门：写操作应被 run 拒绝{C['reset']}")
        rc = cmd_run(argparse.Namespace(device="mock-hw", commands=["sysname HACKED"]))
        if rc == 0:
            fails.append("run 未拒绝写命令")
        print(f"\n{C['bold']}③ 闸门：黑名单应被拒绝{C['reset']}")
        rc = cmd_run(argparse.Namespace(device="mock-hw", commands=["reload"]))
        if rc == 0:
            fails.append("run 未拒绝黑名单")
        print(f"\n{C['bold']}④ apply 下发（含备份 + save）{C['reset']}")
        rc = cmd_apply(argparse.Namespace(device="mock-hw", cmd=["acl 2000", "rule 15 permit source 1.1.1.0 0.0.0.255"],
                                          file=None, yes=True, no_save=False, rollback=False,
                                          verify=["display acl 2000"]))
        if rc != 0:
            fails.append("apply 下发")
        print(f"\n{C['bold']}⑤ backup{C['reset']}")
        rc = cmd_backup(argparse.Namespace(device="mock-hw"))
        if rc != 0:
            fails.append("backup")
    finally:
        # ★ 豁免开关必须用完即清（2026-10-03）：lib/approval.py 的 selftest 免人审
        #   只在这个变量为 1 且目标是本机模拟器时才生效。哪怕自检中途抛异常，
        #   也不能把它留在环境里 —— 否则这台机器之后的真实写操作会绕过人审。
        os.environ.pop("NETDEV_SELFTEST", None)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
    print(f"\n{C['bold']}== 自检结果 =={C['reset']}")
    if fails:
        print(f"{C['red']}✘ 失败项：{'; '.join(fails)}{C['reset']}")
        print(out[-2000:])
        return 1
    print(f"{C['grn']}✔ 全部通过（run / 闸门×2 / apply+save / backup）{C['reset']}")
    print(f"{C['dim']}镜像与日志：{ROOT/'live'} , {ROOT/'logs'}{C['reset']}")
    return 0




# ─────────────────────────────────────────────────────────────────────────────
#  网页接入命令：安装 / 体检 / 自愈 / 卸载
#    宿主的终端页左栏「命令」区读的是 <workspace>/.pi/commands.json（原生功能）。
#    本命令负责把那 4 条接入命令幂等写进去，并在依赖变化后自愈（换解释器/换家目录都能修）。
# ─────────────────────────────────────────────────────────────────────────────
WEB_CMDS = [
    ("→ 快速接入（SSH / Telnet / 串口）", "quick"),
    ("≡ 管理接入目标（接入/列出/新增/删除）", "manage"),
    ("↕ 备份 / 恢复 / 管理快照", "snapmenu"),
]

# 历史名字：旧按钮升级后要一并清掉（否则 web repair 后旧的会赖着不走）
WEB_CMDS_LEGACY = {
    # emoji 版旧名（2026-09-19 改线性图标前的名字）：必须保留原样才能被清掉
    "🔌 快速接入（SSH / Telnet / 串口）",
    "🧭 管理接入目标（接入/列出/新增/删除）",
    "💾 备份 / 恢复 / 管理快照",
    "➕ 添加接入目标（含端口）",
    "🔌 SSH 接入（可选端口）",
    "🔌 Telnet 接入（可选端口）",
    "🔌 串口接入（选串口+波特率）",
    "📜 回看屏幕（最近 1000 行）",
    "💾 备份设备配置（客户快照）",
    "♻️ 恢复配置备份（先预览）",
    "🗑 管理/删除快照",
}

# 每台设备一个“接 xx”按钮（前缀固定，便于清理旧设备按钮）
DEV_CMD_PREFIX = "↗ 接 "                        # 现行前缀（线性图标）
DEV_CMD_PREFIX_LEGACY = ("🖥 接 ",)             # 历史前缀（emoji 版）：清理旧按钮时一并匹配
_DEV_PREFIXES = (DEV_CMD_PREFIX, *DEV_CMD_PREFIX_LEGACY)

# 设备按钮开关：config/web.toml 的 device_buttons（默认 true = 生成）。
# 关掉后左栏只留 3 条功能按钮；**多终端各接一台不受影响** —— netdev attach 仍按 pty
# 给每个终端建独立的 view-<pty> 会话，在每个终端里各跑一次「快速接入」即可。
WEB_CONF = ROOT / "config" / "web.toml"


def _web_device_buttons_enabled() -> bool:
    try:
        if WEB_CONF.exists():
            import tomllib
            d = tomllib.loads(WEB_CONF.read_text(encoding="utf-8"))
            if "device_buttons" in d:
                return bool(d.get("device_buttons"))
    except Exception:
        pass
    return True


def _web_set_device_buttons(enabled: bool):
    """把 device_buttons 写进 config/web.toml（本文件由 netdev 管理）。"""
    WEB_CONF.parent.mkdir(parents=True, exist_ok=True)
    WEB_CONF.write_text(
        "# pi-web-ui 左栏「命令」区的生成选项\n"
        "# device_buttons：是否给每台设备生成「↗ 接 xx」按钮（false = 只留 3 条功能按钮）\n"
        f"device_buttons = {'true' if enabled else 'false'}\n",
        encoding="utf-8")
    return WEB_CONF


# ── 接入记录：重启后一键恢复（tmux 不持久，这里记住"上次接了谁"）──────────────
STATE_DIR = _P.state_dir()   # 全新克隆里没有这个目录，state_dir() 会建
LAST_TARGETS = STATE_DIR / "last_targets.json"


def _remember_target(dev, window, spec=None):
    """把本次接入记进 state/last_targets.json（去重，保留最近 20 条）。"""
    import json as _json
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        items = _json.loads(LAST_TARGETS.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    entry = {
        "window": window,
        "name": dev.get("name"),
        "protocol": dev.get("protocol", "ssh"),
        "spec": spec or dev.get("name"),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = [x for x in items if x.get("window") != window and x.get("spec") != entry["spec"]]
    items.append(entry)
    items = items[-20:]
    try:
        LAST_TARGETS.write_text(_json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except Exception:
        pass


def cmd_resume(a):
    """列出上次接入过的目标；--all 把当前不存在的会话重建起来。"""
    import json as _json
    items = []
    try:
        items = _json.loads(LAST_TARGETS.read_text(encoding="utf-8")) or []
    except Exception:
        items = []
    if not items:
        print(f"{C['dim']}还没有接入记录。先用 netdev shell/telnet/ssh 或网页里的接入命令接一次。{C['reset']}")
        return 0
    wins = set()
    if shutil.which("tmux") and _tmux("has-session", "-t", "netops").returncode == 0:
        wins = set(_tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split())
    print(f"{C['bold']}接入记录（最近 {len(items)} 条）{C['reset']}")
    for x in items:
        live = x.get("window") in wins
        mark = f"{C['grn']}在线{C['reset']}" if live else f"{C['yel']}已断{C['reset']}"
        print(f"  [{mark}] {x.get('name','?'):<18}{x.get('protocol',''):<7}{x.get('spec',''):<40}{C['dim']}{x.get('at','')}{C['reset']}")
    missing = [x for x in items if x.get("window") not in wins]
    if not getattr(a, "all", False):
        print(f"\n{C['dim']}重建已断开的：netdev resume --all   只重建最近一条：netdev resume --last{C['reset']}")
        return 0
    if not missing:
        print(f"\n{C['grn']}✔ 记录里的目标都在线，无需重建{C['reset']}")
        return 0
    print(f"\n{C['bold']}重建 {len(missing)} 条…{C['reset']}")
    for x in missing:
        spec = x.get("spec") or x.get("name")
        print(f"  · {spec}")
        try:
            cmd_shell(argparse.Namespace(device=spec))
        except SystemExit as e:
            print(f"    {C['yel']}跳过：{e}{C['reset']}")
    return 0


# ── 日志：状态 / 轮转 / 定时轮转（只搬不删：gzip 归档到 logs/rotated）────────────
LOGROTATE_LABEL = "com.netdev.logrotate"
LOGROTATE_PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{LOGROTATE_LABEL}.plist"
ROTATED_DIR = ROOT / "logs/rotated"
WEBLOG = pathlib.Path.home() / ".pi-web/pi-web-ui.log"


def _live_windows():
    if shutil.which("tmux") and _tmux("has-session", "-t", "netops").returncode == 0:
        return set(_tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split())
    return set()


def _log_targets(max_mb_weblog=20, max_mb_live=50, max_mb_archive=200):
    """(文件, 阈值MB, 是否可安全轮转)——活着的会话正在写的镜像日志不动（避免 fd 错位）。"""
    live = _live_windows()
    out = []
    for f in (WEBLOG, WEBLOG.with_suffix(".err")):
        if f.exists():
            out.append((f, max_mb_weblog, True))
    for f in sorted((ROOT / "live").glob("*.log")):
        inuse = any(f.name.startswith(w + ".") for w in live)
        out.append((f, max_mb_live, not inuse))
    for f in sorted((ROOT / "logs").glob("*.log")):
        out.append((f, max_mb_archive, True))
    return out


def _rotate_one(f: pathlib.Path) -> str:
    """gzip 归档到 logs/rotated/ 后清空原文件（内容不丢，只是搬走）。"""
    import gzip
    ROTATED_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = ROTATED_DIR / f"{f.name}.{ts}.gz"
    with open(f, "rb") as src, gzip.open(dst, "wb") as out:
        shutil.copyfileobj(src, out, length=1 << 20)
    f.write_bytes(b"")
    return str(_P.rel_to_home(dst))


def _launchd_has(label: str) -> bool:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return label in r.stdout


def cmd_logs(a):
    act = a.action
    if act == "status":
        print(f"{C['bold']}日志占用{C['reset']}")
        def _sz(p):
            try:
                return p.stat().st_size
            except Exception:
                return 0
        # ① pi-web-ui 日志（单文件）
        for f in (WEBLOG, WEBLOG.with_suffix(".err")):
            if not f.exists():
                continue
            sz = _sz(f)
            flag = f"{C['yel']}超限{C['reset']}" if sz > 20 * 1024 * 1024 else f"{C['grn']}正常{C['reset']}"
            print(f"  [{flag}] {sz/1024/1024:7.2f} MB  {_P.rel_to_home(f)}（上限 20 MB）")
        # ② 目录类：只给汇总 + 最大 3 个
        live = _live_windows()
        for d, limit in ((ROOT / "live", 50), (ROOT / "logs", 200)):
            files = sorted((f for f in d.glob("*.log")), key=_sz, reverse=True) if d.exists() else []
            total = sum(_sz(f) for f in files)
            over = [f for f in files if _sz(f) > limit * 1024 * 1024]
            flag = f"{C['yel']}{len(over)} 个超限{C['reset']}" if over else f"{C['grn']}正常{C['reset']}"
            print(f"  [{flag}] {total/1024/1024:7.2f} MB  {_P.rel_to_home(d)}/（{len(files)} 个文件，单文件上限 {limit} MB）")
            for f in files[:3]:
                inuse = d.name == "live" and any(f.name.startswith(w + ".") for w in live)
                print(f"           {_sz(f)/1024/1024:7.2f} MB  {f.name}" + (f"  {C['dim']}(会话在用)" + C['reset'] if inuse else ""))
        print(f"  {C['dim']}归档目录 logs/rotated ｜ 定时轮转 agent：{'已装（每 6h 检查）' if _launchd_has(LOGROTATE_LABEL) else '未装 → netdev logs install-agent'}{C['reset']}")
        return 0

    if act == "rotate":
        did = []
        for f, limit, safe in _log_targets(max_mb_weblog=getattr(a, "max_mb", 20)):
            try:
                sz = f.stat().st_size
            except Exception:
                continue
            if sz < limit * 1024 * 1024:
                continue
            if not safe:
                print(f"  {C['dim']}跳过（会话在用）：{f.name}{C['reset']}")
                continue
            did.append((f, sz, _rotate_one(f)))
        if not did:
            print(f"{C['grn']}✔ 没有超限日志，无需轮转{C['reset']}")
            return 0
        for f, sz, dst in did:
            print(f"  {C['grn']}✔{C['reset']} {f.name}（{sz/1024/1024:.1f} MB）→ {dst}")
        if any(f == WEBLOG for f, _, _ in did) and _launchd_has("com.xingshuyin.pi-web-ui"):
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.xingshuyin.pi-web-ui"],
                           capture_output=True, text=True)
            print(f"  {C['dim']}已重启 pi-web-ui 让日志句柄归零（网页会自动重连）{C['reset']}")
        print(f"{C['dim']}归档在 ~/netops/logs/rotated/（gzip，只搬不删）{C['reset']}")
        return 0

    if act == "install-agent":
        LOGROTATE_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LOGROTATE_PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LOGROTATE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{ROOT}/netdev</string>
    <string>logs</string>
    <string>rotate</string>
  </array>
  <key>StartInterval</key><integer>21600</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardErrorPath</key><string>{ROOT}/logs/logrotate.err.log</string>
  <key>StandardOutPath</key><string>{ROOT}/logs/logrotate.out.log</string>
</dict>
</plist>
""", encoding="utf-8")
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LOGROTATE_LABEL}"], capture_output=True, text=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LOGROTATE_PLIST)],
                           capture_output=True, text=True)
        print(f"{C['grn']}✔ 已装定时轮转{C['reset']}（每 6 小时检查一次，超限才动）→ {LOGROTATE_PLIST}")
        if r.returncode != 0 and r.stderr.strip():
            print(f"  {C['yel']}{r.stderr.strip()[:120]}{C['reset']}")
        return 0

    if act == "uninstall-agent":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LOGROTATE_LABEL}"], capture_output=True, text=True)
        if LOGROTATE_PLIST.exists():
            bak = ROOT / "backups" / f"{LOGROTATE_LABEL}.plist.removed-{time.strftime('%Y%m%d_%H%M%S')}"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(LOGROTATE_PLIST), str(bak))
            print(f"{C['grn']}✔ 已卸载定时轮转{C['reset']}（plist 移入 {bak.name}，未删除）")
        else:
            print("  本来就未安装")
        return 0
    return 0


def cmd_policy(a):
    """写操作权限模式：readonly（只读）｜ ask（确认，默认）｜ allow（放行，可带 TTL）。"""
    import json as _json
    act = a.action
    if getattr(a, "json", False):
        print(_json.dumps(approval.mode_info(), ensure_ascii=False))
        return 0
    if act == "show" or act is None:
        mi = approval.mode_info()
        color = {"readonly": C['red'], "ask": C['yel'], "allow": C['grn']}[mi["mode"]]
        print(f"{C['bold']}写操作权限模式{C['reset']}: {color}{mi['label']}{C['reset']}（{mi['mode']}）"
              + (f"  {C['dim']}剩余 {mi['remaining']//60} 分 {mi['remaining']%60} 秒后自动回落确认模式{C['reset']}"
                 if mi.get("remaining") else ""))
        print(f"  readonly = 只读模式：写操作一律拒发（AI 只能读）")
        print(f"  ask      = 确认模式：每次写操作弹原生弹窗，人点「允许」才发（默认）")
        print(f"  allow    = 放行模式：不再询问（放宽本身也要人点弹窗一次；默认 5 小时后自动回落确认模式）")
        print(f"  {C['dim']}网页：顶栏「 权限模式」下拉可直接切换｜审计：~/netops/logs/approvals.log{C['reset']}")
        return 0
    if act == "allow":
        minutes = getattr(a, "minutes", None)
        if approval.policy() == "allow" and not minutes:
            print("  已经是放行模式，无需重复放宽。"); return 0
        if not approval.ask_policy_relax():
            print(f"{C['red']}✘ 人审未通过，模式维持 {approval.policy()}{C['reset']}"); return 3
        approval.set_policy("allow", minutes=minutes)
        mi = approval.mode_info()
        print(f"{C['yel']}⚠ 已切到放行模式：写操作不再弹窗{C['reset']}"
              f"（{int((mi.get('remaining') or 0) / 60)} 分钟后自动回落确认模式）")
        return 0
    if act in ("ask", "readonly"):
        approval.set_policy(act)
        print(f"{C['grn']}✔ 已切到{approval.MODE_LABEL[act]}（{act}）{C['reset']}")
        return 0
    raise SystemExit("用法: netdev policy [show|readonly|ask|allow] [--minutes N] [--json]")


def cmd_policy_placeholder():
    pass


def _web_python():
    """挑一个稳的解释器：优先系统 python3（与 venv 无关），回落 venv。"""
    for cand in ("/usr/bin/python3", str(ROOT / ".venv/bin/python3"), shutil.which("python3") or ""):
        if cand and pathlib.Path(cand).exists():
            return cand
    return "python3"


def _web_entries():
    """生成网页终端（pi-web-ui）左栏按钮。

    ★ 2026-10-01 修：原来这里把工具路径**写死**成 `~/netops/tools/...`
      （当时注释说"~ 由宿主展开，换家目录也不怕"）——
      实际上它只对"装在 ~/netops"成立。用 `--prefix /opt/netdev` 或
      任意路径安装时，生成的按钮指向一个不存在的目录，点下去直接报错，
      而 `netdev doctor` 只看"按钮条数"、看不出路径是错的 →
      **装完看着成功、点开才发现不能用**。
      现在一律用真实 ROOT 的绝对路径；ROOT 本身就是
      NETDEV_ROOT 环境变量 / 脚本位置推导出来的（见文件头）。
      `~` 形式仅在 ROOT 恰好等于 $HOME/netops 时保留，纯为可读性。
    """
    py = _web_python()
    home_np = pathlib.Path.home() / "netops"
    base = "~/netops" if ROOT == home_np.resolve() else str(ROOT)
    quick = f"{base}/tools/quick_conn.py"
    scroll = f"{base}/tools/scrollback.py"
    snap = f"{base}/tools/snapshot.py"
    out = []
    for name, kind in WEB_CMDS:
        if kind == "scroll":
            cmd = f"{py} {scroll} --lines 1000"
        elif kind == "snapmenu":
            cmd = f"{py} {snap} menu"
        elif kind == "snapsave":
            cmd = f"{py} {snap} save"
        elif kind == "snaprestore":
            cmd = f"{py} {snap} restore"
        elif kind == "snapmanage":
            cmd = f"{py} {snap} manage"
        else:
            cmd = f"{py} {quick} {kind}"
        out.append({"name": name, "command": cmd, "cwd": "${pwd}"})
    # ── 每台设备一个按钮（DEV_CMD_PREFIX）──────────────────────────────
    # 为什么需要：宿主的按钮点击是“按标题复用终端”——通用按钮永远只在同一个终端里跑。
    # 每台设备一个按钮 → 宿主会给每个按钮一个**自己的终端** → 天然就是“一终端一设备”。
    # 而且左栏按钮列表是**数据**（.pi/commands.json，点 ⟳ 即重读），不经过前端 JS 缓存。
    try:
        for d in (engine.load_devices().values() if _web_device_buttons_enabled() else ()):
            nm = d.get("name")
            if not nm:
                continue
            label = DEV_CMD_PREFIX + nm + ("（模拟器）" if d.get("sim") else "")
            out.append({"name": label, "command": f"{py} {quick} attach {nm}", "cwd": "${pwd}"})
    except Exception:
        pass
    return out


def _web_path(cwd=None):
    root = pathlib.Path(cwd).expanduser().resolve() if cwd else pathlib.Path.home()
    return root / ".pi" / "commands.json"


def _web_load(f: pathlib.Path):
    import json as _json
    if not f.exists():
        return {"commands": []}, None
    try:
        d = _json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("commands"), list):
            return d, None
        return {"commands": []}, '文件形状不是 {"commands": [...]}，已按空清单处理（原文件会先备份）'
    except Exception as e:
        return {"commands": []}, f"解析失败（{e}），已按空清单处理（原文件会先备份）"


def _web_write(f: pathlib.Path, data: dict) -> str:
    import json as _json
    # ★ 悬空软链兜底（2026-10-01 实测踩到）：
    #   ~/.pi/commands.json 曾是一条指向已删除目录的软链（/tmp/wb-e2e/...），
    #   write_text 会跟随软链去写那个不存在的目标 → FileNotFoundError → 整个
    #   `netdev web repair` 抛 traceback 崩掉。开源后别人机器上同样可能出现
    #   （装过又卸载、我们自己的 e2e 测试残留）。这里：软链目标目录不可达 →
    #   删掉软链，改写成真文件，保证 repair 永远能落地。
    try:
        if f.is_symlink():
            _tgt = f.resolve(strict=False)
            if not _tgt.parent.exists():
                f.unlink()
    except Exception:
        pass
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"✘ 无法创建目录：{f.parent}\n"
                         f"   原因: {type(e).__name__}: {e}\n"
                         f"   处理: 检查该目录的父级权限（ls -ld {f.parent.parent}）")
    bak = ""
    if f.exists():
        bak = str(BACKUPS / f"commands.json.bak-{time.strftime('%Y%m%d_%H%M%S')}")
        BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, bak)
    try:
        f.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"✘ 写入失败：{f}\n"
                         f"   原因: {type(e).__name__}: {e}\n"
                         f"   处理: 看它是不是悬空软链（ls -l {f}）；是就 rm 掉重试")
    return bak



def _doctor_rows():
    """体检项 → (名称, 是否通过, 说明/修法)"""
    import json as _json
    rows = []
    dev = ROOT / "netdev"
    rows.append(("netdev 入口", dev.exists() and os.access(dev, os.X_OK), str(dev)))
    venv = ROOT / ".venv/bin/python"
    rows.append(("venv python", venv.exists(), str(venv) if venv.exists() else "缺失（设备读写会不可用）"))
    if venv.exists():
        r = subprocess.run([str(venv), "-c", "import netmiko,serial;print(netmiko.__version__, serial.__version__)"],
                           capture_output=True, text=True)
        rows.append(("netmiko / pyserial", r.returncode == 0, (r.stdout or r.stderr).strip()[:60]))
    _which_tmux = shutil.which("tmux")
    tmux = pathlib.Path(_which_tmux) if _which_tmux else next(
        (p for p in (ROOT.parent / "homebrew/bin/tmux", pathlib.Path("/opt/homebrew/bin/tmux"),
                     pathlib.Path("/usr/local/bin/tmux"), pathlib.Path("/usr/bin/tmux")) if p.exists()), None)
    rows.append(("tmux（同屏会话）", bool(tmux), str(tmux) if tmux else "缺失：同屏会话/AI 读屏不可用"))
    script = ROOT / "tools/quick_conn.py"
    rows.append(("接入脚本", script.exists() and os.access(script, os.X_OK),
                 str(script) + ("" if script.exists() and os.access(script, os.X_OK) else "（缺失或不可执行）")))
    f = _web_path(None)
    data, warn = _web_load(f)
    have = {c.get("name") for c in data["commands"]}
    missing = [n for n, _ in WEB_CMDS if n not in have]
    rows.append(("终端页命令清单", not missing, (f"{f} ✔ {len(have & {n for n,_ in WEB_CMDS})}/{len(WEB_CMDS)} 条"
                 if not missing else f"缺 {len(missing)} 条 → netdev web repair") + (f"；{warn}" if warn else "")))
    conns = _P.cfg("connections.json")
    devs_toml = _P.cfg("devices.toml")
    ndev = len(engine.load_devices()) if devs_toml.exists() else 0
    if not devs_toml.exists():
        # 全新克隆/新装：还没有设备清单是**正常状态**，不是故障。
        # 判据要看"文件在不在"，不要看"里面有没有设备"（实测踩到：clone 完恒红）。
        rows.append(("设备/连接清单", False,
                     "还没有 config/devices.toml → 从模板生成："
                     "cp config/devices.toml.example config/devices.toml（里面有自带的本机模拟器）"))
    else:
        rows.append(("设备/连接清单", True,
                     f"devices.toml {ndev} 台" + ("（还没加设备是正常的：netdev device-add <名字>）" if not ndev else "")
                     + f"；connections.json {'有' if conns.exists() else '暂无（正常）'}"))
    ports = sorted(pathlib.Path("/dev").glob("cu.usbserial*"))
    rows.append(("串口设备", True, f"{len(ports)} 个：" + (", ".join(p.name for p in ports) if ports else "当前无（USB-Console 线未插是正常的）")))
    try:
        tmux_bin = shutil.which("tmux") or "tmux"
        out = subprocess.run([tmux_bin, "list-panes", "-a", "-F", "#{window_name} dead=#{pane_dead}"],
                             capture_output=True, text=True, timeout=6).stdout.strip()
        dead = [x.split()[0] for x in out.splitlines() if "dead=1" in x]
        rows.append(("同屏会话", True, (out.replace("\n", " ｜ ") if out else "无会话（正常；接上设备后才会有）") +
                     (f"　⚠ 已死窗口：{', '.join(dead)}（桥已退出，设备可能被拔）→ 重新接入即可" if dead else "")))
    except Exception as e:
        rows.append(("同屏会话", False, f"查不到：{e}"))
    _uh = _ui_health()
    rows.append((f"netdev-ui :{UI_PORT}", _uh["up"],
                 (f"HTTP 200 {_uh['detail']}  PID {_uh['pid'] or '未记'}"
                  if _uh["up"] else f"{'端口被占但服务不应答' if _uh['port_open'] else '无响应'}"
                  f" → 启动：netdev ui start（或直接 netdev ui，没有就起、有就报状态）")))
    log = ROOT / "logs/ui-service.log"
    live = ROOT / "live"
    def _sz(p):
        try:
            return sum(x.stat().st_size for x in ([p] if p.is_file() else p.rglob("*")) if x.is_file())
        except Exception:
            return 0
    lsz, vsz = _sz(log), _sz(live)
    rows.append(("日志占用", lsz < 50 * 1024 * 1024 and vsz < 200 * 1024 * 1024,
                 f"ui-service.log {lsz/1024:.0f}K；netops/live {vsz/1024/1024:.1f}M"
                 + ("（偏大，建议轮转）" if lsz >= 50 * 1024 * 1024 or vsz >= 200 * 1024 * 1024 else "")))
    # 配置软链完好性（收编后：真身在 ~/netops/config/，外面是软链；软链断了 = 功能会坏）
    links = [
        (pathlib.Path.home()/".pi/commands.json", ROOT/"config/pi-commands.json"),
        (pathlib.Path.home()/"AGENTS.md", ROOT/"config/AGENTS.workspace.md"),
        (pathlib.Path.home()/".zsh/completions/_netdev", ROOT/"config/_netdev"),
        (pathlib.Path.home()/".bash_completion.d/netdev", ROOT/"config/netdev.bash"),
        (ROOT/"devices.toml", ROOT/"config/devices.toml"),
        (ROOT/"connections.json", ROOT/"config/connections.json"),
        (ROOT/"state", ROOT/"config/state"),
    ]
    # 真身在 config/ 下，**外面那几条软链是安装包才建的**。
    # 源码安装（git clone）压根不会有，所以"缺软链"对 clone 用户不是故障 ——
    # 实测：全新克隆后这一项恒红，而功能其实全好（2026-10-03 修）。
    # 所以只把「真身缺失」当问题；软链缺失降级为提示。
    missing_real = [_P.rel_to_home(t) for l, t in links if not t.exists()]
    missing_link = [_P.rel_to_home(l) for l, t in links if t.exists() and not l.exists()]
    if missing_real:
        rows.append(("配置真身", False,
                     f"缺：{', '.join(missing_real)} → 从模板生成："
                     f"cp config/<同名>.example config/<同名>"))
    elif missing_link:
        rows.append(("配置软链", True,
                     f"{len(links) - len(missing_link)}/{len(links)} 条正常；"
                     f"另有 {len(missing_link)} 条外部软链没建（源码安装属正常，要建跑 netdev web repair）"))
    else:
        rows.append(("配置软链", True, f"{len(links)}/{len(links)} 条正常（真身都在 config/）"))
    # 写操作人审：策略 + 最近一次裁决 + 审批代码指纹（防止被悄悄改掉）
    try:
        import hashlib
        cur = approval.policy()
        ap = ROOT / "lib/approval.py"
        h = hashlib.sha256(ap.read_bytes()).hexdigest()[:12] if ap.exists() else "缺失"
        base_f = ROOT / "state/approval.baseline.sha256"
        if not base_f.exists():
            base_f.parent.mkdir(parents=True, exist_ok=True)
            base_f.write_text(h + "\n")
        base = base_f.read_text().strip()
        last = ""
        alog = ROOT / "logs/approvals.log"
        if alog.exists():
            lines = [x for x in alog.read_text(errors="ignore").splitlines() if x.strip()]
            if lines:
                f = lines[-1].split()
                last = f" 最近: {f[1]} {f[0].split()[1] if len(f[0].split()) > 1 else ''}"
        mismatch_note = ""
        if h != base:
            mismatch_note = " ⚠ 与基线不一致！→ 若确认合法，重设：netdev doctor --rebless"
        rows.append(("写操作人审", cur == "ask" and h == base,
                     f"策略={cur} · 审批码指纹={h}" + ("（与基线一致）" if h == base else mismatch_note) + last))
    except Exception as e:
        rows.append(("写操作人审", False, f"检查失败：{type(e).__name__}"))

    lc = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    la_dir = pathlib.Path.home() / "Library/LaunchAgents"
    la_ui = la_dir / "com.netdev.ui.plist"
    loaded = UI_LABEL in lc
    installed = la_ui.exists()
    up = _uh["up"]
    # 这一项问的是「重启后 :8898 会不会自己回来」，属部署选择，不是缺陷：
    # 服务正在跑就不判失败，只如实说明重启后的行为。
    if loaded:
        rows.append(("开机自启", True, "已装且已加载 —— 登录后会自动回来"))
    elif installed:
        rows.append(("开机自启", up,
                     "已装未加载 —— 现在服务在跑，但重启后不会自动回来"
                     " → 想自动：netdev ui install（本机 launchctl bootstrap 常被安全策略拦，拦了也不影响日常用 netdev ui）"
                     if up else "已装未加载，且服务没在跑 → netdev ui start"))
    else:
        rows.append(("开机自启", up,
                     "未装 —— 重启后需手动 netdev ui start（想自动：netdev ui install）"
                     if up else "未装，且服务没在跑 → netdev ui start"))

    # pi-web-ui 前端小补丁：左栏按钮“作用在当前选中的终端”（否则会按标题复用同一个终端）
    patcher = ROOT / "tools/piweb_patch.py"
    if patcher.exists():
        pr = subprocess.run([sys.executable, str(patcher), "--check"], capture_output=True, text=True)
        note = (pr.stdout or pr.stderr).strip()
        rows.append(("左栏按钮补丁", pr.returncode == 0,
                     note + ("" if pr.returncode == 0 else "  → 重打：netdev web patch（或 python3 tools/piweb_patch.py）")))
    return rows


# ─────────────────────────────────────────────────────────── 网页服务生命周期
def _ui_pid() -> int:
    try:
        return int(UI_PIDFILE.read_text().strip())
    except Exception:
        return 0


def _ui_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ui_port_open() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.8)
        return s.connect_ex((UI_HOST, UI_PORT)) == 0


def _ui_health(timeout: float = 3.0) -> dict:
    """问一次 /api/health。up 以「HTTP 真的答了」为准，不看 PID 文件。"""
    d = {"up": False, "pid": _ui_pid(), "port_open": False, "detail": ""}
    try:
        with urllib.request.urlopen(UI_BASE + "/api/health", timeout=timeout) as r:
            body = r.read(300).decode("utf-8", "replace")
            d["up"] = r.status == 200 and '"ok": true' in body.replace("'", '"')
            d["detail"] = body.strip()
    except Exception as e:
        d["detail"] = f"{type(e).__name__}"
    d["port_open"] = _ui_port_open()
    return d


def _ui_daemon(*args: str) -> tuple[int, str]:
    """调 ui/daemonize.py —— 唯一可靠的启动路径（double-fork + setsid，脱离终端）。
    返回 (rc, 输出文本)。输出被捕获，由调用方决定怎么展示（避免和 CLI 的提示重复两遍）。

    为什么不用 nohup / launchctl：
      · nohup &：仍留在当前进程组，终端一关就被连坐收掉；
      · launchctl bootstrap：本机安全策略会拦（实测 5: Input/output error）。
    """
    if not UI_DAEMON.exists():
        return 2, f"找不到 {UI_DAEMON}（该文件负责让服务脱离终端后台运行，缺失说明安装不完整）"
    p = subprocess.run([sys.executable, str(UI_DAEMON),
                        "--port", str(UI_PORT), "--host", UI_HOST,
                        "--pidfile", str(UI_PIDFILE), "--logfile", str(UI_LOGFILE), *args],
                       capture_output=True, text=True)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _ui_kill_orphan() -> None:
    """端口被占但 PID 文件对不上（旧实例 / 手工起的前台进程）时，按端口找出来收掉。"""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run([lsof, "-nP", f"-iTCP:{UI_PORT}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=6).stdout.split()
    except Exception:
        return
    for s in out:
        if s.isdigit() and int(s) != os.getpid():
            try:
                os.kill(int(s), signal.SIGTERM)
                print(f"{C['dim']}   已停掉占用 {UI_PORT} 的旧进程 PID {s}{C['reset']}")
            except OSError:
                pass
    time.sleep(0.6)
    try:
        UI_PIDFILE.unlink()
    except Exception:
        pass


_UI_PLIST_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{server}</string>
        <string>--port</string>
        <string>{port}</string>
        <string>--host</string>
        <string>{host}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def _ui_status_lines() -> list[tuple[bool, str]]:
    h = _ui_health()
    rows: list[tuple[bool, str]] = []
    detail = f"HTTP 200 {h['detail']}" if h["up"] else f"不通（{h['detail']}）"
    rows.append((h["up"], f"{'服务应答':<8}{UI_BASE}/api/health  {detail}"))
    rows.append((not h["port_open"] or h["up"], f"{'端口':<8}{UI_PORT}  {'已监听' if h['port_open'] else '未监听'}"))
    pid, pid_ok = h["pid"], _ui_alive(h["pid"])
    if pid_ok:
        pid_note = f"{pid}（存活）"
    elif not pid:
        pid_note = "无 PID 文件"
    else:
        pid_note = f"{pid}（已死 —— PID 文件是残留）"
    rows.append((pid_ok or h["up"], f"{'进程':<8}{pid_note}"))
    if UI_PLIST.exists():
        loaded = UI_LABEL in subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        rows.append((loaded, f"{'开机自启':<7}{'已装已加载' if loaded else '已装未加载'}（{UI_PLIST.name}）"))
    else:
        rows.append((True, f"{'开机自启':<7}未装（想装：netdev ui install）"))
    return rows


def cmd_ui(a):
    act = getattr(a, "action", None) or "ensure"

    if act == "status":
        rows = _ui_status_lines()
        print(f"{C['bold']}netdev-ui 状态（{UI_BASE}）{C['reset']}")
        for ok, note in rows:
            print(f"  {C['grn']}✔{C['reset']} {note}" if ok else f"  {C['yel']}!{C['reset']} {note}")
        return 0 if _ui_health()["up"] else 1

    if act in ("ensure", "start"):
        h = _ui_health()
        if h["up"]:
            print(f"{C['grn']}✔{C['reset']} 服务已在运行  {UI_BASE}   PID {h['pid'] or '（未记 PID）'}")
            if act == "ensure":
                print(f"{C['dim']}  日志：{UI_LOGFILE}{C['reset']}")
            return 0
        if h["port_open"] and not h["up"]:
            print(f"{C['yel']}!{C['reset']} 端口 {UI_PORT} 被一个不健康/来路不明的进程占着，先清掉它")
            _ui_kill_orphan()
        rc, out = _ui_daemon()
        if rc == 0:
            h = _ui_health(6.0)
            print(f"{C['grn']}✔{C['reset']} 已启动  {UI_BASE}   PID {h['pid'] or '（未记 PID）'}")
            print(f"{C['dim']}  日志：{UI_LOGFILE}{C['reset']}")
            print(f"{C['dim']}  已脱离终端会话 —— 关掉终端/启动器窗口都不受影响；停止：netdev ui stop{C['reset']}")
        else:
            print(f"{C['red']}✘ 启动失败{C['reset']}")
            if out:
                print(f"{C['dim']}{out}{C['reset']}")
            print(f"{C['dim']}  日志：{UI_LOGFILE}{C['reset']}")
        return rc

    if act == "stop":
        h = _ui_health()
        if not h["port_open"] and not _ui_alive(h["pid"]):
            print(f"{C['dim']}服务本来就没在跑{C['reset']}")
            return 0
        rc, _out = _ui_daemon("--stop")
        if _ui_port_open():
            _ui_kill_orphan()
        if _ui_port_open():
            print(f"{C['yel']}!{C['reset']} 端口 {UI_PORT} 仍被占用，请手动查：lsof -nP -iTCP:{UI_PORT}")
        else:
            print(f"{C['grn']}✔{C['reset']} 已停止")
        return rc

    if act == "restart":
        cmd_ui(argparse.Namespace(action="stop"))
        time.sleep(0.5)
        return cmd_ui(argparse.Namespace(action="start"))

    if act == "log":
        n = getattr(a, "lines", 40) or 40
        if not UI_LOGFILE.exists():
            print(f"{C['dim']}暂无日志：{UI_LOGFILE}{C['reset']}")
            return 1
        tail = UI_LOGFILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        print(f"{C['bold']}{UI_LOGFILE}{C['reset']}（末 {len(tail)} 行）")
        for ln in tail:
            print("  " + ln)
        return 0

    if act == "open":
        rc = cmd_ui(argparse.Namespace(action="ensure"))
        if rc == 0:
            import webbrowser
            webbrowser.open(UI_BASE)
            print(f"{C['dim']}  已在浏览器打开 {UI_BASE}{C['reset']}")
        return rc

    if act == "install":
        python = ROOT / ".venv/bin/python"
        if not python.exists():
            python = pathlib.Path(sys.executable)
        UI_PLIST.parent.mkdir(parents=True, exist_ok=True)
        UI_PLIST.write_text(_UI_PLIST_TPL.format(
            label=UI_LABEL, python=python, server=ROOT / "ui/server.py",
            port=UI_PORT, host=UI_HOST, root=ROOT, log=UI_LOGFILE))
        print(f"{C['grn']}✔{C['reset']} 已写入 {UI_PLIST}")
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(UI_PLIST)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"{C['grn']}✔{C['reset']} 已加载，登录后 :{UI_PORT} 会自动起来")
            return 0
        err = (r.stderr or r.stdout).strip()
        print(f"{C['yel']}!{C['reset']} launchctl 加载失败：{err}")
        print(f"{C['dim']}  本机安全策略常拦 bootstrap —— 不影响使用：{C['reset']}")
        print(f"{C['dim']}  日常用 `netdev ui`（没有就起、有就报状态），它不依赖 launchd。{C['reset']}")
        return 0

    if act == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{UI_LABEL}"], capture_output=True, text=True)
        if UI_PLIST.exists():
            UI_PLIST.unlink()
            print(f"{C['grn']}✔{C['reset']} 已移除 {UI_PLIST}")
        else:
            print(f"{C['dim']}本来就没装{C['reset']}")
        return 0

    print(f"{C['red']}未知动作：{act}{C['reset']}")
    return 2


def cmd_doctor(a):
    if getattr(a, "rebless", False):
        import hashlib
        ap = ROOT / "lib/approval.py"
        if not ap.exists():
            print(f"{C['red']}✘ 找不到审批代码 {ap}，无法重设基线{C['reset']}")
            return 2
        h = hashlib.sha256(ap.read_bytes()).hexdigest()[:12]
        base_f = ROOT / "state/approval.baseline.sha256"
        base_f.parent.mkdir(parents=True, exist_ok=True)
        old = base_f.read_text().strip() if base_f.exists() else "(无)"
        base_f.write_text(h + "\n")
        print(f"{C['bold']}审批代码篡改检测基线已重设{C['reset']}")
        print(f"  旧基线：{old}")
        print(f"  新基线：{h}")
        print(f"  文件：{base_f}")
        print(f"{C['dim']}注意：请确认 approval.py 的变更是合法的，否则不应重设。{C['reset']}")
        return 0

    rows = _doctor_rows()
    bad = [r for r in rows if not r[1]]
    print(f"{C['bold']}netdev 体检（{len(rows) - len(bad)}/{len(rows)} 通过）{C['reset']}")
    for name, ok, note in rows:
        mark = f"{C['grn']}✔{C['reset']}" if ok else f"{C['yel']}!{C['reset']}"
        print(f"  {mark} {name:<20}{note}")
    if bad:
        print(f"\n{C['dim']}有问题项：{', '.join(r[0] for r in bad)}{C['reset']}")
        print(f"{C['dim']}自动修复能修的：netdev web repair{C['reset']}")
    return 0


def cmd_web(a):
    act = a.action
    f = _web_path(getattr(a, "cwd", None))
    data, warn = _web_load(f)
    if warn:
        print(f"{C['yel']}⚠ {warn}{C['reset']}")
    names = {n for n, _ in WEB_CMDS} | WEB_CMDS_LEGACY
    have = {c.get("name") for c in data["commands"]}

    def _old_device_buttons():
        """历史生成的“↗ 接 xx”按钮（每次 install/repair 都重算）"""
        return {c.get("name") for c in data["commands"]
                if str(c.get("name", "")).startswith(_DEV_PREFIXES)}

    if act in ("install", "repair"):
        if getattr(a, "device_buttons", None) is not None:
            _web_set_device_buttons(a.device_buttons)
            print(f"{C['grn']}✔ 设备按钮：{'生成' if a.device_buttons else '不生成'}{C['reset']}"
                  f"  {C['dim']}已记入 {WEB_CONF.relative_to(ROOT)}，以后 install/repair 都照此{C['reset']}")
        drop = names | _old_device_buttons()
        data["commands"] = [c for c in data["commands"] if c.get("name") not in drop] + _web_entries()
        bak = _web_write(f, data)
        ndev = sum(1 for c in data["commands"] if str(c.get("name", "")).startswith(DEV_CMD_PREFIX))
        tail = f" + {ndev} 条设备接入按钮" if ndev else "，无设备按钮（多终端仍可用「快速接入」）"
        print(f"{C['grn']}✔ 已写入 {len(data['commands'])} 条命令"
              f"（{len(WEB_CMDS)} 条功能{tail}）{C['reset']} → {f}")
        if bak:
            print(f"  {C['dim']}原文件备份：{bak}{C['reset']}")
        print(f"  {C['dim']}解释器：{_web_python()}（换 venv 也不影响；坏了跑 netdev web repair）{C['reset']}")
        print(f"  {C['dim']}在 pi-web-ui 里：终端页 → 左栏「命令」区（或点 ⟳ 重新读取）{C['reset']}")
        return 0

    if act == "patch":
        # pi-web-ui 前端小补丁：左栏按钮“作用在当前选中的终端”（app 升级后会丢，跑这个重打）
        patcher = ROOT / "tools/piweb_patch.py"
        if not patcher.exists():
            raise SystemExit(f"✘ 找不到 {patcher}")
        return subprocess.run([sys.executable, str(patcher), *(["--revert"] if getattr(a, "revert", False) else [])]).returncode

    if act == "uninstall":
        left = [c for c in data["commands"] if c.get("name") not in names]
        data["commands"] = left
        bak = _web_write(f, data)
        print(f"{C['grn']}✔ 已移除接入命令{C['reset']}（剩余 {len(left)} 条）{f}")
        if bak:
            print(f"  {C['dim']}备份：{bak}{C['reset']}")
        return 0

    # check
    missing = [n for n, _ in WEB_CMDS if n not in have]
    stale = []
    for c in data["commands"]:
        if c.get("name") in names:
            cmd = str(c.get("command") or "")
            parts = cmd.split()
            prog = parts[0] if parts else ""
            if prog and not pathlib.Path(prog).exists():
                stale.append(f"{c['name']} → 解释器不存在：{prog}")
                continue
            # ★ 2026-10-01 补：原来只校验解释器，**脚本路径压根没查** ——
            #   于是「装到非 ~/netops 时按钮指向不存在的 quick_conn.py」这种
            #   装完看着成功、点开才发现不能用的坑，体检永远报 ✔。
            #   现在把路径型参数（去掉 ~ 展开）也一并存在性校验。
            for tok in parts[1:]:
                if tok.startswith("-") or not ("/" in tok):
                    continue
                p = pathlib.Path(tok.replace("~", str(pathlib.Path.home()), 1))
                if not p.exists():
                    stale.append(f"{c['name']} → 脚本不存在：{tok}")
                    break
    print(f"{C['bold']}接入命令体检{C['reset']} · {f}")
    print(f"  设备按钮：{'生成' if _web_device_buttons_enabled() else '不生成（只留功能按钮）'}"
          f"　{C['dim']}改法：netdev web install --no-device-buttons / --device-buttons{C['reset']}")
    print(f"  命令清单：{len(have & names)}/{len(WEB_CMDS)} 条" + (f"　{C['yel']}缺：{', '.join(missing)}{C['reset']}" if missing else f"　{C['grn']}✔{C['reset']}"))
    for s in stale:
        print(f"  {C['yel']}! {s}{C['reset']}")
    for path, label in ((ROOT / "tools/quick_conn.py", "接入脚本"), (ROOT / "netdev", "netdev 入口")): 
        print(f"  {label}：{'✔' if path.exists() else '✖'} {path}")
    if missing or stale:
        print(f"  {C['dim']}修法：netdev web repair{C['reset']}")
    return 0


def cmd_mcp(a):
    os_exec = pathlib.Path(__file__).with_name("netdev_mcp.py")
    os.execv(sys.executable, [sys.executable, str(os_exec)])
    return 0


# ─────────────────────────────────────────────────────────────── 入口
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 首次运行：从 config/*.example 补齐派生配置（只补缺的，不覆盖已有的）。
    # 源码安装（git clone）没有 install.sh 那一步播种，全新克隆后这些文件不存在，
    # doctor 会恒红（实测）。放在最前面，任何子命令都先做这一步。
    _made = _P.bootstrap()
    if _made and argv and argv[0] not in ("doctor", "--help", "-h"):
        print(f"{C['dim']}[首次运行] 已从模板生成：{'、'.join(_made)}{C['reset']}")
    # 快捷写法：`netdev <设备名>` 等同 `netdev attach <设备名>`（少敲几个字，多终端时好用）
    if argv and not argv[0].startswith("-"):
        try:
            _first = argv[0]
            _known = _first in ("list", "run", "apply", "snap", "shell", "attach", "screen-ls", "screen-read",
                                "screen-send", "backup", "save", "snap", "login", "serial-discover", "identify",
                                "conn", "web", "doctor", "policy", "serve", "logs", "ping", "mcp")
            if not _known:
                if _tmux("has-session", "-t", "netops").returncode == 0 and \
                        _first in _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split():
                    argv = ["attach", _first] + argv[1:]
                elif _first in engine.load_devices():
                    argv = ["attach", _first] + argv[1:]
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="netdev", description="网络设备调试通道")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="列出设备与镜像")
    lp.add_argument("--json", action="store_true", help="机器可读输出（给网页/AI 用）")
    lp.set_defaults(fn=cmd_list)

    r = sub.add_parser("run", help="执行只读命令")
    r.add_argument("device"); r.add_argument("commands", nargs="+")
    r.set_defaults(fn=cmd_run)

    ap = sub.add_parser("apply", help="下发配置（备份→确认→逐条→save）")
    ap.add_argument("device"); ap.add_argument("--cmd", action="append")
    ap.add_argument("--file"); ap.add_argument("--yes", action="store_true")
    ap.add_argument("--no-save", action="store_true"); ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--verify", action="append")
    ap.set_defaults(fn=cmd_apply)

    sv = sub.add_parser("save", help="落盘"); sv.add_argument("device")
    sv.add_argument("--yes", action="store_true"); sv.set_defaults(fn=cmd_save)

    bk = sub.add_parser("backup", help="备份运行/flash 配置"); bk.add_argument("device")
    bk.set_defaults(fn=cmd_backup)

    df = sub.add_parser("diff", help="两份配置对比"); df.add_argument("file_a"); df.add_argument("file_b")
    df.set_defaults(fn=cmd_diff)

    pg = sub.add_parser("ping", help="设备侧 ping")
    pg.add_argument("device"); pg.add_argument("target")
    pg.add_argument("--source"); pg.add_argument("--count", type=int, default=5)
    pg.set_defaults(fn=cmd_ping)

    cn = sub.add_parser("conn", help="连接簿：SSH/Telnet/串口 接入清单（含端口管理）")
    cn.add_argument("action", choices=["list", "add", "rm", "connect", "promote"])
    cn.add_argument("proto_or_key", nargs="?")
    cn.add_argument("host", nargs="?")
    cn.add_argument("--port", type=int)
    cn.add_argument("-u", "--username", default="")
    cn.add_argument("--name", default="")
    cn.add_argument("--device", help="串口设备路径")
    cn.add_argument("--baud", type=int, default=9600)
    cn.add_argument("--note", default="")
    cn.add_argument("--platform", default="huawei_vrp")
    cn.add_argument("--json", action="store_true", help="机器可读输出（给网页/AI 用）")
    cn.set_defaults(fn=cmd_conn)

    tn = sub.add_parser("telnet", help="临时 Telnet 同屏会话（不用登记设备）")
    tn.add_argument("host"); tn.add_argument("port", nargs="?", type=int)
    tn.add_argument("-u", "--username", default="")
    tn.set_defaults(fn=cmd_quick_shell, proto="telnet")

    qs = sub.add_parser("ssh", help="临时 SSH 同屏会话（不用登记设备）")
    qs.add_argument("target", help="[user@]host[:port]")
    qs.set_defaults(fn=cmd_quick_shell, proto="ssh")

    sh = sub.add_parser("shell", help="交互式会话（tmux 承载，可人机同屏）")
    sh.add_argument("device", help="设备名，或 telnet://… / ssh://… / serial:/dev/…")
    sh.add_argument("--restart", action="store_true",
                    help="窗口已存在也强制重启里面的命令（如把旧的直连 ssh 换成带退格适配的 ssh 桥）")
    sh.set_defaults(fn=cmd_shell)

    at = sub.add_parser("attach", help="把设备窗格接到“当前这个终端”自己的会话（多终端互不抢屏）")
    at.add_argument("window", help="设备名（netops 里的窗口名），也接受 netops:<窗口名>")
    at.set_defaults(fn=cmd_attach)

    sl = sub.add_parser("screen-ls", help="列出人机同屏会话")
    sl.add_argument("--json", action="store_true", help="机器可读输出（给网页/AI 用）")
    sl.set_defaults(fn=cmd_screen_ls)

    ss = sub.add_parser("screen-send", help="往同屏会话发命令（AI 用；你在屏幕上看得见）")
    ss.add_argument("--yes", action="store_true", help="确认直接发送写操作（默认会被拦下，建议改用 apply）")
    ss.add_argument("device"); ss.add_argument("text")
    ss.add_argument("--no-enter", dest="no_enter", action="store_true", default=False)
    ss.add_argument("--wait", type=float, default=1.5, help="发完等几秒再读屏")
    ss.add_argument("--lines", type=int, default=40)
    ss.set_defaults(fn=cmd_screen_send)

    sr = sub.add_parser("screen-read", help="读同屏会话当前屏幕")
    sr.add_argument("device"); sr.add_argument("--lines", type=int, default=40)
    sr.set_defaults(fn=cmd_screen_read)

    w = sub.add_parser("watch", help="实时镜像跟读")
    w.add_argument("device", nargs="?"); w.add_argument("--lines", type=int, default=40)
    w.add_argument("--no-follow", action="store_true"); w.set_defaults(fn=cmd_watch)

    sub.add_parser("serial-discover", help="发现串口设备（含 USB 适配器信息）").set_defaults(fn=cmd_serial_discover)

    da = sub.add_parser("device-add", help="往设备清单里加一台设备（自动备份+校验）")
    da.add_argument("--name", required=True, help="设备名（清单里唯一，之后 netdev run <名> 用它）")
    da.add_argument("--protocol", default="ssh", choices=["ssh", "telnet", "serial"])
    da.add_argument("--host", help="IP 或域名（ssh/telnet 必填）")
    da.add_argument("--port", type=int, help="端口（默认 ssh=22 / telnet=23）")
    da.add_argument("--device-port", dest="device_port", help="串口路径，如 /dev/cu.usbserial-XXXX")
    da.add_argument("--auto", action="store_true", help="串口：自动发现设备")
    da.add_argument("--baud", type=int, default=9600)
    da.add_argument("--username", help="登录用户名（可留空，串口登录时会问）")
    da.add_argument("--platform", default="huawei_vrp",
                    help="netmiko 平台：huawei_vrp / h3c_comware / ruijie_os / cisco_ios …")
    da.add_argument("--esn", help="已知 ESN（用于 identify 身份比对）")
    da.add_argument("--tags", help="标签，英文逗号分隔，如 机房,核心交换机")
    da.add_argument("--file", help="指定清单文件（默认 ~/netops/devices.toml）")
    da.add_argument("--dry-run", dest="dry_run", action="store_true", help="只打印将写入的内容，不落盘")
    da.set_defaults(fn=cmd_device_add)

    kk = sub.add_parser("keys", help="查看/自动探测退格键适配模式")
    kk.add_argument("device")
    kk.add_argument("--redo", action="store_true", help="重新探测")
    kk.add_argument("--set", choices=["auto", "bs", "del", "pass"], help="人工强制指定")
    kk.set_defaults(fn=cmd_keys)

    idf = sub.add_parser("identify", help="读设备 ESN/型号，确认真机身份")
    idf.add_argument("device"); idf.set_defaults(fn=cmd_identify)

    cm = sub.add_parser("cmds", help="厂商常用命令速查")
    cm.add_argument("vendor", nargs="?", default="huawei")
    cm.set_defaults(fn=cmd_cmds)

    ht = sub.add_parser("hint", help="向设备要补全提示（设备的 '?' 帮助）")
    ht.add_argument("device"); ht.add_argument("prefix", nargs="?", default="")
    ht.set_defaults(fn=cmd_hint)

    lg = sub.add_parser("login", help="登录并（默认）把用户名+密码存入本地凭据文件（~/.netops/credentials.json，权限600）")
    lg.add_argument("device")
    lg.add_argument("--no-store", dest="no_store", action="store_true", default=False)
    lg.add_argument("--probe-only", dest="probe_only", action="store_true", default=False,
                    help="只问设备要什么（不弹框、不输密码）")
    lg.add_argument("--retries", type=int, default=2, help="串口登录重试次数（默认 2，避免触发设备锁定）")
    lg.set_defaults(fn=cmd_login)

    ob = sub.add_parser("onboard", help="新设备上架清点"); ob.add_argument("device")
    ob.add_argument("--yes", action="store_true"); ob.set_defaults(fn=cmd_onboard)

    sub.add_parser("selftest", help="离线端到端自检").set_defaults(fn=cmd_selftest)

    wb = sub.add_parser("web", help="把「接入命令」装进 pi-web-ui 终端页（幂等安装 / 体检 / 自愈 / 卸载）")
    wb.add_argument("action", choices=["install", "check", "repair", "uninstall", "patch"])
    wb.add_argument("--cwd", help="目标工作目录（默认 ~ ；命令清单是 per-workspace 的）")
    wb.add_argument("--json", action="store_true")
    wb.add_argument("--device-buttons", dest="device_buttons", action="store_true", default=None,
                    help="给每台设备生成一个「↗ 接 xx」按钮（默认开启）")
    wb.add_argument("--no-device-buttons", dest="device_buttons", action="store_false",
                    help="不生成设备按钮：左栏只留功能按钮（多终端接入不受影响）")
    wb.set_defaults(fn=cmd_web)

    doc = sub.add_parser("doctor", help="一键体检：依赖 / 服务 / 串口 / 命令清单 / 日志")
    doc.add_argument("--rebless", action="store_true",
                     help="重新生成 approval.py 的篡改检测基线（仅在确认合法变更后使用）")
    doc.set_defaults(fn=cmd_doctor)

    uip = sub.add_parser("ui", help="网页服务：没有就起、有就报状态（start/stop/restart/status/log/open/install/uninstall）")
    uip.add_argument("action", nargs="?", default="ensure",
                     choices=["ensure", "start", "stop", "restart", "status", "log", "open", "install", "uninstall"],
                     help="默认 ensure：确保服务在跑（起不来是历史高频故障，所以默认就帮你拉起）")
    uip.add_argument("-n", "--lines", type=int, default=40, help="log 动作显示末几行（默认 40）")
    uip.set_defaults(fn=cmd_ui)

    rs = sub.add_parser("resume", help="看/重建上次接入过的目标（重启后一键恢复同屏会话）")
    rs.add_argument("--all", action="store_true", help="把当前已断开的全部重建")
    rs.add_argument("--last", action="store_true", help="只重建最近一条（等同 --all 的最后一个）")
    rs.set_defaults(fn=cmd_resume)

    sn = sub.add_parser("snap", help="配置快照：备份 / 对比 / 恢复（接客户设备标准动作）")
    sn.add_argument("action", nargs="?", default="list", choices=["save", "list", "import", "diff", "restore", "rm", "trash", "unrm", "purge"])
    sn.add_argument("--force-danger", action="store_true",
                    help="跳过「防误删保护」强制执行还原计划（仅在确认计划无误时用，风险自负）")
    sn.add_argument("device", nargs="?")
    sn.add_argument("--tag", default="", help="标签（客户名 / 阶段）")
    sn.add_argument("--note", default="", help="备注")
    sn.add_argument("--from", dest="from_", default="", help="用哪份快照（默认最新）")
    sn.add_argument("--file", help="import 时的 cfg 文件")
    sn.add_argument("--device", dest="dev_name", help="import 时的设备名（也可用位置参数）")
    sn.add_argument("--apply", action="store_true", help="真的下发（默认只预览）")
    sn.add_argument("--only-add", action="store_true", help="只补回缺失，不撤销你新增的配置")
    sn.add_argument("--purge", action="store_true", help="rm 时物理删除（破坏性，需配合 --yes）")
    sn.add_argument("refs", nargs="*", help="rm 时可给多个编号/ID片段")
    sn.add_argument("--yes", action="store_true", help="二次确认（写操作必须）")
    sn.add_argument("--json", action="store_true", help="机器可读输出（给脚本/AI 用）")
    sn.add_argument("--undo-extra", action="store_true", help='同时撤销“多出的行”（默认只提示）')
    sn.set_defaults(fn=cmd_snap)

    lg = sub.add_parser("logs", help="日志：状态 / 轮转 / 定时轮转（只搬不删）")
    lg.add_argument("action", nargs="?", default="status", choices=["status", "rotate", "install-agent", "uninstall-agent"])
    lg.add_argument("--max-mb", type=int, default=20, help="pi-web-ui.log 阈值（MB，默认 20）")
    lg.set_defaults(fn=cmd_logs)

    pl = sub.add_parser("policy", help="写操作人审策略：show/ask/allow（放宽需人批）")
    pl.add_argument("action", nargs="?", default="show", choices=["show", "readonly", "ask", "allow"])
    pl.add_argument("--minutes", type=int, help="放行模式的 TTL（分钟），到期自动回落确认模式")
    pl.add_argument("--json", action="store_true", help="机器可读（给网页/AI 用）")
    pl.set_defaults(fn=cmd_policy)
    sub.add_parser("mcp", help="以 MCP stdio 服务器方式运行").set_defaults(fn=cmd_mcp)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PaneBusy as e:        # 同屏会话没回到提示符：给人话，不要甩 traceback
        print(f"{C['red']}✘ 同屏会话没能在超时内回到提示符：{e}{C['reset']}")
        print(f"{C['dim']}   常见原因：设备停在 `---- More ----` 分页、或输出太长。"
              f"分页已自动翻完，重试一次即可；仍失败就 netdev shell <设备> 重连。{C['reset']}")
        sys.exit(1)
