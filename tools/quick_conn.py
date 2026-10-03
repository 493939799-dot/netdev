#!/usr/bin/env python3
"""quick_conn.py —— 接入与管理设备目标（给 pi-web-ui 终端页的「命令」栏用）。

用法:
    quick_conn.py quick          # → 快速接入（菜单：SSH / Telnet / 串口）—— 不带参数就是这个
    quick_conn.py manage         # ≡ 管理接入目标（菜单：接入三种 / 列出 / 新增 / 删除）
    quick_conn.py list           # 只列出已接入目标（连接簿 + 设备清单 + 在线会话）
    quick_conn.py add            # 新增接入目标（写进 netdev 连接簿）
    quick_conn.py rm <名称|id> --yes   # 删连接簿条目（不带 --yes 则交互确认）
    quick_conn.py ssh            # 直接接 SSH（跳过菜单；选连接簿/正式设备，或输 主机[:端口]）
    quick_conn.py telnet         # 同上
    quick_conn.py serial         # 列本机串口，选设备 + 波特率
    quick_conn.py ssh --list     # 只列出候选，不交互
    quick_conn.py ssh --dry-run  # 打印将要执行的 netdev 命令，不真正接入

设计：设备能力全在 netdev（连接簿 / 同屏会话 / 镜像留档 / 三级闸门），本脚本只是
「选目标 → 调 netdev → attach 同屏会话」的交互外壳；接好后 AI 也能在同一块屏上操作。
"""
from __future__ import annotations

import json
import os
import pathlib
import select
import subprocess
import sys
import termios
import time

HOME = pathlib.Path.home()
ROOT = HOME / "netops"
NETDEV = str(ROOT / "netdev")
TMUX = next((p for p in (HOME / "homebrew/bin/tmux", HOME / "bin/tmux",
                         "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")
             if pathlib.Path(p).exists()), None)
# 依赖面刻意保持最小：只用 stdlib + netdev CLI（不 import netmiko，venv 换了也不影响"列清单"）

KINDS = {"ssh": "SSH", "telnet": "Telnet", "serial": "串口"}
C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "x": "\033[0m"}


def ask(prompt, default="", timeout=None):
    """读一行；超时/取消返回 None（绝不永久卡住）。

    不用 input()/readline()：终端里只要有一个“没回车的残留按键”，就会永久阻塞。
    """
    timeout = int(os.environ.get("QUICK_CONN_TIMEOUT", 240)) if timeout is None else timeout
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return None if line == "" else (line.strip() or default)
    fd = sys.stdin.fileno()
    buf, deadline = b"", time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], min(0.5, max(0.05, deadline - time.time())))
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        for sep in (b"\n", b"\r"):
            if sep in buf:
                return buf.split(sep, 1)[0].decode("utf-8", "replace").strip() or default
    print(f"\n{C['y']}（{timeout}s 没等到输入，已取消）{C['x']}")
    return None


def _flush_stdin():
    """丢掉终端里残留的半行按键（免得被当成菜单选项）。"""
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def sh(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _netdev_json(*args):
    """拿 netdev CLI 的机器可读输出（CLI 是唯一真相来源，这里不重复实现任何设备逻辑）。"""
    r = sh([NETDEV, *args])
    try:
        data = json.loads(r.stdout or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_devices():
    return _netdev_json("list", "--json")


def load_conns():
    return _netdev_json("conn", "list", "--json")


def live_windows():
    return {p["window"]: p for p in _netdev_json("screen-ls", "--json")}


def serial_ports():
    """可用串口：所有 /dev/cu.*（排除蓝牙/调试等系统假串口）。

    这样换任何 USB 转串口线（FTDI/CH340/CP210x/带 SPP 的设备）都能自动认出来。
    """
    skip_suffix = ("-Incoming-Port", "-Modem")
    return sorted(f"/dev/{p.name}" for p in pathlib.Path("/dev").glob("cu.*")
                  if not p.name.endswith(skip_suffix) and p.name != "cu.debug-console")


def candidates(kind):
    """候选目标：正式设备（☆）+ 连接簿条目，按协议过滤，标出是否已在同屏会话里。"""
    live = live_windows()
    out = []
    for d in load_devices():
        if d.get("protocol") != kind:
            continue
        addr = d.get("address") or ""
        out.append({"kind": "device", "key": d["name"], "name": d["name"], "addr": addr,
                    "window": d["name"], "official": True, "live": d["name"] in live})
    for c in load_conns():
        if c.get("protocol") != kind:
            continue
        out.append({"kind": "conn", "key": c["id"], "name": c["name"], "addr": c.get("address", ""),
                    "window": None, "official": False, "live": False, "uri": c.get("uri")})
    return out


def prompt_kind():
    print(f"{C['b']}接入方式：{C['x']}1) SSH   2) Telnet   3) 串口")
    return {"1": "ssh", "2": "telnet", "3": "serial"}.get((ask("选 1/2/3（回车取消）：") or ""), "")


def build_cmd(choice, kind):
    """选择（编号或自由输入）→ (netdev 子命令参数, 说明)。"""
    if kind == "serial":
        dev_path = choice.strip()
        # ☆ 该串口在设备清单里有名字 → 走设备名：自动探测波特率 + 复用已有同屏会话
        #   （以前这里硬造 serial:/dev/...@9600，会绕开 devices.toml，速率错、还和已有会话抢串口）
        named = next((d for d in load_devices()
                      if d.get("protocol") == "serial" and d.get("port") == dev_path), None)
        if named:
            return ["shell", named["name"]], f"设备 {named['name']}（波特率自动探测）"
        baud = BAUD if BAUD and BAUD != "9600" else "auto"
        return ["shell", f"serial:{dev_path}@{baud}"], f"{dev_path} @ {baud}"
    if "raw" in choice:                      # 自由输入（主机[:端口]）
        raw = str(choice["raw"]).strip()
        if "://" not in raw:
            raw = f"{kind}://{raw}"
        return ["shell", raw], raw
    if choice["kind"] == "device":
        return ["shell", choice["key"]], f"设备 {choice['name']}"
    if choice["kind"] == "conn":
        return ["conn", "connect", choice["key"]], f"连接簿 {choice['name']}（{choice['addr']}）"
    raise SystemExit(f"✘ 认不出的选择：{choice!r}")


def main_add():
    """交互式添加接入目标（含端口）→ 写入 netdev 连接簿。"""
    print(f"\n{C['b']}+ 新增接入目标（写进 netdev 连接簿）{C['x']}")
    print(f"{C['b']}协议：{C['x']}1) SSH   2) Telnet   3) 串口")
    k = {"1": "ssh", "2": "telnet", "3": "serial"}.get((ask("选 1/2/3（回车取消）：") or ""), "")
    if not k:
        return 0
    args = ["conn", "add", k]
    if k == "serial":
        ports = serial_ports()
        for i, p in enumerate(ports, 1):
            print(f"  {i}) {p}")
        d = ask(f"串口路径（选编号或直输）{'[默认 1]' if ports else ''}：")
        if d is None:
            return 0
        dev = ports[int(d) - 1] if d.isdigit() and 1 <= int(d) <= len(ports) else (d or (ports[0] if ports else ""))
        if not dev:
            print(f"{C['y']}没给串口路径，取消{C['x']}")
            return 0
        baud = ask("波特率（回车 = 9600）：")
        if baud is None:
            return 0
        baud = baud or "9600"
        args += ["--device", dev, "--baud", baud]
        default_name = "console-" + dev.rsplit("-", 1)[-1].lower()
    else:
        host = ask("主机 / IP：")
        if host is None:
            return 0
        host = host.strip()
        if not host:
            print(f"{C['y']}没给主机，取消{C['x']}")
            return 0
        port = ask(f"端口（回车 = {22 if k == 'ssh' else 23}）：")
        if port is None:
            return 0
        port = port.strip()
        user = ask(f"用户名（{k}，可空）：")
        if user is None:
            return 0
        user = user.strip()
        args += [host]
        if port:
            args += ["--port", port]
        if user:
            args += ["-u", user]
        default_name = f"{k}-{host.replace('.', '-')}"
    nm = ask(f"名称（回车 = {default_name}）：")
    if nm is None:
        return 0
    name = nm.strip() or default_name
    nt = ask("备注（可空）：")
    if nt is None:
        return 0
    note = nt.strip()
    args += ["--name", name]
    if note:
        args += ["--note", note]
    print(f"\n{C['b']}▷ netdev {' '.join(args)}{C['x']}")
    r = subprocess.run([NETDEV, *args], text=True)
    if r.returncode == 0:
        print(f"\n{C['d']}当前连接簿：{C['x']}")
        subprocess.run([NETDEV, "conn", "list"], text=True)
        print(f"{C['g']}下次点对应的接入命令就能看到它{C['x']}")
    return r.returncode


def main_connect(kind, flags=frozenset()):
    """接入某一类（ssh / telnet / serial）：列现成目标 → 选或直接输地址 → netdev shell → attach。"""
    global BAUD
    BAUD = "9600"
    print(f"\n{C['b']}→ {KINDS[kind]} 接入{C['x']}")

    if kind == "serial":
        ports = serial_ports()
        live = live_windows()
        if not ports:
            print(f"{C['y']}  没发现串口（/dev/cu.usbserial*）——检查 USB-Console 线是否插好{C['x']}")
            print(f"  {C['d']}也可手动输入路径：/dev/cu.usbserial-XXXX{C['x']}")
        for i, p in enumerate(ports, 1):
            win = f"serial-{p.replace('/', '-').replace('.', '-')}"
            mark = f"  {C['g']}[已连接]{C['x']}" if win in live else ""
            # 清单里的串口设备也标一下
            named = next((d["name"] for d in load_devices()
                          if d.get("protocol") == "serial" and d.get("port") == p), "")
            print(f"  {i}) {p}" + (f"  {C['d']}（清单里叫 {named}）{C['x']}" if named else "") + mark)
        for d in load_devices():
            if d.get("protocol") == "serial" and (d.get("port") or "") not in ports:
                print(f"  {C['d']}· 清单设备 {d['name']} → {d.get('port')}{C['x']}")
        if "--list" in flags:
            return 0
        raw = ask(f"\n选编号 / 输串口路径（回车取消）[{C['d']}默认 1{C['x']}]：")
        if raw is None or (raw == "" and not ports):
            return 0
        choice = ports[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(ports) else (raw or (ports[0] if ports else ""))
        if not choice:
            return 0
        b = ask("波特率（回车 = 自动探测）：") or ""
        BAUD = b or "auto"
        args, human = build_cmd(choice, kind)
    else:
        items = candidates(kind)
        if items:
            print(f"{C['d']}现成目标（☆ = 正式设备）：{C['x']}")
            for i, it in enumerate(items, 1):
                star = "☆ " if it["official"] else "  "
                mark = f"  {C['g']}[已连接]{C['x']}" if it["live"] else ""
                print(f"  {i}) {star}{it['name']}  {C['d']}{it['addr']}{C['x']}{mark}")
        else:
            print(f"{C['d']}（连接簿里还没有 {KINDS[kind]} 目标；可直接输地址，或用 netdev conn add 添加）{C['x']}")
        if "--list" in flags:
            return 0
        tip = "选编号，或直接输 主机[:端口] / 用户@主机[:端口]（回车取消）"
        raw = ask(f"\n{tip}：")
        if raw is None or not raw:
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            args, human = build_cmd(items[int(raw) - 1], kind)
        else:
            args, human = build_cmd({"raw": raw}, kind)

    cmd = [NETDEV, *args]
    print(f"\n{C['b']}▷{' ' + ' '.join(args)}{C['x']}   {C['d']}（{human}）{C['x']}")
    if "--dry-run" in flags:
        print(f"{C['d']}--dry-run：只显示命令，不执行{C['x']}")
        return 0

    before = set(live_windows())
    # 告诉 netdev “别自己 attach”，由本脚本用 netdev attach 接（每个终端一个独立会话）
    env = dict(os.environ, NETDEV_NO_ATTACH="1")
    r = subprocess.run(cmd, text=True, env=env)
    if r.returncode != 0:
        print(f"{C['r']}接入失败（退出码 {r.returncode}）{C['x']}")
        return r.returncode
    win = _recent_window(before)
    if not win:
        print(f"{C['y']}没找到刚才那个设备窗口 —— 可能连不上设备（看上面提示）{C['x']}")
        return 1
    if not TMUX:
        return 0
    if not sys.stdin.isatty():
        print(f"{C['d']}（非交互环境：接入用 {NETDEV} attach {win}）{C['x']}")
        return 0
    print(f"\n{C['g']}✔ 进入同屏会话 {win}{C['x']}  {C['d']}这是本终端自己的会话（设备窗格仍在 netops，AI 可同时读）{C['x']}\n")
    # 交给 netdev 处理“本终端自己的会话”一事（链接窗口 + attach）：多终端互不抢屏
    os.execv(NETDEV, [NETDEV, "attach", win])


# ══════════════════════════════════════════════════════════════════════════
#  管理接入目标：列出 / 新增 / 删除 / 接入（菜单循环）
#    一切真实能力都在 netdev CLI（连接簿 conn / 同屏会话 shell），这里只是外壳。
# ══════════════════════════════════════════════════════════════════════════
def _conn_rows():
    """连接簿条目 + 在线状态（一次拿全，列清单/删除/菜单头部都用它）。"""
    rows = []
    for it in _netdev_json("conn", "list", "--json"):
        rows.append({
            "key": it.get("id") or it.get("name") or "",
            "name": it.get("name") or it.get("id") or "?",
            "proto": it.get("protocol") or "?",
            "addr": it.get("address") or it.get("uri") or "",
            "online": bool(it.get("connected")),
            "window": it.get("window") or "",
            "note": it.get("note") or "",
        })
    return rows


def _device_rows():
    """设备清单（devices.toml）—— 也是“已接入目标”的一部分（正式设备）。"""
    live = {p.get("window") for p in _netdev_json("screen-ls", "--json")}
    out = []
    for d in load_devices():
        addr = d.get("address") or (d.get("host") or d.get("port") or "")
        proto = d.get("protocol") or "?"
        out.append({"name": d.get("name") or "?", "proto": proto, "addr": str(addr),
                    "online": (d.get("name") in live)})
    return out


def _sessions():
    return [p.get("window") for p in _netdev_json("screen-ls", "--json") if p.get("window")]


def _print_overview():
    """一行概览（菜单头部 / 删前列清单都用）。"""
    rows, devs, sess = _conn_rows(), _device_rows(), _sessions()
    print(f"  {C['d']}连接簿 {len(rows)} 条（在线 {sum(r['online'] for r in rows)}）"
          f"｜设备清单 {len(devs)} 台（在线 {sum(d['online'] for d in devs)}）"
          f"｜同屏会话 {len(sess)} 个{C['x']}")
    if sess:
        print(f"  {C['d']}在线会话：{', '.join('netops:' + s for s in sess)}{C['x']}")
    return rows


def main_list():
    """列出已接入目标：连接簿（你加过的） + 设备清单 + 当前同屏会话。"""
    rows = _print_overview()
    print(f"\n{C['b']}① 连接簿（你添加的接入目标）{C['x']} {C['d']}{ROOT / 'connections.json'}{C['x']}")
    if not rows:
        print(f"  {C['d']}（空 —— 用「管理接入目标 → 新增」加一个）{C['x']}")
    else:
        for i, r in enumerate(rows, 1):
            mark = f"{C['g']}○ 已连接{C['x']}" if r["online"] else f"{C['d']}—{C['x']}"
            print(f"  {i:>2}) {r['name']:<24} {r['proto']:<7} {r['addr']:<34} {mark}"
                  + (f"  {C['d']}{r['note']}{C['x']}" if r["note"] else ""))
    devs = _device_rows()
    print(f"\n{C['b']}② 设备清单（devices.toml，☆ = 正式设备）{C['x']}")
    for d in devs:
        mark = f"{C['g']}○ 已连接{C['x']}" if d["online"] else f"{C['d']}—{C['x']}"
        print(f"     ☆ {d['name']:<24} {d['proto']:<7} {d['addr']:<34} {mark}")
    print(f"\n{C['b']}③ 当前同屏会话{C['x']}")
    sess = _sessions()
    print(f"     {', '.join('netops:' + s for s in sess) if sess else C['d'] + '（无）' + C['x']}")
    print(f"  {C['d']}接入：本菜单选 1/2/3；删除连接簿条目：选 6{C['x']}")
    return 0


def main_rm(argv=None, flags=frozenset()):
    """删除已接入目标（连接簿条目）。支持：rm <名称/id> --yes 非交互；否则交互式确认。

    只删“连接簿条目”；正在同屏的会话不受影响（要断开会话另用 Ctrl+B D 或 tmux kill-window）。
    删除前 netdev 会先备份 connections.json（lib/conn_store.save 自动做）。
    """
    argv = list(argv or [])
    rows = _conn_rows()
    print(f"\n{C['b']}≡ 删除已接入目标{C['x']}")
    if not rows:
        print(f"  {C['d']}连接簿是空的，没有可删的{C['x']}")
        return 0
    for i, r in enumerate(rows, 1):
        mark = f"{C['g']}○ 已连接{C['x']}" if r["online"] else f"{C['d']}—{C['x']}"
        print(f"  {i:>2}) {r['name']:<24} {r['proto']:<7} {r['addr']:<34} {mark}")

    keys = []
    if argv:
        keys = [a for a in argv if a]
    else:
        raw = ask("\n删哪几条？（编号，多选用逗号如 2,3；或直输名称/id；回车取消）：")
        if raw is None or not raw.strip():
            print(f"{C['d']}已取消，什么都没删。{C['x']}")
            return 0
        for tok in raw.replace("，", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.isdigit() and 1 <= int(tok) <= len(rows):
                keys.append(rows[int(tok) - 1]["key"])
            else:
                keys.append(tok)
    hit = [r for r in rows if r["key"] in keys or r["name"] in keys]
    miss = [k for k in keys if k not in {r["key"] for r in hit} and k not in {r["name"] for r in hit}]
    if not hit:
        print(f"{C['y']}认不出要删的：{', '.join(miss) or '（空）'}{C['x']}")
        return 1
    print(f"\n{C['b']}将删除以下 {len(hit)} 条连接簿条目（同屏会话不断开）：{C['x']}")
    for r in hit:
        print(f"  · {r['name']}  {r['proto']}  {r['addr']}" + (f"  {C['d']}{r['note']}{C['x']}" if r["note"] else ""))
    if miss:
        print(f"  {C['y']}认不出的已跳过：{', '.join(miss)}{C['x']}")
    if "--yes" not in flags:
        c = ask(f"\n确认删除？输 {C['b']}yes{C['x']} 才会删（其它任意输入 = 取消）：")
        if c is None or c.strip().lower() not in ("yes", "y", "是", "确认"):
            print(f"{C['d']}已取消，什么都没删。{C['x']}")
            return 0
    ok, bad = 0, []
    for r in hit:
        res = sh([NETDEV, "conn", "rm", r["key"]])
        if res.returncode == 0:
            ok += 1
            print(f"  {C['g']}✔ 已删除{C['x']} {r['name']}（{r['proto']} {r['addr']}）")
        else:
            bad.append(r["name"])
            print(f"  {C['r']}✘ 删除失败{C['x']} {r['name']}：{(res.stderr or res.stdout).strip()[:120]}")
    if ok:
        print(f"\n{C['g']}✔ 共删除 {ok} 条{C['x']}{C['d']}（netdev 已把原连接簿备份到 ~/netops/backups/connections.json.bak-*）{C['x']}")
        print(f"{C['d']}剩下的：{C['x']}")
        main_list()
    return 0 if not bad else 1


def main_manage():
    """管理接入目标：接入三种连接 / 列出 / 新增 / 删除（菜单循环，直到退出）。"""
    _flush_stdin()
    while True:
        print(f"\n{C['b']}≡ 管理接入目标{C['x']}   {C['d']}{ROOT / 'connections.json'}{C['x']}")
        _print_overview()
        print(f"\n  {C['b']}1{C['x']}) → 接入 SSH"
              f"\n  {C['b']}2{C['x']}) → 接入 Telnet"
              f"\n  {C['b']}3{C['x']}) → 接入 串口（Console）"
              f"\n  {C['b']}4{C['x']}) ☰ 列出已接入目标"
              f"\n  {C['b']}5{C['x']}) + 新增接入目标（含端口）"
              f"\n  {C['b']}6{C['x']}) ×  删除已接入目标（连接簿条目）"
              f"\n  {C['b']}0{C['x']}) 退出")
        c = ask("\n选编号（回车 = 4 列出；0/q = 退出）：", default="4")
        if c is None:
            return 1
        c = (c or "4").strip().lower()
        if c in ("0", "q", "quit", "exit"):
            print(f"{C['d']}已退出，什么都没改。{C['x']}")
            return 0
        if c == "1":
            main_connect("ssh")
        elif c == "2":
            main_connect("telnet")
        elif c == "3":
            main_connect("serial")
        elif c == "4":
            main_list()
        elif c == "5":
            main_add()
        elif c == "6":
            main_rm()
        else:
            print(f"{C['y']}认不出「{c}」—— 请输入 0/1/2/3/4/5/6{C['x']}")


def _recent_window(before):
    """netdev shell 之后找“刚才那个设备窗口”：优先新出现的；否则看 last_targets.json 里最近且仍在线的。

    （窗口已存在时不会新增窗口，以前会误报“没有新建同屏会话”）
    """
    after = set(live_windows())
    new = [w for w in after if w not in before]
    if new:
        return new[0]
    try:
        items = json.loads((ROOT / "state/last_targets.json").read_text(encoding="utf-8"))
        items = sorted(items, key=lambda x: x.get("at", ""), reverse=True)
        for it in items:
            if it.get("window") in after:
                return it["window"]
    except Exception:
        pass
    return None


def main_attach(argv=None):
    """把**指定设备**接到“当前这个终端”自己的会话（左栏“↗ 接 xx”按钮用的就是它）。

    先确保设备窗格存在（netdev shell，不 attach），再交给 netdev attach 接本终端自己的会话，
    于是宿主那边的“按标题复用终端”正好变成“一个按钮 = 一个专属终端 = 一台设备”。
    """
    argv = list(argv or [])
    if not argv:
        print("用法: quick_conn.py attach <设备名>")
        return 1
    dev = argv[0]
    r = subprocess.run([NETDEV, "shell", dev], text=True,
                       env=dict(os.environ, NETDEV_NO_ATTACH="1"))
    if r.returncode != 0:
        print(f"{C['r']}接入失败（退出码 {r.returncode}）{C['x']}")
        return r.returncode
    if not sys.stdin.isatty() or not TMUX:
        print(f"{C['d']}（非交互环境：接入用 {NETDEV} attach {dev}）{C['x']}")
        return 0
    os.execv(NETDEV, [NETDEV, "attach", dev])


def main_quick():
    """→ 快速接入：三种通道合一（选 1/2/3 → 直接进接入流程）。"""
    print(f"\n{C['b']}→ 快速接入{C['x']}  {C['d']}SSH / Telnet / 串口（Console）{C['x']}")
    _print_overview()
    print(f"\n  {C['b']}1{C['x']}) SSH\n  {C['b']}2{C['x']}) Telnet\n  {C['b']}3{C['x']}) 串口（Console）\n  {C['b']}0{C['x']}) 退出")
    c = ask("\n选 1/2/3（回车取消）：")
    if c is None:
        return 1
    c = (c or "").strip().lower()
    if c in ("1", "ssh"):
        return main_connect("ssh")
    if c in ("2", "telnet"):
        return main_connect("telnet")
    if c in ("3", "serial"):
        return main_connect("serial")
    print(f"{C['d']}已取消，什么都没做。{C['x']}")
    return 0


def main():
    argv = [a for a in sys.argv[1:]]
    flags = {a for a in argv if a.startswith("--")}
    argv = [a for a in argv if not a.startswith("--")]
    act = (argv[0] if argv else "").strip()
    if act in ("", "quick", "connect"):    # 不带参数 = 快速接入
        return main_quick()
    if act in ("manage", "menu"):
        return main_manage()
    if act in ("list", "ls", "show"):
        return main_list()
    if act in ("rm", "del", "delete"):
        return main_rm(argv[1:], flags)
    if act in ("attach", "a"):
        return main_attach(argv[1:])
    if act == "add":
        return main_add()
    if act in KINDS:
        return main_connect(act, flags)
    print("用法: quick_conn.py [quick|manage|list|add|rm|attach] | ssh|telnet|serial [--list] [--dry-run]")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
