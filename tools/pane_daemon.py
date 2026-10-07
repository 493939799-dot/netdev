#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pane-daemon —— Windows 同屏会话守护（替代 tmux window + 桥宿主）。

每台设备一个常驻守护：
  1. 按协议拉起「桥」（serial/ssh/telnet bridge），以管道接桥的 stdin/stdout；
  2. 桥的输出 → 环形缓冲（300KB 上限，超了折半淘汰老字节）+ 订阅者广播；
  3. TCP 控制口（127.0.0.1，JSON 行协议，字节 base64），
     端口/ pid 写 state/panes/<device>.json；
  4. 桥退出 → 广播 dead，守护不自动重开（与 tmux remain-on-exit 一致）；
     客户端用 respawn 重新拉起。

用法:
    pane_daemon.py --device <设备名> [--bind 127.0.0.1] [--port 0]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib import host, paths as _P  # noqa: E402

RING_MAX = 300 * 1024
SUBSCRIBER_QMAX = 256 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ───────────────────────────────────────── Windows Job 对象（防孤儿桥）
# 守护被强杀/崩溃时，它拉起的桥会脱管继续握着串口（→ 下次接入
# PermissionError 13 的根源）。把桥放进一个「句柄一关就杀光成员」的 Job：
# 守护进程一死，系统句柄随之关闭，桥（连同其子进程）被连坐杀掉，
# 从此不再产生孤儿桥。分配失败（如嵌套 Job 受限）时静默跳过，不影响接入。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _win_make_kill_on_close_job() -> int:
    """建 Job 并设 KILL_ON_JOB_CLOSE，返回句柄（失败 0）。"""
    if not host.IS_WIN:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return 0
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(h, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(h)
            return 0
        return int(h)
    except Exception:
        return 0


def _win_assign_job(job: int, proc) -> bool:
    """把已启动的进程加进 Job（失败 False，不影响正常接入）。"""
    if not job or not host.IS_WIN or proc is None:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(k32.AssignProcessToJobObject(int(job), int(proc._handle)))
    except Exception:
        return False


# ───────────────────────────────────────── 环形缓冲
class Ring:
    """字节环形缓冲：超限折半淘汰（保留较新的一半）。"""

    def __init__(self, limit: int = RING_MAX):
        self.buf = bytearray()
        self.limit = limit
        self.lock = threading.Lock()

    def append(self, b: bytes) -> None:
        with self.lock:
            self.buf += b
            if len(self.buf) > self.limit:
                del self.buf[: len(self.buf) // 2]

    def tail(self, n: int | None = None) -> bytes:
        with self.lock:
            if n is None or n >= len(self.buf):
                return bytes(self.buf)
            return bytes(self.buf[-n:])


# ───────────────────────────────────────── 订阅者
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs: dict[int, queue.Queue] = {}
        self._n = 0

    def add(self) -> tuple[int, queue.Queue]:
        with self.lock:
            self._n += 1
            q: queue.Queue = queue.Queue()
            self.subs[self._n] = q
            return self._n, q

    def remove(self, sid: int) -> None:
        with self.lock:
            self.subs.pop(sid, None)

    def broadcast(self, b: bytes) -> None:
        with self.lock:
            subs = list(self.subs.values())
        for q in subs:
            try:
                q.put_nowait(b)
            except queue.Full:
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(b)
                except Exception:
                    pass

    def dead(self) -> None:
        with self.lock:
            subs = list(self.subs.values())
        for q in subs:
            try:
                q.put_nowait(None)
            except Exception:
                pass


# ───────────────────────────────────────── 桥进程
class Bridge:
    def __init__(self, device: dict, ring: Ring, hub: Hub, pane_log: pathlib.Path):
        self.dev = device
        self.ring = ring
        self.hub = hub
        self.pane_log = pane_log
        self.proc: subprocess.Popen | None = None
        self.alive = False
        self.command = ""
        self.size = "140x40"
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        # Windows：本环境对「写别的进程 stdin」有拦截，daemon→桥改走
        # localhost TCP 喂送通道；桥的输出仍走 stdout 管道。
        self.feed_srv: socket.socket | None = None
        self.feed_conn: socket.socket | None = None
        self.feed_port = 0
        self._feed_lock = threading.Lock()
        self._job = 0          # Windows：KILL_ON_JOB_CLOSE 的 Job 句柄（防孤儿桥）

    def _new_feed(self):
        """（重新）开一个喂送监听口；每次 spawn/respawn 都换新的。"""
        self._close_feed()
        self.feed_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.feed_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.feed_srv.bind(("127.0.0.1", 0))
        self.feed_srv.listen(1)
        self.feed_port = self.feed_srv.getsockname()[1]

    def _close_feed(self):
        with self._feed_lock:
            if self.feed_conn is not None:
                try:
                    self.feed_conn.close()
                except Exception:
                    pass
                self.feed_conn = None
        if self.feed_srv is not None:
            try:
                self.feed_srv.close()
            except Exception:
                pass
            self.feed_srv = None

    def _accept_feed(self):
        try:
            assert self.feed_srv is not None
            conn, _ = self.feed_srv.accept()
            with self._feed_lock:
                self.feed_conn = conn
        except OSError:
            pass

    def _argv_env(self) -> tuple[list[str], dict]:
        py = host.venv_python(ROOT)
        pyexe = str(py) if py.exists() else sys.executable
        name = self.dev["name"]
        logf = ROOT / "live" / f"{name}.screen.log"
        proto = self.dev.get("protocol", "ssh")
        env = dict(os.environ)
        env["NETDEV_DEVICE"] = name
        # Windows pane-daemon 用 PIPE 接桥 stdout —— isatty() 恒 False，
        # 桥里 colorize.enabled() 会把着色全关掉。强制开，让 IP 橙 + 输入三色
        # （人蓝/AI紫/系统灰）进环形缓冲；xterm.js 原生渲染 ANSI 没问题。
        # 日志文件（live/*.screen.log）桥里单独写原始字节，不会被污染。
        env["NETDEV_FORCE_COLOR"] = "1"
        # 完整设备 dict 序列化 → 桥进程里可用（telnet_bridge / ssh_bridge / serial_bridge
        # 都靠它取 username/password 等字段，ad-hoc 临时目标才能正常自动登录）
        import json as _j3
        try:
            env["NETDEV_DEVICE_JSON"] = _j3.dumps(self.dev, ensure_ascii=False)
        except Exception:
            pass
        if proto == "serial":
            port = self.dev.get("port") or ""
            if str(port).strip().lower() in ("", "auto"):
                # 清单里写的是 auto（或没写）→ 先解析成具体端口再交给桥。
                # 桥只认具体端口名，直接给它 "auto" 会开不了口；
                # 而 netdev device-add serial --auto 生成的正是这种条目。
                from lib import engine as _eng2
                port = _eng2.discover_serial_port() or ""
                if not port:
                    print("[pane-daemon] 未发现可用串口设备（检查 USB-Console 线/驱动）", flush=True)
            # 波特率：清单写 auto/空 → 先用上次探测到的缓存值（接入更快，也不会
            # 把字面量 "auto" 写进 command 让人误以为没认到设备）；没缓存就传 auto，
            # 由桥（serial_bridge）自己扫候选档并回写缓存。
            # 2026-10-06 修：原来直接 self.dev.get("baud", 9600) —— 写 auto 时把
            # 字符串 "auto" 透传（桥能处理），但默认值 9600 会让老清单静默按 9600 起，
            # 真机 115200 时接入要先自检切档、state/UI 还一直显示 9600。
            baud = self.dev.get("baud")
            if str(baud).strip().lower() in ("", "auto", "none"):
                from lib import engine as _eng3
                baud = _eng3.serial_baud_cache_get(port) or "auto"
            argv = [pyexe, str(HERE / "serial_bridge.py"), str(port), str(baud), str(logf)]
            env["SERIAL_BACKSPACE"] = str(self.dev.get("backspace", "auto"))
            env["SERIAL_DEVICE"] = name
            self.command = f"serial_bridge {port}@{baud}"
        elif proto == "telnet":
            h_, p_ = self.dev["host"], int(self.dev.get("port", 23))
            argv = [pyexe, str(HERE / "telnet_bridge.py"), str(h_), str(p_), str(logf)]
            env["NETDEV_BACKSPACE"] = str(self.dev.get("backspace") or "bs")
            self.command = f"telnet_bridge {h_}:{p_}"
        else:
            # 与 netdev_cli._shell_inner 的 ssh_argv 完全同构
            import shutil
            from lib import engine
            ssh_exe = shutil.which("ssh") or shutil.which("ssh.exe") or "ssh"
            h_ = self.dev["host"]
            user = self.dev.get("username", "admin")
            p_ = int(self.dev.get("port", 22))
            ssh_argv = [ssh_exe, "-o", f"KexAlgorithms=+{engine.LEGACY_KEX}",
                        "-o", "ControlMaster=no", "-o", "ControlPath=none"]
            if self.dev.get("sim"):
                # 本机模拟器：每次启动都换主机密钥，放宽检查（真机绝不加）
                ssh_argv += ["-o", "StrictHostKeyChecking=no",
                             "-o", "UserKnownHostsFile=NUL"]
            ssh_argv += ["-p", str(p_), f"{user}@{h_}"]
            argv = [pyexe, str(HERE / "ssh_bridge.py"), str(logf), "--"] + ssh_argv
            env["NETDEV_BACKSPACE"] = str(self.dev.get("backspace") or "auto")
            self.command = f"ssh {user}@{h_}:{p_}"
        return argv, env

    def spawn(self) -> bool:
        with self._lock:
            argv, env = self._argv_env()
            flags = 0
            if host.IS_WIN:
                CREATE_NO_WINDOW = 0x08000000
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                self._new_feed()
                env["NETDEV_FEED_PORT"] = str(self.feed_port)
                # 先挂出喂送口，桥一连进来就接
                threading.Thread(target=self._accept_feed, daemon=True).start()
            try:
                self.proc = subprocess.Popen(
                    argv, cwd=str(ROOT), env=env,
                    stdin=subprocess.PIPE if not host.IS_WIN else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, bufsize=0,
                    creationflags=flags, close_fds=True)
            except Exception as e:
                sys.stderr.write(f"[pane-daemon] 桥启动失败: {type(e).__name__}: {e}\n")
                self.alive = False
                return False
            if host.IS_WIN:
                # 把桥放进 KILL_ON_JOB_CLOSE 的 Job：守护一死桥即被连坐，
                # 不会脱管握着串口（否则下次接入 PermissionError 13）。
                if not self._job:
                    self._job = _win_make_kill_on_close_job()
                _win_assign_job(self._job, self.proc)
            self.alive = True
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()
            return True

    def _pump(self):
        assert self.proc is not None
        out = self.proc.stdout
        logf = open(str(self.pane_log), "ab", buffering=0)
        try:
            while True:
                try:
                    d = out.read(65536)
                except (ValueError, OSError):
                    break
                if not d:
                    break
                self.ring.append(d)
                try:
                    logf.write(d)
                except (ValueError, OSError):
                    pass
                self.hub.broadcast(d)
        finally:
            try:
                logf.close()
            except Exception:
                pass
            self.alive = False
            self.hub.dead()

    def write(self, b: bytes) -> bool:
        if not self.alive or self.proc is None:
            return False
        if host.IS_WIN:
            line = (json.dumps({"op": "send", "b64": _b64(b)}) + "\n").encode()
            with self._feed_lock:
                conn = self.feed_conn
                if conn is None:
                    return False
                try:
                    conn.sendall(line)
                    return True
                except OSError:
                    return False
        try:
            self.proc.stdin.write(b)
            self.proc.stdin.flush()
            return True
        except (ValueError, OSError):
            return False

    def terminate(self) -> None:
        if host.IS_WIN:
            self._close_feed()
        p = self.proc
        if p is None:
            return
        if host.IS_WIN:
            try:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, timeout=8)
            except Exception:
                pass
        else:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


# ───────────────────────────────────────── 守护主体
class Daemon:
    def __init__(self, device_name: str, bind: str, port: int):
        self.name = device_name
        self.bind = bind
        self.ring = Ring()
        self.hub = Hub()
        self.pane_log = _P.runtime_dir("live") / f"{device_name}.pane.log"
        dev = self._resolve()
        self.bridge = Bridge(dev, self.ring, self.hub, self.pane_log)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((bind, port))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self.rows, self.cols = 40, 140

    def _resolve(self) -> dict:
        """取设备 dict。优先顺序：env NETDEV_DEVICE_JSON（ad-hoc 临时目标）→ devices.toml 按名查。"""
        import json as _json2
        raw = os.environ.get("NETDEV_DEVICE_JSON")
        if raw:
            try:
                d = _json2.loads(raw)
                if isinstance(d, dict) and d.get("name") == self.name:
                    return d
            except Exception:
                pass
        from lib import engine
        devs = engine.load_devices()
        if self.name in devs:
            return dict(devs[self.name])
        try:
            return dict(engine.get_device(self.name))
        except Exception:
            raise SystemExit(f"[pane-daemon] 找不到设备 {self.name!r}")

    # ── 注册表
    def _write_reg(self):
        d = _P.state_dir() / "panes"
        d.mkdir(parents=True, exist_ok=True)
        reg = {"device": self.name, "pid": os.getpid(), "host": self.bind,
               "port": self.port, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "command": self.bridge.command, "size": self.bridge.size}
        (d / f"{self.name}.json").write_text(
            json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")

    def _rm_reg(self):
        try:
            (_P.state_dir() / "panes" / f"{self.name}.json").unlink()
        except Exception:
            pass

    # ── 连接处理
    def _handle(self, conn: socket.socket):
        f = conn.makefile("rwb", buffering=0)
        sid: int | None = None
        writer_stop = threading.Event()

        def _writer(q: queue.Queue):
            try:
                while not writer_stop.is_set():
                    try:
                        b = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if b is None:
                        f.write((json.dumps({"event": "dead"}) + "\n").encode())
                        continue
                    f.write((json.dumps({"event": "data", "b64": _b64(b)}) + "\n").encode())
            except (ValueError, OSError):
                pass

        writer_t = None
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    f.write(b'{"ok":false,"error":"bad json"}\n')
                    continue
                op = msg.get("op")
                if op == "alive":
                    f.write((json.dumps({"ok": True, "alive": self.bridge.alive,
                                         "pid": (self.bridge.proc.pid if self.bridge.proc else 0),
                                         "command": self.bridge.command,
                                         "size": f"{self.cols}x{self.rows}"}) + "\n").encode())
                elif op == "send":
                    data = str(msg.get("data", "")).encode("utf-8", "replace")
                    f.write(b'{"ok":true}\n' if self.bridge.write(data)
                            else b'{"ok":false,"error":"bridge dead"}\n')
                elif op == "send-key":
                    keys = {"Enter": b"\r", "Space": b" ",
                            "C-c": b"\x03", "C-u": b"\x15"}
                    k = str(msg.get("key"))
                    b = keys.get(k)
                    if b is None and len(k) == 1:
                        # 对齐 tmux send-keys 语义：单字符键名按字面发送（如 y/n/q）
                        b = k.encode("utf-8", "replace")
                    if b is None:
                        f.write(b'{"ok":false,"error":"unknown key"}\n')
                    else:
                        f.write(b'{"ok":true}\n' if self.bridge.write(b)
                                else b'{"ok":false,"error":"bridge dead"}\n')
                elif op == "capture":
                    n = int(msg.get("tail_bytes", RING_MAX))
                    b = self.ring.tail(n)
                    f.write((json.dumps({"ok": True, "b64": _b64(b)}) + "\n").encode())
                elif op == "resize":
                    self.rows = int(msg.get("rows", self.rows))
                    self.cols = int(msg.get("cols", self.cols))
                    f.write(b'{"ok":true}\n')
                elif op == "respawn":
                    self.bridge.terminate()
                    ok = self.bridge.spawn()
                    f.write((json.dumps({"ok": ok}) + "\n").encode())
                elif op == "subscribe":
                    sid, q = self.hub.add()
                    # ★ 2026-10-07 修：先回 {"ok":true}，再发 tail 整屏快照。
                    #   客户端 pane.subscribe() 用 Control.request() 收应答，而它只读
                    #   【一行】。原来先发 {"event":"data"}（tail），那一行会被当成应答
                    #   吞掉 —— 于是订阅瞬间整屏历史就丢了：打开终端是空屏，只有之后的
                    #   新输出才出现（用户看到的就是"终端没内容/像卡住"）。
                    f.write(b'{"ok":true}\n')
                    writer_t = threading.Thread(target=_writer, args=(q,), daemon=True)
                    writer_t.start()
                    if msg.get("tail", True):
                        tail_b = self.ring.tail()
                        if tail_b:
                            f.write((json.dumps({"event": "data", "b64": _b64(tail_b)})
                                     + "\n").encode())
                elif op == "kill":
                    f.write(b'{"ok":true}\n')
                    self._stop.set()
                    # 响应写完后退出
                    def _go():
                        time.sleep(0.4)
                        self.bridge.terminate()
                        try:
                            self.srv.close()
                        except Exception:
                            pass
                        os._exit(0)
                    threading.Thread(target=_go, daemon=True).start()
                else:
                    f.write(b'{"ok":false,"error":"unknown op"}\n')
        except (ValueError, OSError):
            pass
        finally:
            writer_stop.set()
            if sid is not None:
                self.hub.remove(sid)
            try:
                f.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def serve(self):
        self._write_reg()          # 先写：桥（ssh_bridge）启动时要从注册表读尺寸
        if not self.bridge.spawn():
            sys.stderr.write("[pane-daemon] 桥未能启动，守护仍提供控制口\n")
        self._write_reg()          # 再写：补上 command / 实际 size
        try:
            while not self._stop.is_set():
                try:
                    conn, _addr = self.srv.accept()
                except OSError:
                    break
                conn.settimeout(None)
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            self._rm_reg()


def main() -> int:
    ap = argparse.ArgumentParser(description="netdev pane-daemon（Windows 同屏会话守护）")
    ap.add_argument("--device", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    d = Daemon(a.device, a.bind, a.port)
    print(f"[pane-daemon] {a.device} 控制口 {a.bind}:{d.port} pid {os.getpid()}", flush=True)
    d.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
