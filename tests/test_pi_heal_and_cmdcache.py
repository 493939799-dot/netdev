#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：pi 启动自愈 + 采集命令学习缓存（2026-10-01）。

背景（两处真 bug，都在真机上踩到/查出）：
  1. pi 的 proper-lockfile 锁是「空目录」`~/.pi/agent/*.json.lock`；
     崩溃时清理没跑到 → 锁目录永久残留 → pi 每次启动 EEXIST →
     读不到 settings.json → 静默回退默认 provider → 报 "No API key found"。
     netdev 侧的义务：**能探测出来 + 能自愈（只搬不删）**。
  2. `collect_metrics` 里 `cache_get` 的结果塞进一个 `retry` 字典后再没用过，
     `mon_cmds()` 也无视缓存 → 「探测命中 → cache_put」完全是单向的，
     每轮采集都要重撞一次墙。现在走 `plan_commands()` 让学到的命令直接生效。

用法：
    python3 tests/test_pi_heal_and_cmdcache.py
不依赖真机 / 不发网络请求（只碰临时目录与 lib/platforms.py）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


import platforms as P  # noqa: E402


# ======================================================================
# 一、采集命令学习缓存：必须「学一次，一直用」
# ======================================================================
def test_cmd_cache():
    print("\n[1] 采集命令学习缓存")
    tmp = tempfile.mkdtemp(prefix="netdev-cache-")
    os.environ["NETDEV_ROOT"] = tmp
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False

    base = P.plan_commands("ruijie_os", "sw1")
    check("plan_commands 给出平台默认命令", bool(base.get("cpu")), str(base))
    check("锐捷默认 cpu 命令来自档案", base.get("cpu") == "show cpu", repr(base.get("cpu")))

    # 模拟「探测命中并记住」——某台老锐捷只认 display cpu-usage
    P.cache_put("sw1", "cpu", "display cpu-usage")
    after = P.plan_commands("ruijie_os", "sw1")
    check("学到的命令在建计划阶段即生效（本次修复点）",
          after.get("cpu") == "display cpu-usage", repr(after.get("cpu")))

    other = P.plan_commands("ruijie_os", "sw2")
    check("学习结果按设备隔离，不污染别的设备",
          other.get("cpu") == "show cpu", repr(other.get("cpu")))

    f = pathlib.Path(tmp) / "config" / "cmd-cache.json"
    check("缓存已落盘", f.is_file(), str(f))
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False
    again = P.plan_commands("ruijie_os", "sw1")
    check("重启后（清内存重载）仍生效", again.get("cpu") == "display cpu-usage",
          repr(again.get("cpu")))

    f.write_text("{ this is not json", encoding="utf-8")
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False
    try:
        bad = P.plan_commands("huawei_vrp", "h1")
        check("缓存文件损坏时静默降级（不抛异常）",
              bad.get("cpu") == "display cpu-usage", repr(bad))
    except Exception as e:
        check("缓存文件损坏时静默降级（不抛异常）", False, f"{type(e).__name__}: {e}")

    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("NETDEV_ROOT", None)


# ======================================================================
# 二、pi 残留锁：判定要准（不误删活锁）、自愈要可还原
# ======================================================================
def test_pi_locks():
    print("\n[2] pi 残留锁判定与自愈")
    home = tempfile.mkdtemp(prefix="netdev-pihome-")
    agent = pathlib.Path(home) / ".pi" / "agent"
    agent.mkdir(parents=True)

    ui = ROOT / "ui"
    sys.path.insert(0, str(ui))
    try:
        import server as S            # noqa: E402
        check("ui/server.py 可导入（语法/依赖无误）", True)
    except Exception as e:
        check("ui/server.py 可导入（语法/依赖无误）", False, f"{type(e).__name__}: {e}")
        shutil.rmtree(home, ignore_errors=True)
        return

    real_pi_dir = S.PI_DIR
    S.PI_DIR = agent

    check("空目录里没有残留锁", S._pi_stale_locks() == [])

    fresh = agent / "settings.json.lock"
    fresh.mkdir()
    check("新鲜锁（<60s）不判定为残留", S._pi_stale_locks() == [])

    old = time.time() - 3600
    os.utime(fresh, (old, old))
    stale = [p.name for p in S._pi_stale_locks()]
    check("陈旧空锁目录判定为残留", stale == ["settings.json.lock"], str(stale))

    (agent / "auth.json.lock").mkdir()
    (agent / "auth.json.lock" / "pid").write_text("123", encoding="utf-8")
    os.utime(agent / "auth.json.lock", (old, old))
    stale = [p.name for p in S._pi_stale_locks()]
    check("非空目录不判定为锁（避免误搬用户数据）", stale == ["settings.json.lock"], str(stale))

    r = S.pi_heal_locks(dry=True)
    check("dry-run 只报告不动手", r["found"] == 1 and r["moved"] == [] and fresh.is_dir())

    r = S.pi_heal_locks()
    check("修复：移走残留锁", r["moved"] == ["settings.json.lock"], str(r))
    check("修复后原位置已空", not fresh.exists())
    q = pathlib.Path(r["quarantine"] or "")
    check("隔离区在 ~/.pi/agent 之外（不往 pi 扫描的目录里塞东西）",
          q.is_dir() and agent not in q.parents, str(q))
    check("隔离区里锁原样保留（只搬不删）", (q / "settings.json.lock").is_dir())
    check("非空的 auth.json.lock 仍在原地", (agent / "auth.json.lock" / "pid").is_file())

    # 收尾：本次测试在真实家目录建的隔离区要清掉（测试自身不留垃圾）
    if q.is_dir() and q.parent.name == ".quarantine-pi-locks":
        shutil.rmtree(q.parent, ignore_errors=True)
    S.PI_DIR = real_pi_dir
    shutil.rmtree(home, ignore_errors=True)


# ======================================================================
# 三、平台自动识别：五家 banner
# ======================================================================
def test_detect():
    print("\n[3] 平台自动识别（display version 回显）")
    cases = [
        ("Huawei Versatile Routing Platform Software\nVRP (R) software, Version 5.170", "huawei_vrp"),
        ("H3C Comware Software, Version 7.1.070", "h3c_comware"),
        ("Ruijie Networks RGOS Version 11.0", "ruijie_os"),
        ("Cisco IOS Software, C2960 Software", "cisco_ios"),
        ("Maipu MyPower OS Version 2.0", "maipu_s"),
        ("\x00\x01 garbage not a real banner", None),
    ]
    for text, want in cases:
        got = P.detect_platform(text)
        check(f"识别 {want or '不认识'} <= {text.splitlines()[0][:38]!r}", got == want, f"得到 {got!r}")


def main():
    print("=" * 66)
    print("netdev 回归测试：pi 启动自愈 + 采集命令学习缓存")
    print("=" * 66)
    test_cmd_cache()
    test_pi_locks()
    test_detect()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  NG ", f)
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
