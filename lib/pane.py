# -*- coding: utf-8 -*-
"""同屏会话适配层（2026-10 Windows 移植引入，计划回哺主仓）。

POSIX: tmux（命令封装，旧行为不变）
Windows: **pane-daemon**（每设备一个常驻守护，TCP JSON 行协议；
         实现见 tools/pane_daemon.py）——替代 tmux window + 桥宿主。

所有「开屏 / 发字 / 读屏 / 杀屏」统一从这里走，别处不要再直接拼 tmux。

控制口协议（一行 JSON 一请求/应答；字节一律 base64）：
    {"op":"alive"}                    → {"ok":true,"alive":bool,"pid":int}
    {"op":"respawn"}                  → {"ok":true}
    {"op":"send","data":"..."}        → {"ok":true}           （字面字节）
    {"op":"send-key","key":"Enter"}   → {"ok":true}           （Enter/Space/C-c/C-u）
    {"op":"capture","raw":bool}       → {"ok":true,"b64":...}
    {"op":"subscribe","tail":bool}    → 流式 {"event":"data","b64":...}/{"event":"dead"}
    {"op":"resize","rows":N,"cols":M} → {"ok":true}
    {"op":"kill"}                     → {"ok":true} 后守护退出
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import host, paths as _P

BIND_HOST = "127.0.0.1"

_ANSI_RE = re.compile(
    rb"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)"     # OSC: \x1b]...\x07 或 \x1b]...\x1b\\
    rb"|\x1b\[[0-9;?]*[ -/]*[@-~]"         # CSI: \x1b[ + 参数 + 中间字节 + 最终字节
    rb"|\x1b[OPX-_]"                       # 简单控制序列（单字节最终）
    rb"|\x1b[()][0-9A-Za-z]"               # 字符集切换 G0/G1
    rb"|[\x1b\x07\x08\x0f\x0e]"            # 裸 ESC/BEL/BS/SI/SO
)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def strip_ansi(b: bytes) -> str:
    """去 ANSI 控制序列 → 纯文本（CR 规整）。"""
    txt = _ANSI_RE.sub(b"", b).decode("utf-8", "replace")
    return txt.replace("\r\n", "\n").replace("\r", "\n")


# ───────────────────────────────────────── 守护注册表
def panes_dir() -> pathlib.Path:
    d = _P.state_dir() / "panes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reg_path(name: str) -> pathlib.Path:
    return panes_dir() / f"{name}.json"


def read_reg(name: str) -> dict | None:
    try:
        d = json.loads(reg_path(name).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


class Control:
    """一条到 pane-daemon 的控制连接。"""

    def __init__(self, host_: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host_, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        try:
            # ★ 2026-10-07：关 Nagle。内部请求都是「写一小段 + 立刻等一行回」，
            #   开 Nagle 会和延迟 ACK 撞出 10~40ms 的偶发停顿。
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.f = self.sock.makefile("rwb", buffering=0)

    def request(self, msg: dict, timeout: float | None = None) -> dict:
        if timeout is not None:
            self.sock.settimeout(timeout)
        self.f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        line = self.f.readline()
        if not line:
            raise ConnectionError("pane-daemon 关闭了连接")
        return json.loads(line.decode("utf-8", "replace"))

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def control(name: str, timeout: float = 5.0) -> Control | None:
    """拿到该设备守护的控制连接；守护不在/不应答 → None。"""
    reg = read_reg(name)
    if not reg or not host.pid_alive(int(reg.get("pid", 0))):
        return None
    try:
        c = Control(reg.get("host", BIND_HOST), int(reg["port"]), timeout=timeout)
        c.request({"op": "alive"})
        return c
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        return None


# ───────────────────────────────────────── 复用长连接（每设备一条）
# ★ 2026-10-07 修（网页终端「输入很卡」）：send_literal / send_key / bridge_alive
#   原来每次都 control() —— 新建 TCP 连接 + 一次 alive 往返，用完即关。
#   Windows 上「新建连接 + 首个往返」约一半会撞上 10~26ms 的延迟尖峰
#   （实测：新建 p50 1.4ms / 27/60 >10ms / 峰值 25.7ms；复用长连接 p50 0.18ms / 0/60）。
#   这里给每台设备缓存一条长连接，请求在锁内串行化；连接失效（守护重启/被杀）
#   时自动重连一次。事件流（subscribe）仍走 control() 的独占连接，不受影响。
class _Link:
    __slots__ = ("name", "lock", "ctrl")

    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self.ctrl: Control | None = None

    def request(self, msg: dict, timeout: float = 5.0) -> dict | None:
        with self.lock:
            for _ in (0, 1):                      # 首次失败 → 重连再试一次
                if self.ctrl is None:
                    reg = read_reg(self.name)
                    if not reg or not host.pid_alive(int(reg.get("pid", 0))):
                        return None
                    try:
                        self.ctrl = Control(reg.get("host", BIND_HOST),
                                            int(reg["port"]), timeout=timeout)
                    except Exception:
                        self.ctrl = None
                        return None
                try:
                    return self.ctrl.request(msg, timeout=timeout)
                except Exception:
                    try:
                        self.ctrl.close()
                    except Exception:
                        pass
                    self.ctrl = None
            return None

    def drop(self) -> None:
        with self.lock:
            if self.ctrl is not None:
                try:
                    self.ctrl.close()
                except Exception:
                    pass
                self.ctrl = None


_LINKS: dict[str, _Link] = {}
_LINKS_GUARD = threading.Lock()


def _link(name: str) -> _Link:
    with _LINKS_GUARD:
        lk = _LINKS.get(name)
        if lk is None:
            lk = _Link(name)
            _LINKS[name] = lk
        return lk


def drop_link(name: str) -> None:
    """丢掉该设备的长连接（守护退出/被杀、会话结束时调用）。"""
    with _LINKS_GUARD:
        lk = _LINKS.pop(name, None)
    if lk is not None:
        lk.drop()


def bridge_alive(name: str) -> bool:
    """守护在跑 **且** 桥进程活着（串口独占语义看的是这个）。"""
    r = _link(name).request({"op": "alive"})
    return bool(r and r.get("alive"))


def holds(name: str) -> bool:
    """该设备是否正在活着的人机同屏会话里（替代 tmux has-window 判定）。"""
    return bridge_alive(name)


# ───────────────────────────────────────── 守护拉起
def _daemon_log(name: str) -> pathlib.Path:
    d = _P.runtime_dir("logs")
    return d / f"pane-{name}.daemon.log"


def _force_kill_pid_tree(pid: int) -> None:
    """强杀某 pid 及其整棵子进程树（Windows 用 taskkill /T；POSIX 用 SIGKILL）。"""
    if not pid or pid <= 0:
        return
    if not host.IS_WIN:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass
        return
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, timeout=8, creationflags=0x08000000)
    except Exception:
        pass


def _win_proc_table() -> list[dict]:
    """Windows 进程表 [{pid, ppid, cmd}]（PowerShell CIM；失败给空表）。

    ★ 必须「按字节收 + errors='replace' 解码」：进程命令行里含中文（本机就有），
      PowerShell 默认按控制台代码页输出，用 text=True 会抛 UnicodeDecodeError
      → 异常被吞 → 空表 → 孤儿桥永远清不掉（本修复第一版正是这么翻车的）。
    """
    ps = ("$ProgressPreference='SilentlyContinue';"
          "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
          "Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=20, creationflags=0x08000000)
    except Exception:
        return []
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    if not txt:
        return []
    try:
        items = json.loads(txt)
    except Exception:
        return []
    if isinstance(items, dict):
        items = [items]
    out: list[dict] = []
    for it in items or []:
        try:
            out.append({"pid": int(it.get("ProcessId") or 0),
                        "ppid": int(it.get("ParentProcessId") or 0),
                        "cmd": str(it.get("CommandLine") or "")})
        except Exception:
            continue
    return out


_BRIDGE_SCRIPTS = ("serial_bridge.py", "ssh_bridge.py", "telnet_bridge.py")


def _kill_stale_bridges(name: str) -> list[int]:
    """清掉本设备遗留的「孤儿桥」——父守护已死、桥进程却还在跑。

    为什么必须清：桥（serial_bridge 等）会独占串口。守护被强杀/崩溃后桥脱管，
    仍握着 COMx → 下一次接入的新桥 open 直接 PermissionError 13 → 守护还活着
    但桥秒死 → 界面把该设备判为「未接入」而从列表里过滤掉（用户反馈
    "明明接入了却不显示"，live/huawei.pane.log 里就是一串 PermissionError 13）。

    认领规则：命令行含本设备专有的 `live\\<name>.screen.log` 且是桥脚本，
    并且**父链上没有任何活着的本设备守护**。返回被清掉的 pid。
    """
    if not host.IS_WIN:
        return []
    procs = _win_proc_table()
    if not procs:
        return []
    by_pid = {p["pid"]: p for p in procs}
    marker = f"{name}.screen.log"

    # 活守护：命令行含 pane_daemon.py 且指向本设备（venv 转发器 + base 两个 pid 都算）
    live_daemons = {p["pid"] for p in procs
                    if "pane_daemon.py" in p["cmd"] and "--device" in p["cmd"]
                    and name in p["cmd"]}

    def _under_live_daemon(pid: int) -> bool:
        seen = set()
        cur = pid
        while cur and cur not in seen:
            seen.add(cur)
            if cur in live_daemons:
                return True
            par = by_pid.get(cur)
            cur = par["ppid"] if par else 0
        return False

    killed: list[int] = []
    for p in procs:
        c = p["cmd"]
        if marker not in c or not any(s in c for s in _BRIDGE_SCRIPTS):
            continue
        if _under_live_daemon(p["pid"]):
            continue                    # 活守护的桥，不能杀
        try:
            subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"],
                           capture_output=True, timeout=8, creationflags=0x08000000)
            killed.append(p["pid"])
        except Exception:
            pass
    return killed


def start_daemon(name: str, dev: dict | None = None) -> None:
    """以后台脱离方式拉起 pane-daemon（不等它就绪）。

    dev: 可选设备 dict。传了就序列化进 env NETDEV_DEVICE_JSON，
         守护侧 Daemon._resolve() 优先用它——这样 ad-hoc 目标
         （netdev telnet 1.2.3.4 / netdev ssh admin@1.2.3.4）
         不需要先在 devices.toml 登记就能跑通。
    """
    import json as _json

    root = _P.ROOT
    py = host.venv_python(root)
    if not py.exists():
        py = pathlib.Path(sys.executable)
    script = root / "tools" / "pane_daemon.py"
    argv = [str(py), str(script), "--device", name, "--bind", BIND_HOST]
    env = dict(os.environ)
    if dev:
        env["NETDEV_DEVICE_JSON"] = _json.dumps(dev, ensure_ascii=False)
    logf = open(str(_daemon_log(name)), "a", encoding="utf-8", errors="replace")
    if host.IS_WIN:
        # ★ 2026-10-07 修（接入时仍弹终端窗口）：**不要** DETACHED_PROCESS。
        #   venv 的 .venv\Scripts\python.exe 是「转发器」——真正跑代码的是它
        #   再拉起的 base python。实测（cons_test：5 组组合对照）带
        #   DETACHED_PROCESS 时，转发器会给那个子进程新建一个**可见**控制台
        #   （CREATE_NO_WINDOW 被吞掉），于是每次接入都弹一个终端；只留
        #   CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP 则完全无窗口。
        #   守护存活不依赖 DETACHED：Windows 父进程退出本就不连坐子进程
        #   （实证：桥进程正是这样在守护死后继续活着的）。
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)


def ensure(dev, *, restart: bool = False, timeout: float = 60.0) -> bool:
    """确保该设备的同屏会话在跑（守护 + 桥都活着）。返回是否成功。

    dev 可传设备 dict 或设备名字符串。
    """
    name = dev["name"] if isinstance(dev, dict) else dev
    if restart:
        kill(name)
    c = control(name)
    if c is not None:
        try:
            r = c.request({"op": "alive"})
            if not r.get("alive"):
                # 桥死了 → 先清遗留孤儿桥（很可能还握着串口，导致原地重开也失败），
                # 再 respawn；否则会陷入「守护活着但桥永远起不来」的死循环。
                _kill_stale_bridges(name)
                c.request({"op": "respawn"}, timeout=10)
            return True
        except Exception:
            return False
        finally:
            c.close()
    # 守护不在 → 先清遗留孤儿桥（守护已死，桥可能仍握着串口），再拉起并轮询注册表
    _kill_stale_bridges(name)
    try:
        reg_path(name).unlink()
    except Exception:
        pass
    start_daemon(name, dev if isinstance(dev, dict) else None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if control(name) is not None:
            return True
        time.sleep(0.3)
    return False


# ───────────────────────────────────────── 发 / 读
def send_literal(name: str, data) -> bool:
    """往桥里写字面字节/文本（不自动回车）。"""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = str(data)
    r = _link(name).request({"op": "send", "data": text})
    return bool(r and r.get("ok"))


_SPECIAL = {"Enter": "\r", "Space": " ", "C-c": "\x03", "C-u": "\x15"}


def send_key(name: str, key: str) -> bool:
    """发特殊键（Enter/Space/C-c/C-u）。"""
    if key in _SPECIAL:
        r = _link(name).request({"op": "send", "data": _SPECIAL[key]})
    else:
        r = _link(name).request({"op": "send-key", "key": key})
    return bool(r and r.get("ok"))


def capture_raw(name: str, tail_bytes: int = 300 * 1024) -> bytes:
    """取屏幕原始字节（含 ANSI），默认全量环形缓冲。"""
    r = _link(name).request({"op": "capture", "raw": True,
                             "tail_bytes": int(tail_bytes)})
    if not r:
        return b""
    return _ub64(r.get("b64", ""))


def capture_text(name: str, lines: int = 200) -> str:
    """取屏幕纯文本（去色），最后 n 行。"""
    txt = strip_ansi(capture_raw(name))
    out_lines = [x for x in txt.split("\n")]
    return "\n".join(out_lines[-lines:])


def tail(name: str, n: int = 12) -> str:
    """同屏窗格最后 n 行纯文本（对齐 tmux capture-pane -S -n 的调用语义）。"""
    return capture_text(name, n)


def resize(name: str, rows: int, cols: int) -> bool:
    r = _link(name).request({"op": "resize", "rows": int(rows), "cols": int(cols)})
    return bool(r and r.get("ok"))


def kill(name: str) -> bool:
    """杀掉该设备的同屏会话（守护退出、桥被连坐终止）。"""
    reg = read_reg(name)
    c = control(name)
    if not c:
        # 守护不应答但注册表残留 → 强杀整棵进程树再清注册表。
        # ★ 2026-10-07 修：原来只 unlink 注册表就返回，脱管的守护+桥会继续
        #   活着并握着串口 → 下次接入 PermissionError 13（孤儿桥就是这么来的）。
        _force_kill_pid_tree(int((reg or {}).get("pid", 0)))
        _kill_stale_bridges(name)
        try:
            reg_path(name).unlink()
        except Exception:
            pass
        drop_link(name)
        return True
    try:
        try:
            c.request({"op": "kill"}, timeout=3)
        except Exception:
            pass
    finally:
        c.close()
    deadline = time.time() + 8
    while time.time() < deadline:
        reg = read_reg(name)
        if not reg or not host.pid_alive(int(reg.get("pid", 0))):
            break
        time.sleep(0.2)
    # 兜底：守护没自己退（kill 请求没送达/卡住）→ 强杀整棵树，别留孤儿桥
    reg = read_reg(name)
    if reg and host.pid_alive(int(reg.get("pid", 0))):
        _force_kill_pid_tree(int(reg.get("pid", 0)))
    _kill_stale_bridges(name)
    try:
        reg_path(name).unlink()
    except Exception:
        pass
    drop_link(name)
    return True


def list_screens() -> list[dict]:
    """列出全部同屏会话（screen-ls 数据源）。

    返回字段对齐旧 tmux 版：window/size/command/dead/target。
    """
    out: list[dict] = []
    for jf in panes_dir().glob("*.json"):
        name = jf.stem
        reg = read_reg(name)
        if not reg:
            continue
        alive_daemon = host.pid_alive(int(reg.get("pid", 0)))
        dead = True
        command = reg.get("command", "")
        size = reg.get("size", "")
        c = control(name, timeout=1.5)
        if c is not None:
            try:
                r = c.request({"op": "alive"})
                dead = not bool(r.get("alive"))
                command = r.get("command") or command
                size = r.get("size") or size
            except Exception:
                dead = not alive_daemon
            finally:
                c.close()
        out.append({"target": f"pane:{name}", "window": name, "size": size,
                    "command": command, "dead": dead, "pid": reg.get("pid")})
    return sorted(out, key=lambda x: x["window"])


# ───────────────────────────────────────── 喂送通道客户端（桥内使用）
def feed_client(port) -> tuple[queue.Queue, socket.socket]:
    """桥：连守护的喂送口，守护推来的输入字节放进队列。

    返回 (q, sock)；q 里 b"" 哨兵 = 喂送通道断了。
    """
    s = socket.create_connection((BIND_HOST, int(port)), timeout=10)
    s.settimeout(None)              # 连上后长期阻塞读，不要 10s 超时
    q: queue.Queue = queue.Queue()

    def _pump():
        f = s.makefile("rb", buffering=0)
        while True:
            line = f.readline()
            if not line:
                q.put(b"")
                return
            try:
                m = json.loads(line.decode("utf-8", "replace"))
                if m.get("op") == "send":
                    q.put(_ub64(m.get("b64", "")))
            except Exception:
                pass

    threading.Thread(target=_pump, daemon=True).start()
    return q, s


# ───────────────────────────────────────── 订阅（本地客户端用）
def subscribe(name: str, sink, *, include_tail: bool = True) -> Control | None:
    """订阅该会话的实时输出。

    sink(bytes) 每收到一块数据被调用；守护线程内执行，sink 要快。
    返回 Control（关闭即退订）；守护不在 → None。
    返回的连接上另起线程读事件；调用方 close 结束。
    """
    import threading
    c = control(name)
    if not c:
        return None

    def _pump():
        try:
            while True:
                line = c.f.readline()
                if not line:
                    break
                ev = json.loads(line.decode("utf-8", "replace"))
                if ev.get("event") == "data":
                    sink(_ub64(ev.get("b64", "")))
                elif ev.get("event") == "dead":
                    sink(b"")
        except Exception:
            pass

    try:
        c.request({"op": "subscribe", "tail": bool(include_tail)})
        c.sock.settimeout(None)        # 事件流要长期挂着，去掉 5s 超时
        threading.Thread(target=_pump, daemon=True).start()
        return c
    except Exception:
        c.close()
        return None
