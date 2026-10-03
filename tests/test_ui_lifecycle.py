#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：网页服务（netdev-ui）生命周期 —— 「服务又起不来了」的防线（2026-10-01）。

背景（真机上反复踩到的故障）：
    用户报「服务又起不来了」，查下来是三个坑叠在一起：
      1. 产品**根本没有启动入口** —— 只有文档里的 `python ui/server.py`，
         `netdev ui` 这个命令此前不存在；
      2. `ui/启动.command` 用 `exec` 前台跑 —— 关掉终端窗口 = 服务被杀；
      3. 本机 `launchctl bootstrap` 被安全策略拦（5: Input/output error），
         doctor 却一直建议用它，于是「照做也起不来」。
    修法：`netdev ui`（没有就起、有就报状态）成为一等公民命令，
    底层用 double-fork + setsid 真正脱离进程组（ui/daemonize.py）。

本测试验证的**关键性质**（不只是"能跑"）：
    · 起得来 / 健康可得 / 幂等（已在跑不重复起）
    · **真脱离**：子进程与测试进程不在同一进程组（这正是 nohup 做不到、
      而「关终端就死」的根因）—— 用 getpgid 对比来证明，不靠感觉
    · 停得掉 / 重复停不报错
    · CLI 层 `netdev ui` 的 exit code 语义：在跑=0、没跑=1（脚本可依赖）

隔离：全部跑在 8899 端口 + 临时 PID/日志文件，**绝不碰你正在用的 8898 实例**。

用法：
    python3 tests/test_ui_lifecycle.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8899                      # 刻意避开线上 8898
HOST = "127.0.0.1"
BASE = f"http://{HOST}:{PORT}"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def _port_open(port: int = PORT) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((HOST, port)) == 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _health(port: int = PORT) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=4) as r:
            body = r.read(300).decode()
            if r.status == 200 and '"ok": true' in body:
                return {"body": body}
    except Exception:
        pass
    return None


def _wait(pred, timeout=15.0, step=0.4) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ======================================================================
# 一、底层：ui/daemonize.py 的守护语义
# ======================================================================
def test_daemonize(tmp: pathlib.Path):
    print("\n一、daemonize 守护语义（起 → 健康 → 幂等 → 真脱离 → 停）")
    pidf, logf = tmp / "ui.pid", tmp / "ui.log"
    daemon = ROOT / "ui" / "daemonize.py"
    py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

    def run(*args):
        p = subprocess.run([py, str(daemon), "--port", str(PORT), "--host", HOST,
                            "--pidfile", str(pidf), "--logfile", str(logf), *args],
                           capture_output=True, text=True, timeout=90)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    try:
        check("起点：端口应当是关的", not _port_open(), f"8899 意外已被占用，测试中止")
        if _port_open():
            return

        rc, out = run()
        check("start 返回 0", rc == 0, out[-300:])
        check("端口已监听", _wait(_port_open), out[-300:])
        check("PID 文件已写入", pidf.exists())
        pid = int(pidf.read_text().strip()) if pidf.exists() else 0
        check("进程存活", _alive(pid), f"pid={pid}")
        check("健康检查 200", _health() is not None, "GET /api/health")
        check("日志有启动横幅", logf.exists() and "netdev-ui 已启动" in logf.read_text(errors="replace"))

        # ── 真脱离：这是「关终端就死」的根因判据 ──────────────────────
        # double-fork + setsid 之后，服务与调用方**不在同一进程组**。
        # nohup & 做不到这一点（它留在原进程组，父会话结束就被连坐）。
        same_pg = os.getpgid(pid) == os.getpgid(os.getpid())
        check("已脱离调用方进程组（关终端不掉）", not same_pg,
              f"服务 pgid={os.getpgid(pid)} 调用方 pgid={os.getpgid(os.getpid())}")

        # ── 幂等：已经在跑就别重复起 ──────────────────────────────────
        rc2, out2 = run()
        pid2 = int(pidf.read_text().strip())
        check("重复 start 返回 0", rc2 == 0, out2[-300:])
        check("重复 start 被识别为已在跑", "已在监听" in out2 or "已在后台运行" in out2, out2[-300:])
        check("重复 start 没有换进程（PID 不变）", pid2 == pid, f"{pid} -> {pid2}")

        # ── 停 ────────────────────────────────────────────────────────
        rc3, out3 = run("--stop")
        check("stop 返回 0", rc3 == 0, out3[-300:])
        check("端口已关闭", _wait(lambda: not _port_open()), out3[-300:])
        check("进程已退出", _wait(lambda: not _alive(pid)), f"pid={pid}")
        check("PID 文件已清理", not pidf.exists())

        rc4, out4 = run("--stop")
        check("重复 stop 不报错（优雅）", rc4 == 0 and "没找到进程" in out4, out4[-200:])
    finally:
        run("--stop")


# ======================================================================
# 二、CLI 层：netdev ui（真正的修复点）
# ======================================================================
def test_cli(tmp: pathlib.Path):
    print("\n二、netdev ui 命令（exit code 语义 + ensure 自动拉起）")
    pidf, logf = tmp / "cli.pid", tmp / "cli.log"
    env = dict(os.environ)
    env.update({
        "NETDEV_UI_PORT": str(PORT),
        "NETDEV_UI_HOST": HOST,
        "NETDEV_UI_PIDFILE": str(pidf),
        "NETDEV_UI_LOGFILE": str(logf),
    })
    py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

    def ui(*args):
        p = subprocess.run([py, str(ROOT / "netdev_cli.py"), "ui", *args],
                           capture_output=True, text=True, timeout=90, env=env)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    try:
        rc, out = ui("status")
        check("没在跑时 status 退出码=1（脚本可依赖）", rc == 1, f"rc={rc}\n{out[-300:]}")
        check("status 明确说「不通」", "不通" in out, out[-300:])

        rc, out = ui()                       # 默认 ensure：没有就起
        check("ensure 自动拉起并返回 0", rc == 0, out[-300:])
        check("ensure 后健康可得", _wait(lambda: _health() is not None), out[-300:])
        check("ensure 输出提示「关终端不受影响」", "关掉终端" in out, out[-300:])

        rc, out = ui("status")
        check("在跑时 status 退出码=0", rc == 0, f"rc={rc}\n{out[-300:]}")
        check("status 报出 PID", "存活" in out, out[-300:])

        rc, out = ui("log", "-n", "3")
        check("log 能读到启动横幅", rc == 0 and "netdev-ui 已启动" in out, out[-300:])

        rc, _ = ui("stop")
        check("stop 返回 0", rc == 0)
        check("stop 后端口关闭", _wait(lambda: not _port_open()))

        # 真脱离也要在 CLI 路径上成立（CLI → daemonize，链路一致）
        ui()
        _wait(lambda: _health() is not None)
        pid = int(pidf.read_text().strip())
        check("CLI 起的实例同样脱离进程组",
              not (os.getpgid(pid) == os.getpgid(os.getpid())), f"pid={pid}")
    finally:
        ui("stop")


def main() -> int:
    print("=" * 68)
    print("回归测试：netdev-ui 生命周期（隔离端口 %d，不碰线上 8898）" % PORT)
    print("=" * 68)
    if _port_open():
        print(f"\n✘ 端口 {PORT} 已被占用，测试无法安全进行。请先释放后重跑。")
        return 2
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="netdev-ui-test-"))
    try:
        test_daemonize(tmp)
        test_cli(tmp)
    finally:
        # 只删本测试自己的临时目录；服务已在各段 finally 里停掉
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
    print("=" * 68)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
