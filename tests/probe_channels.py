#!/usr/bin/env python3
"""三种接入方式 · 真实场景健壮性复核

覆盖：接入 → 开终端 → 监控 → 命令 → 快照 → 占用冲突 → AI/手动切换 → 重复与删除重接。
串口：SERIAL_DEV（见下方常量；默认用仓库自带的本机模拟器，
        跑真机请 export NETDEV_PROBE_SERIAL=<你的串口设备名>）
模拟：mock-hw（SSH 127.0.0.1:20022）
      telnet-lab（Telnet 127.0.0.1:2323）

用法: python3 tests/probe_channels.py [--json]
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

# ── 路径全部由脚本自身位置推导（2026-10-03 修）────────────────────────
# 原来这里硬编码了本机的 tmux 与 netdev 绝对路径：
# 换一台机器 / 换用户名，这个探针直接全灭，而且报错是「文件不存在」，
# 看不出是路径写死。改成 ROOT + shutil.which 后任意安装位置都能跑。
ROOT = pathlib.Path(__file__).resolve().parent.parent
# 串口目标设备名。**不要写死成某台真机的名字** —— 既泄露身份，别人也跑不了。
# 默认用仓库自带的本机模拟器；跑自己的真机：export NETDEV_PROBE_SERIAL=<设备名>
SERIAL_DEV = os.environ.get("NETDEV_PROBE_SERIAL", "mock-hw")

NETDEV = str(ROOT / "netdev")
TMUX = shutil.which("tmux") or "tmux"
# netdev 入口脚本自己会补 PATH，这里给一个干净的最小环境
_MIN_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

B = "http://127.0.0.1:8898"
RESULTS: list[tuple[str, str, str, str]] = []      # (通道, 场景, 结果, 备注)


def _req(path: str, body: dict | None = None, timeout: int = 180):
    try:
        if body is None:
            r = urllib.request.urlopen(B + path, timeout=timeout)
        else:
            rq = urllib.request.Request(B + path, data=json.dumps(body).encode(),
                                        headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(rq, timeout=timeout)
        return json.load(r)
    except Exception as e:
        return {"__err__": f"{type(e).__name__}: {e}"}


def rec(chan, scene, ok, note=""):
    RESULTS.append((chan, scene, "PASS" if ok else "FAIL", note))
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} [{chan}] {scene}" + (f"  — {note}" if note else ""))


def run_devices():
    d = _req("/api/devices")
    return {x["name"]: x for x in (d.get("devices") or [])}




def _tmux(*a):
    return subprocess.run([TMUX, *a], capture_output=True, text=True)


def wait_prompt(win: str, timeout: float = 25.0) -> str:
    """等设备回到提示符（不靠固定 sleep —— 固定 sleep 是上一版误判的根源）。"""
    t0 = time.time()
    box = ""
    while time.time() - t0 < timeout:
        box = _tmux("capture-pane", "-p", "-J", "-t", f"netops:{win}").stdout
        tail = [l for l in box.splitlines() if l.strip()][-1:] or [""]
        if any(k in tail[0] for k in ("<Huawei>", "[Huawei]", "#", ">", "$")):
            return box
        time.sleep(0.6)
    return box



def read_screen(dev: str, lines: int = 40) -> str:
    """用 netdev 官方通道读屏（比 tmux capture-pane 可靠：不受可见区限制）。"""
    r = subprocess.run([NETDEV, "screen-read", dev, "--lines", str(lines)],
                       capture_output=True, text=True, timeout=60, env=_MIN_ENV)
    return r.stdout + r.stderr

def send_via_netdev(dev: str, cmd: str) -> str:
    """用 netdev 官方通道发命令（等价于 AI 的 screen_send），比 tmux send-keys 可靠。"""
    r = subprocess.run([NETDEV, "screen-send", dev, cmd, "--yes"],
                       capture_output=True, text=True, timeout=60, env=_MIN_ENV)
    return r.stdout + r.stderr


# ───────────────────────────── 每通道的通用场景 ─────────────────────────────
def probe(chan: str, dev: str, kind: str):
    print(f"\n\033[36m━━━ {chan} · {dev} ━━━\033[0m")
    # 先清掉该设备的旧窗格（旧桥可能处于卡死状态，实测踩到过）
    _tmux("kill-window", "-t", f"netops:{dev}")
    time.sleep(2)

    # T1 接入 / 开终端（人机同屏窗格）
    t = _req("/api/term/open", {"device": dev, "rows": 40, "cols": 140}, 180)
    ok = bool(t.get("sid"))
    rec(chan, "T1 开终端（建同屏窗格）", ok, t.get("window") or str(t.get("__err__") or t.get("error") or "")[:70])
    if not ok:
        return
    sid, win = t["sid"], t.get("window")
    cap = wait_prompt(win, 30)

    import subprocess
    tm = TMUX
    rec(chan, "T2 会话建立（有横幅/提示符）", ("串口已连接" in cap) or ("<Huawei>" in cap) or ("[" in cap),
        (cap.strip().splitlines() or ["(空)"])[-1][:60])

    # T3 监控采集（走同屏，不直连）
    m = _req(f"/api/monitor?device={dev}", None, 200)
    if m.get("__err__"):
        rec(chan, "T3 监控采集", False, m["__err__"][:70])
    else:
        mm = m.get("metrics") or {}
        rec(chan, "T3 监控采集", mm.get("cpu_10s") is not None or mm.get("mem_pct") is not None,
            f"cpu={mm.get('cpu_10s')}% mem={mm.get('mem_pct')}% via={m.get('via')}")

    # T4 只读命令（netdev 官方通道 screen-send —— 与 AI 用的同一条路）
    send_via_netdev(dev, "display clock")
    time.sleep(4)
    cap2 = wait_prompt(win, 25) + read_screen(dev, 60)
    rec(chan, "T4 只读命令回显", ("Time Zone" in cap2) or ("Time" in cap2 and "display clock" in cap2), "display clock")

    # T5 快照（抓配置）
    s = _req("/api/snap/save", {"device": dev, "tag": f"健壮性复核-{chan}"}, 290)
    rec(chan, "T5 快照抓配置", bool(s.get("ok")),
        f"{s.get('lines') or s.get('msg') or str(s.get('error') or s.get('__err__') or '')[:50]}")

    # T6 关终端（会话释放）
    c = _req("/api/term/close", {"sid": sid})
    rec(chan, "T6 关闭终端会话", bool(c.get("ok", True)), "")


# ─────────────────────────── 串口专有：占用与冲突 ───────────────────────────
def probe_serial_conflict():
    print("\n\033[36m━━━ 串口专有 · 占用与冲突 ━━━\033[0m")
    import subprocess
    tm = TMUX

    # 起桥
    t = _req("/api/term/open", {"device": SERIAL_DEV, "rows": 40, "cols": 140}, 180)
    if not t.get("sid"):
        rec("串口", "S1 重建桥", False, str(t.get("error") or t.get("__err__"))[:70]); return
    rec("串口", "S1 重建桥", True, t.get("window"))
    time.sleep(7)

    # 锁是否被记录（带业主）
    # 2026-10-03 修：原来按本机某块 USB 适配器的**真实序列号**过滤 ——
    # 换一块线、换一个用户名，这里就永远匹配不到，断言会**假失败**，
    # 而且把硬件标识符带进了公开仓。改成按"串口锁"这个语义过滤。
    lk = _req("/api/portlocks")
    locks = lk.get("locks") or []
    mine = [l for l in locks if "cu." in str(l.get("port", "")) or "usbserial" in str(l.get("port", ""))]
    rec("串口", "S2 锁记录业主", bool(mine),
        f"{mine[0]['holder']}｜{mine[0]['purpose']}" if mine else "无锁")

    # 直连必须被拦（这是本次事故的根因路径）
    sys.path.insert(0, str(ROOT))
    try:
        from lib import engine
        dev = engine.get_device(SERIAL_DEV)
        try:
            s = engine.connect(dev, password=None); s.close()
            rec("串口", "S3 直连被独占拦下", False, "竟然成功了（护栏失效）")
        except Exception as e:
            msg = str(e)
            rec("串口", "S3 直连被独占拦下", ("已被占用" in msg or "同屏会话" in msg),
                msg.splitlines()[0][:70])
    except Exception as e:
        rec("串口", "S3 直连被独占拦下", False, f"导入失败 {e}")

    # MCP 的串口工具也必须被拦
    try:
        import netdev_mcp as M
        try:
            r = M.t_serial({"device": SERIAL_DEV, "command": "display clock"})
            rec("串口", "S4 MCP t_serial 被拦", not r.get("ok"),
                ("已拦：" + str(r.get("error", ""))[:50]) if not r.get("ok") else "竟然执行了")
        except Exception as e:
            rec("串口", "S4 MCP t_serial 被拦", "占用" in str(e), str(e).splitlines()[0][:70])
    except Exception as e:
        rec("串口", "S4 MCP t_serial 被拦", False, f"导入失败 {e}")

    # 桥是否还活着（拦截后不该受影响）
    e2 = subprocess.run([tm, "list-panes", "-t", f"netops:{SERIAL_DEV}", "-F", "#{pane_dead}"],
                        capture_output=True, text=True).stdout.strip()
    rec("串口", "S5 拦截后桥安然无恙", e2 == "0", f"pane_dead={e2 or '?'}")

    # 串口写操作必须走 apply（run 通道拒绝写）
    r = _req("/api/netdev/run", {"device": SERIAL_DEV, "command": "vlan 3999"})
    rec("串口", "S6 写操作被闸门拒绝", (not r.get("ok")) or ("拒" in str(r.get("error", ""))),
        str(r.get("error") or r.get("output") or "")[:60])


# ─────────────────────────── AI 与手动 双向切换 ───────────────────────────
def probe_handoff():
    print("\n\033[36m━━━ AI ↔ 手动 双向切换 ━━━\033[0m")
    import subprocess
    tm = TMUX

    # 确保有桥
    t = _req("/api/term/open", {"device": SERIAL_DEV, "rows": 40, "cols": 140}, 180)
    if not t.get("sid"):
        rec("切换", "H0 准备桥", False, str(t.get("error"))[:60]); return
    win = t.get("window")
    time.sleep(6)

    # H1 手动敲一条 → 应出现在屏上（AI 读得到同一块屏）
    marker = "display esn"
    subprocess.run([tm, "send-keys", "-t", f"netops:{win}", "-l", marker], capture_output=True)
    subprocess.run([tm, "send-keys", "-t", f"netops:{win}", "Enter"], capture_output=True)
    cap = wait_prompt(win, 25) + read_screen(SERIAL_DEV, 60)
    rec("切换", "H1 手动敲命令 → 屏可见", "ESN of device" in cap, marker)

    # H2 AI 读同一块屏（screen_read 等价物 = 面板读取）
    via_api = _req(f"/api/monitor?device={SERIAL_DEV}", None, 200)
    rec("切换", "H2 AI 侧能读同一块屏", not via_api.get("__err__"),
        f"via={via_api.get('via')}")

    # H3 AI 发命令（走 screen-send 通道）→ 手动能看见
    send_via_netdev(SERIAL_DEV, "display version")
    time.sleep(4)
    cap2 = wait_prompt(win, 30) + read_screen(SERIAL_DEV, 60)
    rec("切换", "H3 AI 发命令 → 手动可见", "VRP (R) software" in cap2, "display version")

    # H4 双方并发不炸：AI 读的同时手动写
    import threading
    errs = []
    def reader():
        for _ in range(3):
            try: _req(f"/api/monitor?device={SERIAL_DEV}", None, 120)
            except Exception as e: errs.append(str(e))
            time.sleep(0.6)
    th = threading.Thread(target=reader); th.start()
    subprocess.run([tm, "send-keys", "-t", f"netops:{win}", "-l", "display clock"], capture_output=True)
    subprocess.run([tm, "send-keys", "-t", f"netops:{win}", "Enter"], capture_output=True)
    th.join()
    wait_prompt(SERIAL_DEV, 20)
    e3 = subprocess.run([tm, "list-panes", "-t", f"netops:{SERIAL_DEV}", "-F", "#{pane_dead}"],
                        capture_output=True, text=True).stdout.strip()
    rec("切换", "H4 并发（AI 读 + 手动写）不炸", e3 == "0" and not errs, f"pane_dead={e3} errs={len(errs)}")


# ─────────────────────────── 重复与删除重接 ───────────────────────────
def probe_repeat():
    print("\n\033[36m━━━ 重复接入 / 删除重接 ━━━\033[0m")

    # R1 同一目标连开两次终端（应复用窗格而非堆叠）
    a = _req("/api/term/open", {"device": "mock-hw", "rows": 40, "cols": 140}, 180)
    b = _req("/api/term/open", {"device": "mock-hw", "rows": 40, "cols": 140}, 180)
    rec("重复", "R1 连开两次终端不堆窗格",
        bool(a.get("window")) and a.get("window") == b.get("window"),
        f"{a.get('window')} / {b.get('window')}")
    if a.get("sid"): _req("/api/term/close", {"sid": a["sid"]})
    if b.get("sid"): _req("/api/term/close", {"sid": b["sid"]})

    # R2 临时目标：接入 → 删 → 再接入
    nm = f"repeat-lab-{int(time.time()) % 10000}"
    add = _req("/api/conn/add", {"protocol": "telnet", "host": "127.0.0.1", "port": "2323",
                                "name": nm, "username": "admin"})
    rec("重复", "R2a 临时目标接入", bool(add.get("ok")), nm)
    lst = _req("/api/devices").get("devices") or []
    me = [x for x in lst if x.get("name") == nm]
    rec("重复", "R2b 列表可见", bool(me), f"kind={me[0].get('kind') if me else '?'}")
    if me:
        rm = _req("/api/conn/rm", {"key": me[0].get("id")})
        rec("重复", "R2c 删除（连带释放）", bool(rm.get("ok")),
            "窗格已清" if rm.get("pane_killed") else "无窗格")
        lst2 = _req("/api/devices").get("devices") or []
        rec("重复", "R2d 删除后确实消失", not [x for x in lst2 if x.get("name") == nm], "")

    # R3 正式设备：✕ 只释放不删除
    r = _req("/api/device/free", {"name": "mock-hw"})
    devices = _req("/api/devices").get("devices") or []
    still = [x for x in devices if x.get("name") == "mock-hw"]
    rec("重复", "R3 正式设备 ✕ 只释放不删", bool(r.get("ok")) and bool(still),
        f"free ok={r.get('ok')} 设备仍在={bool(still)}")


def main():
    print("\033[1m三种接入方式 · 真实场景健壮性复核\033[0m")
    devs = run_devices()
    print("目标:", ", ".join(f"{k}({v['kind']})" for k, v in devs.items()))

    for chan, dev in (("串口", SERIAL_DEV), ("SSH", "mock-hw"), ("Telnet", "telnet-lab")):
        if dev not in devs:
            print(f"\n  ! {chan} 目标 {dev} 不在列表，跳过")
            continue
        try:
            probe(chan, dev, devs[dev].get("kind"))
        except Exception as e:
            rec(chan, "意外异常", False, f"{type(e).__name__}: {e}")

    try: probe_serial_conflict()
    except Exception as e: rec("串口", "冲突场景异常", False, str(e)[:80])
    try: probe_handoff()
    except Exception as e: rec("切换", "切换场景异常", False, str(e)[:80])
    try: probe_repeat()
    except Exception as e: rec("重复", "重复场景异常", False, str(e)[:80])

    # 汇总
    passed = sum(1 for _, _, r, _ in RESULTS if r == "PASS")
    total = len(RESULTS)
    print(f"\n\033[1m汇总：{passed}/{total} 通过\033[0m")
    for chan, scene, res, note in RESULTS:
        if res == "FAIL":
            print(f"  \033[31m✗\033[0m [{chan}] {scene} — {note}")
    if "--json" in sys.argv:
        print(json.dumps([{"chan": c, "scene": s, "result": r, "note": n} for c, s, r, n in RESULTS],
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
