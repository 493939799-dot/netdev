#!/usr/bin/env python3
"""netdev MCP server（stdio, JSON-RPC 2.0 / MCP 2024-11-05）。

工具：netdev_list / netdev_run / netdev_apply / netdev_save / netdev_backup /
      netdev_diff / netdev_ping / netdev_serial_run / netdev_watch_tail / netdev_connect_info

安全：本服务器只接受**显式 confirmed=true** 的写操作；黑名单命令一律拒绝。
"""
from __future__ import annotations

import importlib
import subprocess
import os
import json
import pathlib
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
CLI = str(ROOT / "netdev")
LIB_DIR = ROOT / "lib"
sys.path.insert(0, str(ROOT))

from lib import approval, creds, engine, gates, mirror  # noqa: E402

PROTOCOL = "2024-11-05"
SERVER = {"name": "netdev", "version": "1.0.0"}

# MCP 服务器“自我介绍”：客户端握手时会把它交给 AI，让 AI 知道本服务器是干什么的、
# 什么时候该用、有什么硬规矩。（pi 侧由 pi-mcp-adapter 读取并在 AI 查 netdev 时展示）
INSTRUCTIONS = (
    "netdev —— 网络设备调试通道（华为 / H3C / 锐捷 / 思科；SSH / Telnet / 串口 Console）。"
    "\n\n什么时候用：用户提到设备、路由器、交换机、串口 / Console，或说接入 / 连一下 / 看设备 /"
    "看屏幕 / 回看历史 / 抓配置 / 下发配置 / 备份 / 快照 / 恢复快照 / 连接簿 / ESN 时，"
    "就用本服务器的工具，不要自己拼 ssh / telnet / screen 裸连。"
    "\n\n怎么用：先 netdev_list 看有哪些设备（以及哪些已有同屏会话），再决定目标。"
    "只读用 netdev_run；写操作优先用 netdev_apply（会先备份→逐条→校验），"
    "不要用 netdev_screen_send 直接灌配置。"
    "\n\n硬规矩：黑名单命令（reload / format / delete / reset saved-configuration）永不执行，"
    "连询问都没有；所有写操作都需要显式 confirmed=true，并且真机上还会弹原生弹窗要人点允许（AI 点不了）。"
    "\n\n更细的规则见 ~/netops/config/AGENTS.workspace.md。"
)

TOOLS = [
    {"name": "netdev_list", "description": "列出已配置设备与最近镜像流",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "netdev_run", "description": "在设备上执行只读命令（display/dir/ping 等），返回原始回显",
     "inputSchema": {"type": "object", "required": ["device", "commands"], "properties": {
         "device": {"type": "string"}, "commands": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "netdev_connect_info", "description": "连接并返回设备基本信息（型号/版本/提示符）",
     "inputSchema": {"type": "object", "required": ["device"], "properties": {"device": {"type": "string"}}}},
    {"name": "netdev_apply", "description": "下发配置：强制先备份→逐条下发→校验→可选 save。必须 confirmed=true",
     "inputSchema": {"type": "object", "required": ["device", "commands", "confirmed"], "properties": {
         "device": {"type": "string"}, "commands": {"type": "array", "items": {"type": "string"}},
         "confirmed": {"type": "boolean"}, "save": {"type": "boolean", "default": True},
         "verify": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "netdev_save", "description": "落盘（save vrpcfg.zip）。必须 confirmed=true",
     "inputSchema": {"type": "object", "required": ["device", "confirmed"], "properties": {
         "device": {"type": "string"}, "confirmed": {"type": "boolean"}}}},
    {"name": "netdev_backup", "description": "抓取运行配置与 flash 配置并存盘",
     "inputSchema": {"type": "object", "required": ["device"], "properties": {"device": {"type": "string"}}}},
    {"name": "netdev_diff", "description": "两份配置文件的差异",
     "inputSchema": {"type": "object", "required": ["file_a", "file_b"], "properties": {
         "file_a": {"type": "string"}, "file_b": {"type": "string"}}}},
    {"name": "netdev_ping", "description": "设备侧 ping（可指定源接口）",
     "inputSchema": {"type": "object", "required": ["device", "target"], "properties": {
         "device": {"type": "string"}, "target": {"type": "string"},
         "source": {"type": "string"}, "count": {"type": "integer", "default": 5}}}},
      {"name": "netdev_serial_run", "description": "串口通道执行命令（无 IP 设备 / 救砖）",
       "inputSchema": {"type": "object", "required": ["command", "device"], "properties": {
           "command": {"type": "string"},
           "device": {"type": "string", "description": "设备名，用 netdev list 查"}}}},
    {"name": "netdev_watch_tail", "description": "读取实时镜像流尾部（观察层②的一次性快照）",
     "inputSchema": {"type": "object", "properties": {
         "device": {"type": "string"}, "lines": {"type": "integer", "default": 40}}}},
    {"name": "netdev_screen_list", "description": "列出人机同屏（tmux）会话 —— 人可 attach 到同一块屏",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "netdev_screen_send", "description": "往人机同屏会话里发命令（人在屏幕上实时看到 AI 敲的字）。⚠ 含写操作的文本必须 confirmed=true；改配置优先用 netdev_apply（会先备份+逐条校验）",
     "inputSchema": {"type": "object", "required": ["device", "text"], "properties": {
         "device": {"type": "string"}, "text": {"type": "string"},
         "enter": {"type": "boolean", "default": True},
         "wait": {"type": "number", "default": 1.5},
         "lines": {"type": "integer", "default": 40},
         "confirmed": {"type": "boolean", "default": False,
                       "description": "写操作确认；只读/视图导航不需要"}}}},
    {"name": "netdev_screen_read", "description": "读同屏会话当前屏幕（人与 AI 共享同一块屏）",
     "inputSchema": {"type": "object", "required": ["device"], "properties": {
         "device": {"type": "string"}, "lines": {"type": "integer", "default": 40}}}},
]


# ───────────────────────────────────────────────────────────── 实现
def _pw(dev):
    pw, src = creds.get_password(dev, allow_popup=False)
    return pw, src


def _open(dev):
    pw, src = _pw(dev)
    if dev.get("protocol") != "serial" and not pw and not dev.get("allow_no_credential"):
        raise RuntimeError(f"拿不到 {dev['name']} 的密码：先执行 "
                           f"`security add-generic-password -a \"$USER\" -s "
                           f"{dev.get('password_keychain','netdev-'+dev['name'])} -w -U`")
    log = str(ROOT / "logs" / f"{dev['name']}_mcp_session.txt")
    return engine.connect(dev, password=pw, session_log=log), src


def t_list(_):
    devs = engine.load_devices()
    out = [{"name": d["name"], "protocol": d.get("protocol", "ssh"),
            "address": (d.get("host") or d.get("port") or ""),
            "platform": engine.platform_for(d), "tags": d.get("tags", [])}
           for d in devs.values()]
    live = [{"device": p.stem, "bytes": p.stat().st_size}
            for p in sorted((ROOT / "live").glob("*.log"))]
    return {"devices": out, "mirror_streams": live,
            "note": "写操作请用 netdev_apply 且 confirmed=true；黑名单命令会被拒绝"}


def t_run(args):
    # ★ 改调 CLI：CLI 会优先走同屏会话（用户看得见每条命令）
    rc, out, err = _netdev_cli(["run", args["device"], *args["commands"]], timeout=200)
    ok = rc == 0
    results = [{"command": c, "ok": ok, "output": out.strip()[-8000:] if ok else "",
                "error": None if ok else (err.strip() or out.strip())[-800:]}
               for c in args["commands"]]
    return {"device": args["device"], "via": "CLI（优先同屏）", "ok": ok,
            "results": results, "raw": out.strip()[-8000:]}
    # 以下为旧实现（保留备查，不再执行）
    dev = engine.get_device(args["device"])
    s, src = _open(dev)
    m = mirror.Mirror(dev["name"])
    results = []
    try:
        for cmd in args["commands"]:
            k = gates.classify(cmd)
            if k != gates.READ_ONLY:
                m.send(cmd, k)
                results.append({"command": cmd, "ok": False,
                                "error": f"被闸门拒绝（{k}）：run 通道只允许只读命令；写操作请用 netdev_apply"})
                continue
            m.send(cmd, k)
            r = engine.run_smart(s, cmd)
            m.recv(r.text, r.ok)
            results.append({"command": cmd, "ok": r.ok, "output": r.text,
                            "elapsed": r.elapsed, "error": r.error})
    finally:
        s.close(); m.close()
    return {"device": dev["name"], "credential_source": src, "log": str(m.log_path),
            "results": results}


def t_connect_info(args):
    # ★ 改调 CLI（同屏可见）
    rc, out, err = _netdev_cli(["identify", args["device"]], timeout=120)
    return {"device": args["device"], "via": "CLI（同屏）", "ok": rc == 0,
            "output": (out or "")[-4000:],
            "error": ((err or "")[-400:] or None) if rc != 0 else None}

def t_apply(args):
    if not args.get("confirmed"):
        return {"ok": False, "error": "写操作需要 confirmed=true（请先展示变更计划并取得用户同意）"}
    dev = engine.get_device(args["device"])
    cmds = list(args["commands"])
    g = gates.classify_plan(cmds)
    if g["blocked"]:
        return {"ok": False, "error": f"计划含黑名单命令，拒绝执行: {g['blocked']}"}
    # ★ 2026-09-26 改：不再自己直连下发，转调 CLI。
    #   原因（用户反馈）：原来 MCP 自己用 netmiko 直连推送，
    #   用户在屏上【什么都看不到】——“自己就干完了，我很失望”。
    #   CLI 的 apply 本来就有完整实现：走同屏会话（每条命令用户都看得见）
    #   → 先备份 → 逐条下发 → 校验 → 自动 save，并且它自己会弹人审。
    #   两套实现不一致，就是必然有一个漏了。现统一到 CLI 一条路。
    _avy = ["apply", dev["name"]]
    for c in cmds:
        _avy += ["--cmd", c]
    _avy.append("--yes")
    rc, out, err = _netdev_cli(_avy, timeout=300)
    return {"device": dev["name"], "via": "CLI（同屏）", "ok": rc == 0,
            "steps": [{"step": "CLI apply（同屏 + 先备份 + 逐条 + 校验）", "ok": rc == 0}],
            "output": (out or "")[-8000:],
            "error": ((err or "")[-800:] or None) if rc != 0 else None}
    # ── 以下为旧实现（保留备查，不再执行）──
    s, src = _open(dev)
    m = mirror.Mirror(dev["name"])
    out = {"device": dev["name"], "steps": []}
    try:
        # ① 备份
        for kind, cmd in (("run", "display current-configuration"),
                          ("flash", "display saved-configuration")):
            m.send(cmd, "read_only")
            r = s.run(cmd, timeout=60)
            m.recv(r.text, r.ok)
            if r.ok and r.text.strip():
                p = ROOT / "backups" / f"{dev['name']}_{kind}_{_stamp()}.cfg"
                p.write_text(r.text + "\n", encoding="utf-8")
                out["steps"].append({"step": f"备份{kind}", "ok": True, "path": str(p)})
            else:
                out["steps"].append({"step": f"备份{kind}", "ok": False, "error": r.error})
                out["ok"] = False
                out["error"] = "备份失败，已中止下发"
                return out
        # ② 下发
        for c in cmds:
            m.send(c, gates.classify(c))
        r = s.push(cmds)
        m.recv(r.text, r.ok)
        out["steps"].append({"step": "下发配置", "ok": r.ok, "output": r.text, "error": r.error})
        if not r.ok:
            out["ok"] = False
            out["error"] = r.error
            return out
        # ③ 校验
        for c in args.get("verify") or []:
            m.send(c, "read_only")
            vr = s.run(c)
            m.recv(vr.text, vr.ok)
            out["steps"].append({"step": f"校验: {c}", "ok": vr.ok, "output": vr.text})
        # ④ 落盘
        if args.get("save", True):
            m.send("save vrpcfg.zip", "write")
            sv = s.save()
            m.recv(sv.text, sv.ok)
            out["steps"].append({"step": "save", "ok": sv.ok, "output": sv.text, "error": sv.error})
        out["ok"] = all(x["ok"] for x in out["steps"])
        out["log"] = str(m.log_path)
    finally:
        s.close(); m.close()
    return out


def t_save(args):
    # ★ 改调 CLI（同屏可见 + 带人审）；旧的手写实现见 git 历史
    rc, out, err = _netdev_cli(["save", args["device"], "--yes"], timeout=300)
    return {"device": args["device"], "via": "CLI（同屏）", "ok": rc == 0,
            "output": (out or "")[-6000:],
            "error": ((err or "")[-600:] or None) if rc != 0 else None}

def t_backup(args):
    # ★ 改调 CLI（同屏可见）
    rc, out, err = _netdev_cli(["backup", args["device"]], timeout=300)
    return {"device": args["device"], "via": "CLI（同屏）", "ok": rc == 0,
            "output": (out or "")[-6000:],
            "error": ((err or "")[-600:] or None) if rc != 0 else None}

def t_diff(args):
    a = pathlib.Path(args["file_a"]).read_text(encoding="utf-8", errors="replace")
    b = pathlib.Path(args["file_b"]).read_text(encoding="utf-8", errors="replace")
    d = engine.diff_configs(a, b, args["file_a"], args["file_b"])
    return {"identical": not d.strip(), "diff": d}


def t_ping(args):
    # ★ 改调 CLI（同屏可见）
    rc, out, err = _netdev_cli(["ping", args["device"], args.get("target", "")], timeout=120)
    return {"device": args["device"], "via": "CLI（同屏）", "ok": rc == 0,
            "output": (out or "")[-4000:],
            "error": ((err or "")[-400:] or None) if rc != 0 else None}

def t_serial(args):
    """串口通道执行命令。

    ★ 2026-09-26 改：转调 CLI（`netdev run <设备>` 会优先走同屏会话，
      每条命令都打在用户看得见的屏上）。原来这里自己 netmiko/pyserial 直连，
      用户在屏上什么都看不到 —— 与整体"人机同屏"的承诺不符。
      CLI 侧本来就有串口独占锁、凭据、同屏优先等完整逻辑，统一到那一条路。

    ★ 2026-10-03 修 IndentationError：下面 4 行曾经被多缩进 2 格，
      整个文件**语法都不成立**（Python 直接 IndentationError 退出）。
      后果极隐蔽：pi 启动 MCP 服务端时它秒崩，pi **静默丢弃**这个服务端，
      于是模型手里一个 netdev 工具都没有，只能"凭空编"工具调用
      （表现为把 <|DSML|tool_calls> 这类标记当普通文本吐出来）。
      排查入口：`python3 -m py_compile netdev_mcp.py`。
    """
    rc, out, err = _netdev_cli(["run", args.get("device", ""),
                                args.get("command", "")], timeout=200)
    return {"device": args.get("device", ""), "via": "CLI（同屏优先）",
            "command": args.get("command", ""), "ok": rc == 0,
            "output": (out or "")[-8000:],
            "error": ((err or "")[-600:] or None) if rc != 0 else None}


def _tmux(*args):
    import shutil, subprocess
    t = shutil.which("tmux") or str(pathlib.Path.home() / "homebrew/bin/tmux")
    return subprocess.run([t, *args], capture_output=True, text=True)


def _target(name):
    return f"netops:{name}"


def _require(name):
    if _tmux("has-session", "-t", "netops").returncode != 0:
        raise RuntimeError(f"没有同屏会话。先执行：netdev shell {name}")
    wins = _tmux("list-windows", "-t", "netops", "-F", "#{window_name}").stdout.split()
    if name not in wins:
        raise RuntimeError(f"netops 里没有 {name} 窗口。先执行：netdev shell {name}")


def t_screen_list(_):
    r = _tmux("list-panes", "-a", "-F",
              "#{session_name}:#{window_name}  #{pane_width}x#{pane_height}  #{pane_current_command}")
    return {"sessions": [ln for ln in r.stdout.strip().splitlines() if ln],
            "attach_hint": "人在终端执行 tmux attach -t netops 即可看到并接管同一块屏",
            "exit_hint": "退出串口桥 Ctrl+]；离开 tmux：Ctrl+B 然后 D"}


def t_screen_send(args):
    name = args["device"]
    _require(name)
    risk, who = gates.classify_text(args.get("text") or "")
    if risk == gates.BLOCKED:
        return {"ok": False, "error": f"拒绝代发黑名单命令：{who}（这类只能人工亲手敲）"}
    if risk == gates.WRITE and not args.get("confirmed", False):
        return {"ok": False, "risk": "write", "offender": who,
                "error": (f"这段文本含写操作：{who} —— 已拦下。"
                          f"改配置请改用 netdev_apply（先备份→逐条→校验→出错即停）；"
                          f"若确实要直接打进控制台（交互式应答/救急），带 confirmed=true 重试。")}
    # ★ 人审：往控制台直接写东西同样得有人点允许
    if risk == gates.WRITE:
        if not approval.ask(name, [args.get("text") or ""], kind="screen-send", timeout=120):
            return {"ok": False, "error": "人审未通过（拒绝 / 超时）—— 未发送任何字节"}
    _tmux("send-keys", "-t", _target(name), "-l", args["text"])
    if args.get("enter", True):
        _tmux("send-keys", "-t", _target(name), "Enter")
    time.sleep(float(args.get("wait", 1.5)))
    pane = _tmux("capture-pane", "-p", "-J", "-t", _target(name), "-S",
                 f"-{int(args.get('lines', 40))}").stdout
    m = mirror.Mirror(name)
    m.send(f"[同屏] {args['text']}", gates.classify(args["text"]))
    m.recv("\n".join([l for l in pane.splitlines() if l.strip()][-12:]), True)
    m.close()
    return {"device": name, "sent": args["text"], "screen": pane, "log": str(m.log_path)}


def t_screen_read(args):
    name = args["device"]
    _require(name)
    pane = _tmux("capture-pane", "-p", "-J", "-t", _target(name), "-S",
                 f"-{int(args.get('lines', 40))}").stdout
    return {"device": name, "screen": pane}


def t_watch_tail(args):
    dev = args.get("device")
    files = ([ROOT / "live" / f"{dev}.log"] if dev
             else sorted((ROOT / "live").glob("*.log"), key=lambda p: p.stat().st_mtime))
    files = [f for f in files if f.exists()]
    if not files:
        return {"lines": [], "note": "暂无镜像流（尚未发起会话）"}
    out = []
    for f in files:
        body = f.read_text(encoding="utf-8", errors="replace").splitlines()
        out += body[-int(args.get("lines", 40)):]
    return {"lines": out}


# ── 身份信封（2026-09-26 加）────────────────────────────────────────────
#   为什么需要：AI 的上下文会累积，还会被自动压缩。不同设备、不同时间的
#   工具输出混在上下文里时，模型容易"张冠李戴"（把 A 设备的配置当成 B 的）
#   或"把旧数据当新的"（设备配置已变，它还按半小时前的输出判断）。
#
#   对策：每次工具返回都带上【设备 + 时间 + 工具名】——
#   模型每看到一次数据都自带"这是谁、什么时候的"，它就能自查。
#   这是最便宜也最有效的抗污染手段（不损失任何信息，只加三个字段）。
def envelope(tool_name: str, args: dict, payload):
    """给工具返回包一层身份信封。"""
    if not isinstance(payload, dict):
        payload = {"data": payload}
    dev = ""
    for k in ("device", "target", "source"):
        if isinstance(args.get(k), str) and args.get(k):
            dev = args[k]; break
    _who = {
        "device": dev or "(未指定)",
        "tool": tool_name,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 保持原字段在前、信封在后，便于模型阅读
    out = dict(payload)
    out["_identity"] = _who
    return out


# ── 调 CLI（2026-09-26 加，修"AI 操作不走同屏"）──────────────────────────
#   问题：MCP 的 t_apply / t_run / t_save / t_backup / t_ping / t_connect_info
#        都是【自己用 netmiko 直连】实现的，完全绕过了同屏会话 ——
#        AI 在界面上改配置，用户在屏上什么都看不到（用户反馈："自己就干完了，我很失望"）。
#        而 CLI（netdev run / apply / save / backup）本来就有一套完整实现：
#        优先走同屏会话（每条命令都打在用户看得见的屏上）、先备份、逐条下发、校验、
#        写操作还有人审弹窗。两套实现不一致 = 必然有一个是对的、有一个漏了。
#
#   修法：MCP 不再自己实现，全部转调 CLI —— 只做"参数转换"，
#        这样 AI 侧与人工侧看到的、执行的是同一条路径，不可能再分叉。
def _netdev_cli(argv: list, timeout: int = 180, env_extra: dict | None = None):
    """调 netdev CLI，返回 (rc, stdout, stderr)。"""
    env = dict(os.environ)
    _hb = pathlib.Path.home() / "homebrew" / "bin"
    _extra = [str(p) for p in (_hb, pathlib.Path("/opt/homebrew/bin"), pathlib.Path("/usr/local/bin")) if p.exists()]
    _base = env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["PATH"] = ":".join(_extra + ([_base] if _base else []))
    # 审批通道：转发给子进程（不然它找不到网页弹窗地址）
    if os.environ.get("NETDEV_APPROVAL_URL"):
        env["NETDEV_APPROVAL_URL"] = os.environ["NETDEV_APPROVAL_URL"]
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run([CLI, *argv], capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=str(ROOT))
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（{timeout}s）"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


HANDLERS = {
    "netdev_list": t_list, "netdev_run": t_run, "netdev_connect_info": t_connect_info,
    "netdev_apply": t_apply, "netdev_save": t_save, "netdev_backup": t_backup,
    "netdev_diff": t_diff, "netdev_ping": t_ping, "netdev_serial_run": t_serial,
    "netdev_watch_tail": t_watch_tail,
    "netdev_screen_list": t_screen_list,
    "netdev_screen_send": t_screen_send,
    "netdev_screen_read": t_screen_read,
}


# ───────────────────────────────────────────────────────────── 源码热重载（改完即生效，不必重启）
_SELF = pathlib.Path(__file__).resolve()


def _watched_files():
    """被监视的源码：本文件自身 + lib/*.py。"""
    return [p for p in [_SELF, *sorted(LIB_DIR.glob("*.py"))] if p.exists()]


def _snapshot():
    """源码 mtime 快照（纳秒级，避免同秒改动漏检）。"""
    return {str(p): p.stat().st_mtime_ns for p in _watched_files()}


def _tool_fingerprint():
    """工具集指纹：TOOLS 的全部可见字段（名字/描述/inputSchema）+ HANDLERS 的键。

    逐项 sort_keys 规范化后序列化，所以描述、参数 schema 变了也算变；
    也不受 dict 插入顺序抖动影响。
    """
    defs = tuple(json.dumps(t, sort_keys=True, ensure_ascii=False, default=repr) for t in TOOLS)
    return (defs, tuple(sorted(HANDLERS)))


def _module_name_of(path):
    return "lib" if path.stem == "__init__" else f"lib.{path.stem}"


def _reload_one(path):
    name = _module_name_of(path)
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def _reexec_self(mod):
    """把模块顶层代码就地重跑一遍（module __dict__ 原地更新）。

    以脚本方式启动（python netdev_mcp.py -> __name__ == "__main__"）时 __spec__ 为 None，
    importlib.reload 会直接拒绝（ModuleNotFoundError: spec not found），故用编译+exec 代替。
    compile 先行，语法错误在此抛出，旧代码一字未动。
    """
    code = compile(pathlib.Path(mod.__file__).read_text(encoding="utf-8"), mod.__file__, "exec")
    d = mod.__dict__
    was = d.get("__name__")
    d["__name__"] = "_netdev_mcp_reloading_"   # 防顶层 `if __name__ == "__main__"` 递归进 main()
    try:
        exec(code, d)
    finally:
        d["__name__"] = was
    return mod


def _reload_self():
    """reload 本模块自身（必须先做完全部 lib 模块）。"""
    mod = sys.modules[__name__]
    if getattr(mod, "__spec__", None) is None:
        return _reexec_self(mod)
    return importlib.reload(mod)


def _reload_error(path, exc):
    sys.stderr.write(f"netdev-mcp hot-reload failed: {path} -> "
                     f"{type(exc).__name__}: {exc}\n")
    sys.stderr.flush()


_mtimes = _snapshot()


def reload_if_stale():
    """源码有改动就地热重载：先 reload 各 lib 模块，最后 reload 本模块自身。

    顺序不可反：lib 先换新，自身再换新手里的引用。
    任何 reload 失败都只写 stderr 并继续用旧代码服务；
    失败条目不计入 `_mtimes`，修好后下次请求会重试。
    """
    cur = _snapshot()
    stale = sorted(p for p, mt in cur.items() if _mtimes.get(p) != mt)
    if not stale:
        return False
    before = _tool_fingerprint()
    failed = set()
    for p in stale:
        if pathlib.Path(p) == _SELF:
            continue  # 自身放最后
        try:
            _reload_one(pathlib.Path(p))
        except Exception as e:
            failed.add(p)
            _reload_error(p, e)
    if str(_SELF) in stale:
        try:
            _reload_self()
        except Exception as e:
            failed.add(str(_SELF))
            _reload_error(str(_SELF), e)
    for p in stale:  # 只把成功的条目记为已重载
        if p in failed:
            _mtimes.pop(p, None)
        else:
            _mtimes[p] = cur[p]
    if _tool_fingerprint() != before:
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    return True


def _stamp():
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ───────────────────────────────────────────────────────────── JSON-RPC
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(id_, payload):
    send({"jsonrpc": "2.0", "id": id_, "result": payload})


def error(id_, code, message):
    send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def handle(req):
    method = req.get("method")
    id_ = req.get("id")
    if method == "initialize":
        result(id_, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                     "serverInfo": SERVER, "instructions": INSTRUCTIONS})
    elif method in ("notifications/initialized", "initialized"):
        pass
    elif method == "tools/list":
        result(id_, {"tools": TOOLS})
    elif method == "tools/call":
        p = req.get("params", {})
        name, args = p.get("name"), p.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            error(id_, -32602, f"unknown tool: {name}")
            return
        try:
            payload = fn(args)
            # ★ 每个工具返回都带"设备+时间+工具名"，抗上下文污染（见上方说明）
            payload = envelope(name, args, payload)
            result(id_, {"content": [{"type": "text",
                                      "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                         "isError": False})
        except Exception as e:
            result(id_, {"content": [{"type": "text",
                                      "text": f"执行失败: {type(e).__name__}: {e}\n"
                                              f"{traceback.format_exc()[-800:]}"}],
                         "isError": True})
    elif method == "ping":
        result(id_, {})
    elif id_ is not None:
        error(id_, -32601, f"method not supported: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reload_if_stale()
        except Exception as e:  # 热重载本身出错也不能杀死服务器
            sys.stderr.write(f"netdev-mcp hot-reload error: {e}\n")
            sys.stderr.flush()
        try:
            handle(json.loads(line))
        except Exception as e:
            sys.stderr.write(f"netdev-mcp error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
