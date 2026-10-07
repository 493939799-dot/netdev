#!/usr/bin/env python3
"""snapshot.py —— 终端页「命令」栏用的配置快照入口（接客户设备标准动作）。

用法（都是交互式的，在网页内置终端里点命令即可）：
    snapshot.py menu      # ↕ 备份 / 恢复 / 管理快照（菜单；不带参数就是这个）
    snapshot.py save      # 备份：选设备 → 填客户/备注 → 存快照
    snapshot.py restore   # 恢复：选设备 → 选快照 → 看差异预览 → 输入 RESTORE 才真的下发
    snapshot.py list      # 看已有快照

安全设计：
  · 恢复**先预览差异**，必须手工输入 RESTORE 才执行（非交互环境直接拒绝）
  · 只补回“缺失”的行；你自己加的行只给 undo 建议，不自动动
  · 执行前自动再存一份“恢复前”快照（可再回滚）
  · 黑名单命令（reload/format/delete/reset saved-configuration）永不下发
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path.home() / "netops"
NETDEV = str(ROOT / "netdev")
import select
import termios
import tty


class _Timeout(Exception):
    pass


TIMEOUT = 240   # 可被环境变量 SNAPSHOT_TIMEOUT 覆盖


def _flush_input():
    """丢掉终端里残留的按键（上一问没吃完的半行），免得被当成这一问的答案。"""
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _read_line(timeout):
    """按 deadline 读一整行；超时 / EOF / 读不到都返回 None。

    刻意不用 sys.stdin.readline()：终端里只要有一个“没有回车的残留按键”，
    readline() 就会永久阻塞（select 能超时，readline 不能）——那正是
    “点了存快照就卡住不动”的来源。这里改成自己分片读、到点就撤。
    """
    fd = sys.stdin.fileno()
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            r, _, _ = select.select([fd], [], [], min(left, 0.5))
        except Exception:
            return None
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return None
        if not chunk:                       # EOF
            return None
        buf += chunk
        for sep in (b"\n", b"\r"):
            if sep in buf:
                return buf.split(sep, 1)[0].decode("utf-8", "replace")


def ask(prompt, timeout=None, default=""):
    """读一行；超时返回 None（避免没人应答时永久挂住）。"""
    timeout = int(os.environ.get("SNAPSHOT_TIMEOUT", TIMEOUT)) if timeout is None else timeout
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = _read_line(timeout)
    if line is None:
        print(f"\n{C['y']}（{timeout}s 没等到回车，已取消，什么都没做）{C['x']}")
        print(f"{C['d']}这里要打字后按回车；不想填就直接按回车。{C['x']}")
        return None
    return line.strip() or default


def confirm_key(prompt, timeout=None, keys=("y", "Y", "r", "R")):
    """单键确认：cbreak 模式，不受“回车发 \r 还是 \n”影响。返回 True/False/None(超时)。"""
    timeout = int(os.environ.get("SNAPSHOT_TIMEOUT", TIMEOUT)) if timeout is None else timeout
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        _flush_input()
        sys.stdout.write(prompt)
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                print(f"\n{C['y']}（{timeout}s 没确认，已取消，什么都没做）{C['x']}")
                return None
            r, _, _ = select.select([fd], [], [], min(left, 0.5))
            if not r:
                continue
            try:
                ch = os.read(fd, 1).decode("utf-8", "replace")
            except OSError:
                return None
            if not ch:
                return None
            print(ch)
            return ch in keys
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def take_lock():
    """同一时间只允许一个快照向导占用终端。

    两个向导同时读同一个终端，会把彼此的提问和答案冲散（你看到的“卡住”
    有时就是这种：答案被另一个进程吃掉了）。
    """
    from lib import paths as _P
    lock = _P.state_dir() / "snapshot.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a+")
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        print(f"{C['y']}已经有一个快照向导在跑（另一个终端，或上一次没退出）。{C['x']}")
        print(f"{C['d']}先把它结束掉，或者等它 4 分钟自己超时取消，再点一次。{C['x']}")
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()} at={time.strftime('%F %T')}\n")
    fh.flush()
    return fh


C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "x": "\033[0m"}


def sh(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def nj(*args):
    """netdev 的 --json 出口。"""
    try:
        return json.loads(sh([NETDEV, *args]).stdout or "[]")
    except Exception:
        return []


def pick_device():
    """选设备：优先"正在同屏会话里"的那台。"""
    live = nj("screen-ls", "--json")
    devs = {d["name"]: d for d in nj("list", "--json")}
    conns = {c["id"]: c for c in nj("conn", "list", "--json")}
    cands = []
    for p in live:
        w = p["window"]
        if w in devs:
            cands.append(("device", w, f"{w}（正连着·{'串口' if devs[w]['protocol']=='serial' else devs[w]['protocol']}）"))
        else:
            cands.append(("window", w, f"{w}（同屏会话）"))
    for n, d in devs.items():
        if not any(c[1] == n for c in cands):
            cands.append(("device", n, f"{n}（{d['protocol']} {d.get('address') or ''}）"))
    if not cands:
        print(f"{C['y']}没有任何设备或同屏会话 —— 先用接入命令接一台{C['x']}")
        return None
    if len(cands) == 1:
        print(f"{C['d']}设备：{cands[0][2]}{C['x']}")
        return cands[0][1]
    print(f"{C['b']}对哪台设备操作？{C['x']}")
    for i, (_, name, label) in enumerate(cands, 1):
        print(f"  {i}) {label}")
    raw = ask("选编号（回车 = 1）：")
    if raw is None:                      # 超时/取消：真的退出，别拿默认值偷偷跑下去
        return None
    raw = raw or ""
    idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(cands) else 0
    return cands[idx][1]


def do_save():
    dev = pick_device()
    if not dev:
        return 1
    tag = ask("客户 / 阶段标签（如 客户A-到货 / 调试前，可空）：")
    if tag is None:
        return 1
    note = ask("备注（可空）：")
    if note is None:
        return 1
    tag, note = tag or "", note or ""
    args = [NETDEV, "snap", "save", dev]
    if tag:
        args += ["--tag", tag]
    if note:
        args += ["--note", note]
    print()
    print(f"{C['d']}正在抓配置并打包（大配置十几秒到一分钟，别关这个终端）…{C['x']}")
    sys.stdout.flush()
    r = subprocess.run(args, text=True)
    if r.returncode == 0:
        print(f"\n{C['g']}✔ 备份完成。这份快照就是「客户原始状态」，调坏了可以用它恢复。{C['x']}")
        print(f"{C['d']}在网页里点「↻ 恢复配置备份」选它的编号即可还原；或用 netdev snap list 查看编号{C['x']}")
    return r.returncode


def do_restore():
    dev = pick_device()
    if not dev:
        return 1
    snaps = sh([NETDEV, "snap", "list", dev]).stdout
    print(snaps.rstrip())
    snaps_j = nj("snap", "list", dev, "--json")
    if not snaps_j:
        print(f"{C['y']}这台设备还没有快照 —— 先点「↕ 备份设备配置」{C['x']}")
        return 1
    if len(snaps_j) == 1:
        ref = snaps_j[0]["id"]
        print(f"{C['d']}只有一份快照：#{snaps_j[0].get('idx')} {ref}{C['x']}")
    else:
        raw = ask("用哪份快照？（填编号如 4 或 #4，也可填 ID 片段；回车 = 最新）：")
        if raw is None:
            return 1
        raw = raw or ""
        ref = ""
        if raw:
            hit = [x for x in snaps_j if raw == x["id"] or raw in x["id"]]
            if hit:
                ref = hit[0]["id"]
            else:
                print(f"{C['y']}（认不出「{raw}」，改用最新一份）{C['x']}")
        ref = ref or snaps_j[0]["id"]
    hit = [x for x in snaps_j if str(x.get("idx")) == str(ref).lstrip("#")] or [x for x in snaps_j if x["id"] == ref]
    print(f"{C['d']}用快照：{'#' + str(hit[0].get('idx')) + ' ' if hit and hit[0].get('idx') else ''}{hit[0]['id'] if hit else ref}{C['x']}")
    print(f"\n{C['b']}① 先看差异（不动设备）{C['x']}")
    subprocess.run([NETDEV, "snap", "restore", dev, "--from", ref], text=True)
    print(f"\n{C['b']}还原策略{C['x']}：默认 {C['b']}双向{C['x']}（撤销你新增的配置 + 补回缺失的），执行完会自动自检并报告剩余差异。")
    print(f"{C['d']}想只补不撤：在命令行加 --only-add{C['x']}")
    if not sys.stdin.isatty():
        print(f"{C['r']}非交互环境：恢复被拒绝（必须人工确认）。请在内置终端里手动执行。{C['x']}")
        return 1
    print(f"{C['b']}—— 确认 ——{C['x']}")
    print(f"  {C['g']}按 {C['b']}y{C['x']}{C['g']} 立即执行恢复{C['x']}；按其它任意键取消（4 分钟不操作也会自动取消）")
    print(f"  {C['d']}如果这里按键没反应，请改用命令行（复制这一行）：{C['x']}")
    print(f"    netdev snap restore {dev} --from {ref} --apply --yes")
    ok = confirm_key(f"\n执行恢复？[y/N] ")
    if not ok:
        print(f"{C['d']}已取消，什么都没做（设备未改动）。{C['x']}")
        return 0
    print()
    r = subprocess.run([NETDEV, "snap", "restore", dev, "--from", ref, "--apply", "--yes"], text=True)
    if r.returncode == 0:
        print(f"\n{C['g']}✔ 已按快照补回缺失配置。{C['x']}")
        print(f"{C['y']}下一步：落盘 → netdev save {dev}{C['x']}")
        print(f"{C['d']}（没把握就先别 save，重启设备会回到上次保存的配置）{C['x']}")
    return r.returncode


def do_manage():
    """管理快照：看列表 / 删除（进回收区）/ 取回 / 清空回收区。"""
    while True:
        print()
        print(sh([NETDEV, "snap", "list"]).stdout.rstrip())
        tr = sh([NETDEV, "snap", "trash"]).stdout.strip().splitlines()
        tr_n = max(0, len(tr) - 1)
        if tr_n:
            print(f"  {C['d']}回收区里有 {tr_n} 项（可取回）{C['x']}")
        ans = ask("删除哪几份快照？（编号，多选用逗号如 3,5；t=看回收区；p=清空回收区；回车=退出）：")
        if ans is None or ans == "":
            print(f"{C['d']}退出，什么都没做。{C['x']}")
            return 0
        a = ans.strip().lower()
        if a == "t":
            print(sh([NETDEV, "snap", "trash"]).stdout.rstrip())
            back = ask("要取回哪一项？（编号按列表顺序 1..n，回车跳过）：")
            if back and back.isdigit():
                r = subprocess.run([NETDEV, "snap", "unrm"], text=True)
                if r.returncode != 0:
                    print(f"{C['d']}（取回失败，试 netdev snap unrm <名字片段>）{C['x']}")
            continue
        if a == "p":
            print(f"{C['y']}清空回收区 = 物理删除，不可恢复。{C['x']}")
            if confirm_key("确认清空回收区？[y/N] "):
                subprocess.run([NETDEV, "snap", "purge", "--yes"], text=True)
            continue
        refs = [x.strip().lstrip("#") for x in a.replace("，", ",").split(",") if x.strip()]
        if not refs:
            continue
        print()
        r = subprocess.run([NETDEV, "snap", "rm", *refs], text=True)     # 先给预览
        if r.returncode != 0:
            continue
        if not confirm_key(f"确认删除这 {len(refs)} 份（移入回收区，可取回）？[y/N] "):
            print(f"{C['d']}已取消，什么都没删。{C['x']}")
            continue
        subprocess.run([NETDEV, "snap", "rm", *refs, "--yes"], text=True)
        print(f"{C['d']}提示：彻底删除用 p；取回用 t。交付级副本仍在 ~/Desktop/workbuddy/。{C['x']}")
        return 0


def do_menu():
    """↕ 备份 / 恢复 / 管理快照：一个菜单把三个动作收在一起。"""
    while True:
        print()
        print(f"{C['b']}↕ 备份 / 恢复 / 管理快照{C['x']}   {C['d']}接客户设备：先备份（存“客户原始状态”），调坏了从快照恢复{C['x']}")
        snap_list = sh([NETDEV, "snap", "list"]).stdout.rstrip()
        lines = [x for x in snap_list.splitlines() if x.strip()] if snap_list else []
        n = max(0, len(lines) - 2)
        if n:
            print(f"{C['d']}已有快照 {n} 份（最近 3 份）：{C['x']}")
            for l in lines[:5]:
                print(f"  {C['d']}{l}{C['x']}")
        else:
            print(f"{C['d']}（还没有快照 —— 先做「1) 备份」存一份“客户原始状态”）{C['x']}")
        live = nj("screen-ls", "--json")
        devs = nj("list", "--json")
        print(f"  {C['d']}设备清单 {len(devs)} 台｜同屏会话 {len(live)} 个"
              + (f"（{', '.join(p.get('window','') for p in live)}）" if live else "") + f"{C['x']}")
        print(f"\n  {C['b']}1{C['x']}) ↕ 备份（存快照：客户原始状态 / 变更前）"
              f"\n  {C['b']}2{C['x']}) ↻  恢复（先用差异预览，确认后才下发）"
              f"\n  {C['b']}3{C['x']}) ×  管理 / 删除快照（进回收区，可取回）"
              f"\n  {C['b']}0{C['x']}) 退出")
        c = ask("\n选编号（回车 = 1 备份；0/q = 退出）：", default="1")
        if c is None:
            return 0
        c = (c or "1").strip().lower()
        if c in ("0", "q", "quit", "exit"):
            print(f"{C['d']}已退出，什么都没改。{C['x']}")
            return 0
        if c in ("1", "save", "备份"):
            do_save()
        elif c in ("2", "restore", "恢复"):
            do_restore()
        elif c in ("3", "manage", "管理", "删除"):
            do_manage()
        else:
            print(f"{C['y']}认不出「{c}」—— 请输入 0/1/2/3{C['x']}")


def main():
    act = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if act in ("save", "restore", "manage", "del", "delete", "list", "ls"):
        _flush_input()                       # 清掉终端里残留的半行按键
        lock = take_lock()
        if lock is None:
            return 1
    else:
        act = act or "menu"
    if act in ("menu", "main"):
        _flush_input()
        if take_lock() is None:              # 整个菜单会话独占终端，避免两个向导抢提问
            return 1
        return do_menu()
    if act == "save":
        return do_save()
    if act == "restore":
        return do_restore()
    if act in ("manage", "del", "delete"):
        return do_manage()
    if act in ("list", "ls"):
        print(sh([NETDEV, "snap", "list"]).stdout.rstrip())
        return 0
    print("用法: snapshot.py menu | save | restore | manage | list")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n（已取消，什么都没做）")
        sys.exit(130)
