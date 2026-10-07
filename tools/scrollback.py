#!/usr/bin/env python3
"""scrollback.py —— 回看同屏会话的历史（把过去的内容打到当前终端里）。

为什么需要它：在 tmux 里回看要「Ctrl+B 再按 [」或鼠标滚轮进 copy-mode，
在网页内置终端里不一定顺手。这个命令直接把历史**打印成普通文本**，
于是它就躺在终端自己的回滚缓冲里 —— 滚轮/触控板一定能翻，也能直接复制。

用法:
    scrollback.py               # 自动选（只有一个会话时）；多个会话时报编号
    scrollback.py --lines 2000  # 多打一些（默认 1000）
    scrollback.py --list        # 只列同屏会话
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path.home() / "netops"
NETDEV = str(ROOT / "netdev")
C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "y": "\033[33m", "x": "\033[0m"}


def sh(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def live_windows():
    try:
        return json.loads(sh([NETDEV, "screen-ls", "--json"]).stdout or "[]")
    except Exception:
        return []


def main() -> int:
    argv = sys.argv[1:]
    lines = 1000
    only_list = "--list" in argv
    if "--lines" in argv:
        i = argv.index("--lines")
        if i + 1 < len(argv):
            try:
                lines = max(20, min(20000, int(argv[i + 1])))
            except ValueError:
                pass

    panes = live_windows()
    if not panes:
        print(f"{C['y']}当前没有同屏会话{C['x']}（先用接入命令接一台设备）")
        return 1
    if only_list:
        for i, p in enumerate(panes, 1):
            print(f"  {i}) {p['window']}  {p['size']}  {p.get('command','')}")
        return 0

    if len(panes) == 1:
        win = panes[0]["window"]
    elif not sys.stdin.isatty():
        # 非交互（管道/脚本调用）：自动选最近一个，不问
        win = panes[-1]["window"]
        print(f"{C['d']}（非交互环境：自动选最近的 {win}；要指定：scrollback.py --list 后自行调用 netdev screen-read）{C['x']}")
    else:
        print(f"{C['b']}要回看哪个会话？{C['x']}")
        for i, p in enumerate(panes, 1):
            print(f"  {i}) {p['window']}  {C['d']}{p['size']}  {p.get('command','')}{C['x']}")
        raw = input("选编号（回车 = 最近一个）：").strip()
        win = panes[int(raw) - 1]["window"] if raw.isdigit() and 1 <= int(raw) <= len(panes) else panes[-1]["window"]

    r = sh([NETDEV, "screen-read", win, "--lines", str(lines)])
    text = (r.stdout or "").rstrip("\n")
    n = text.count("\n") + 1 if text else 0
    print(f"{C['b']}── {win} 最近 {n} 行（最多 {lines} 行）{'─' * 10}{C['x']}")
    print(text if text else "(空屏)")
    print(f"{C['b']}{'─' * 46}{C['x']}")
    print(f"{C['d']}以上内容已经在你自己终端的回滚缓冲里：滚轮/触控板往上翻即可（要更多：再跑一次并加 --lines 3000）{C['x']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
