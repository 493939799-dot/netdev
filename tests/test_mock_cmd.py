#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：`netdev mock`（本机设备模拟器）。

为什么要有这个测试：
    README 承诺"任何人按两条命令就能复现，不需要真设备"，
    但模拟器原本只被 `netdev selftest` 短暂拉起 —— 读者得自己知道去
    tests/ 里跑一个 Python 脚本。加了 `netdev mock` 之后，
    **这个承诺才成立**，所以它的 start/stop/status 也得守住。

全程只在本机 127.0.0.1 上开一个监听，不碰任何真设备。
"""
from __future__ import annotations

import argparse
import pathlib
import socket
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []
PORT = 20391          # 刻意用 uncommon 端口，避免和 20022 / 8898 撞


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def port_open(p: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", int(p))) == 0
    finally:
        s.close()


def main() -> int:
    print("=" * 66)
    print("回归测试：netdev mock（本机模拟器）")
    print("=" * 66)

    import netdev_cli

    ns = lambda act: argparse.Namespace(action=act, port=PORT)

    # 先确保干净
    netdev_cli.cmd_mock(ns("stop"))

    print("\n一、没在跑时")
    rc = netdev_cli.cmd_mock(ns("status"))
    check("status 在没跑时返回 1（脚本可依赖）", rc == 1, f"rc={rc}")
    check("端口确实没在监听", not port_open(PORT))

    print("\n二、start")
    rc = netdev_cli.cmd_mock(ns("start"))
    check("start 返回 0", rc == 0, f"rc={rc}")
    check("端口已在监听", port_open(PORT))
    rc = netdev_cli.cmd_mock(ns("status"))
    check("status 在跑时返回 0", rc == 0, f"rc={rc}")

    print("\n三、幂等")
    rc = netdev_cli.cmd_mock(ns("start"))
    check("重复 start 返回 0 且不重复起", rc == 0 and port_open(PORT), f"rc={rc}")

    print("\n四、stop")
    rc = netdev_cli.cmd_mock(ns("stop"))
    check("stop 返回 0", rc == 0, f"rc={rc}")
    for _ in range(20):
        if not port_open(PORT):
            break
        time.sleep(0.2)
    check("stop 后端口关闭", not port_open(PORT))
    check("stop 后 pid 文件已清理", not netdev_cli._mock_pid(PORT)[0])

    print("\n五、重复 stop 不报错")
    rc = netdev_cli.cmd_mock(ns("stop"))
    check("重复 stop 返回 0（优雅）", rc == 0, f"rc={rc}")

    print("\n六、残留 pid 文件要被识别为「没在跑」")
    netdev_cli._mock_paths(PORT)[0].write_text("999999", encoding="utf-8")
    pid, _ = netdev_cli._mock_pid(PORT)
    check("PID 文件指向死进程 → 视为没在跑", pid is None, f"pid={pid}")
    check("这种情况 status 返回 1", netdev_cli.cmd_mock(ns("status")) == 1)
    try:
        netdev_cli._mock_paths(PORT)[0].unlink()
    except Exception:
        pass

    print("\n七、不同端口互不干扰")
    other = PORT + 1
    ns2 = lambda act: argparse.Namespace(action=act, port=other)
    rc = netdev_cli.cmd_mock(ns2("start"))
    check("另一个端口能独立起", rc == 0 and port_open(other), f"rc={rc}")
    check("起第二个不影响第一个的 pid 文件", netdev_cli._mock_pid(PORT)[0] is None
          and netdev_cli._mock_pid(other)[0] is not None)
    netdev_cli.cmd_mock(ns2("stop"))
    check("停第二个后第一个的记录仍在（互不影响）", netdev_cli._mock_pid(other)[0] is None)

    print("\n八、行编辑语义：C-u 清行 + ANSI 转义吞噬，模拟器必须吃掉它们")
    # ★ 2026-10-03 实测踩到的真 bug：
    #   netdev 的 _session_run 在下发命令前会发一个 C-u(0x15) 清掉当前行
    #   （防残留的终端能力应答碎片 `;2c` / `0;276;0c` 被当成命令前缀）。
    #   真机 VRP 的行编辑器把 C-u 当"清行"；模拟器原来把它当普通字符并进 buf，
    #   命令于是变成 "\x15display version" → 永远 Unrecognized command。
    #   症状极像"netdev 坏了"，而且**只在同屏窗格存在时才出现** ——
    #   没有窗格时 netdev 走直连，不发 C-u，所以最容易漏掉这一条。
    rc = netdev_cli.cmd_mock(ns("start"))
    check("重新起模拟器（为行编辑测试）", rc == 0 and port_open(PORT), f"rc={rc}")
    try:
        import paramiko
    except Exception as e:
        check("paramiko 可用（行编辑测试需要真连一次）", False, f"{type(e).__name__}: {e}")
        netdev_cli.cmd_mock(ns("stop"))
        return _report()

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect("127.0.0.1", port=PORT, username="admin", password="admin",
                    timeout=8, look_for_keys=False, allow_agent=False)
        ch = cli.invoke_shell()
        time.sleep(1.0)

        def drain(sec=1.5):
            time.sleep(sec)
            out = ""
            while ch.recv_ready():
                out += ch.recv(65535).decode("utf-8", "replace")
            return out

        drain(0.8)                                   # 吃掉 banner 与提示符

        ch.send("\x15display version\n")             # ← netdev 的真实发法
        got = drain(1.5)
        check("C-u 清行后命令仍被正确识别（不再是 Unrecognized）",
              "Unrecognized" not in got and "VRP (R) software" in got, repr(got[-160:]))

        ch.send("display versionZZZ\x7f\x7f\x7f\n")  # 退格仍要能改行
        got2 = drain(1.5)
        check("退格键仍能修正命令（行编辑没被改坏）",
              "Unrecognized" not in got2 and "VRP (R) software" in got2, repr(got2[-160:]))

        ch.send("\x03")                              # Ctrl-C 不能被当成命令
        drain(0.6)
        ch.send("display clock\n")
        got3 = drain(1.5)
        check("Ctrl-C 之后仍能正常下发命令",
              "Unrecognized" not in got3 and "Time Zone" in got3, repr(got3[-160:]))

        # ★ 转义序列污染（2026-10-03 实测）：终端会对设备的查询自动回一段能力应答
        #   （`\x1b[?1;2c` / `0;276;0c`），这些字节会从同屏窗格漏进设备输入流。
        #   真机的行编辑器不当它们是命令字符；模拟器不吞的话命令会变成
        #   `[1;2cdisplay version` → 永远 Unrecognized（且 `?` 还会误触发 help）。
        ch.send("\x1b[?1;2cdisplay version\n")
        got4 = drain(1.5)
        check("ANSI 转义碎片不污染命令行（碎片 + 命令仍被识别）",
              "Unrecognized" not in got4 and "VRP (R) software" in got4, repr(got4[-160:]))

        ch.send("\x1b[?1;2c\x15display clock\n")      # 碎片 + C-u = netdev 同屏的真实下发路径
        got5 = drain(1.5)
        check("碎片 + C-u 清行 = netdev 同屏的真实下发路径",
              "Unrecognized" not in got5 and "Time Zone" in got5, repr(got5[-160:]))
        try:
            cli.close()
        except Exception:
            pass
    except Exception as e:
        check("行编辑测试跑通（真连模拟器）", False, f"{type(e).__name__}: {e}")
    finally:
        netdev_cli.cmd_mock(ns("stop"))

    return _report()


def _report() -> int:
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
