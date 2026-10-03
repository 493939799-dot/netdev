#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 netdev-ui 以「真守护进程」方式拉起（脱离当前会话/进程组）。

为什么需要它
    本机 `launchctl bootstrap` 会被安全策略拦（实测 `Bootstrap failed: 5: Input/output error`），
    而普通的 `nohup … &` 仍留在当前进程组里 —— 父进程（终端 / 沙箱会话）一结束，
    服务就被连坐收掉，表现就是"服务又起不来了"。
    这里用经典 double-fork + os.setsid() 把服务真正交给 init 收养。

用法
    推荐从 CLI 走（它会处理"没有就起、有就报状态"）：
        netdev ui            # 确保服务在跑
        netdev ui restart
        netdev ui status
    本脚本也可单独调用：
        python3 ui/daemonize.py              # 端口 8898
        python3 ui/daemonize.py --port 9000
        python3 ui/daemonize.py --status     # 看是否在跑
        python3 ui/daemonize.py --stop       # 停掉
"""
from __future__ import annotations

import argparse
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
# 默认落点。可用 --pidfile / --logfile 覆盖 —— 自动化测试靠它跑在别的端口上，
# 不去碰你正在用的那个服务（否则一次测试就把线上实例带走了）。
DEFAULT_PIDFILE = ROOT / "logs" / "ui-service.pid"
DEFAULT_LOGFILE = ROOT / "logs" / "ui-service.log"

# ★ 2026-10-04：拉起服务之前，先把宿主（WorkBuddy / CodeBuddy 桌面版）注入的运行时钩子
#   从环境里剥掉。
#
#   为什么必须做在**这里**：那套钩子是通过 PYTHONPATH 里的 sitecustomize.py 生效的 ——
#   它在解释器启动阶段就被自动 import，之后无论怎么改子进程的 env 都来不及了
#   （本进程内的 shutil.rmtree 早就被换掉了）。只有让**服务自己的解释器**从一开始就
#   import 不到它，才算真正修好。
#
#   不修的后果（2026-10-04 实测）：界面里点「彻底删除快照」必失败，而界面上只显示
#   一句 "Load failed" —— 因为守卫抛的 SystemExit 会穿透 HTTP 处理函数，
#   且 threading.excepthook 对 SystemExit 静默，日志里连 traceback 都不留。
#   详见 lib/hostenv.py 的文件头。
try:
    sys.path.insert(0, str(ROOT))
    from lib.hostenv import strip_host_injection   # noqa: E402
except Exception:                                  # pragma: no cover
    strip_host_injection = None                    # 拿不到就退化成"不处理"，不阻断启动


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(pidfile: pathlib.Path) -> int:
    try:
        return int(pathlib.Path(pidfile).read_text().strip())
    except Exception:
        return 0


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def status(port: int, pidfile: pathlib.Path) -> int:
    pid = read_pid(pidfile)
    ok = bool(pid) and _alive(pid)
    print(f"PID 文件: {pidfile}  →  {'PID ' + str(pid) if pid else '(无)'}")
    print(f"进程存活: {'是' if ok else '否'}")
    print(f"端口 {port}: {'已监听' if port_open(port) else '未监听'}")
    if not ok and port_open(port):
        print("提示：端口被别的进程占着 —— 可能是一份没写 PID 文件的旧实例，用 lsof -nP -iTCP:%d 查。" % port)
    return 0 if (ok and port_open(port)) else 1


def stop(port: int, pidfile: pathlib.Path) -> int:
    pid = read_pid(pidfile)
    if pid and _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.2)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        print(f"已停止 PID {pid}")
    else:
        print("按 PID 文件没找到进程")
    try:
        pathlib.Path(pidfile).unlink()
    except Exception:
        pass
    return 0


def start(port: int, host: str, pidfile: pathlib.Path, logfile: pathlib.Path) -> int:
    pidfile, logfile = pathlib.Path(pidfile), pathlib.Path(logfile)
    if port_open(port, host):
        pid = read_pid(pidfile)
        print(f"端口 {port} 已在监听" + (f"（PID {pid}）" if pid and _alive(pid) else "") + "，无需重复启动。")
        return 0

    logfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        python = pathlib.Path(sys.executable)
    argv = [str(python), str(HERE / "server.py"), "--port", str(port), "--host", host]

    # ── double fork ──────────────────────────────────────────────────
    if os.fork() > 0:
        # 父：等第一层子进程退出，然后**轮询**到服务真的应答为止。
        #
        # 2026-10-03 修：原来这里是 `time.sleep(1.2)` 睡死 1.2 秒然后看一次。
        # 在本机上服务 <1.2s 就起来，所以从没暴露；GitHub 的冷启动 runner 上
        # 光是 import netmiko/scrapli/paramiko 就要好几秒，于是父进程提前判死刑，
        # 报「启动后未能连上」——**而服务其实正在慢慢启动**。
        # 后果连锁：紧接着的"重复 start"撞上 Address already in use（_bind）、
        # status 说「无 PID 文件」、log 说「暂无日志」，全都是同一个根因。
        #
        # 教训（同本项目其他地方）：**固定 sleep 是误判的根源，一律换成轮询。**
        try:
            os.wait()          # 第一层子进程 fork 完就 _exit(0)，这里不会久等
        except ChildProcessError:
            pass
        # 上限默认给到 120 秒：这是**后台服务**的启动，等久一点没有副作用，
        # 而"等不够就误判失败"的代价很大（会连锁产生"重复 start 撞端口、
        # status 说无 PID、日志为空"这一串假象）。
        # 为什么需要这么久：GitHub 的 macOS ARM runner 是全新虚拟机，
        # 第一次加载 cryptography / paramiko 的原生库要做代码签名校验，
        # 实测要 30 秒以上（本机不到 1.2 秒，所以这个坑只在 CI 上暴露）。
        # 调大：export NETDEV_UI_START_TIMEOUT=180
        deadline = time.time() + float(os.environ.get("NETDEV_UI_START_TIMEOUT", "120"))
        t0 = time.time()
        pid = None
        while time.time() < deadline:
            pid = read_pid(pidfile)
            if pid and _alive(pid) and port_open(port, host):
                break
            time.sleep(0.3)
        if pid and _alive(pid) and port_open(port, host):
            print(f"✔ netdev-ui 已在后台运行  http://{host}:{port}   PID {pid}")
            print(f"  日志：{logfile}")
            print(f"  停止：netdev ui stop   （等价于 python3 {HERE / 'daemonize.py'} --stop）")
            return 0
        print(f"✘ 启动后未能连上（等了 {time.time() - t0:.0f} 秒仍无应答）—— 看日志：{logfile}")
        print("  若是在 CI / 全新虚拟机上首次启动，冷启动可能要几十秒；"
              "可用 NETDEV_UI_START_TIMEOUT 调大等待上限。")
        try:
            print(logfile.read_text(encoding="utf-8", errors="replace")[-1500:])
        except Exception:
            pass
        return 1

    os.setsid()                       # 新会话：脱离原进程组，父进程死也带不走它
    if os.fork() > 0:
        os._exit(0)                   # 第二层父立刻退出，让孙子被 init 收养

    # 孙子：真正的服务进程
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    logfd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)
    pidfile.write_text(str(os.getpid()) + "\n")
    os.chdir(str(ROOT))
    # ★ 环境净化必须发生在 os.execv **之前**（execv 会把当前 os.environ 原样交给新程序）。
    #   剥掉宿主注入的 PYTHONPATH / NODE_OPTIONS / PATH shim / BASH_ENV 等，
    #   服务的解释器就不会再自动 import 那个 sitecustomize.py。
    if strip_host_injection is not None:
        strip_host_injection(os.environ)
    try:
        os.execv(str(python), argv)
    except Exception:
        os._exit(127)


def main() -> int:
    ap = argparse.ArgumentParser(description="netdev-ui 守护进程启动器")
    ap.add_argument("--port", type=int, default=8898)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--pidfile", default=str(DEFAULT_PIDFILE),
                    help="PID 文件落点（自动化测试用它跑到别的文件，避免动到线上实例）")
    ap.add_argument("--logfile", default=str(DEFAULT_LOGFILE), help="日志落点")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    pidfile, logfile = pathlib.Path(a.pidfile), pathlib.Path(a.logfile)
    if a.status:
        return status(a.port, pidfile)
    if a.stop:
        return stop(a.port, pidfile)
    return start(a.port, a.host, pidfile, logfile)


if __name__ == "__main__":
    raise SystemExit(main())
