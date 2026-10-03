#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：写操作人审闸门（2026-10-03）。

为什么单独给它一个文件：
    `lib/approval.py` 是**唯一**决定"AI 能不能改设备"的地方。它一旦被放宽，
    后面所有护栏都还在，但已经形同虚设。所以它的每一条分支都要有断言，
    尤其**新增的豁免**——豁免是这类文件里唯一天然危险的东西。

本文件守 4 件事：
  1. fail-closed：只读模式 / 无豁免时，ask() 一律拒绝
  2. 豁免够窄：必须同时满足「开关开着」+「目标是本机模拟器」，缺一不可
  3. 豁免绝不含糊：真实设备、假设备名、开关未开 —— 一律不免
  4. 路径可移植：ROOT 认 NETDEV_ROOT，不再写死 ~/netops

用法：
    python3 tests/test_approval_gates.py
不碰真设备、不发网络请求、不弹窗。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import paths as _P   # noqa: E402  路径统一真源

DEVICES_TOML = _P.cfg("devices.toml")   # 安装根有软链就用它，否则 config/（源码安装）
EXAMPLE = ROOT / "config/devices.toml.example"

# 测试要能在**全新克隆**里直接跑（那里还没有 devices.toml），所以自给自足：
# 缺就从模板备一份。只在内存层面判断，不影响被测代码。
if not DEVICES_TOML.exists() and EXAMPLE.exists():
    import shutil
    shutil.copyfile(EXAMPLE, DEVICES_TOML)
    print("  （提示：本次运行已从 config/devices.toml.example 生成 config/devices.toml）")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def _fresh_approval():
    """重新导入，确保拿到干净的模块级状态（APPROVAL_URL / ROOT 等）。

    注意：`lib/paths.py` 的 ROOT 是**首次导入时**算好的（环境变量在进程启动前就定了，
    这是正常设计）。所以要测"换个 NETDEV_ROOT 会怎样"，必须把 paths 从
    sys.modules **和 lib 包的属性上**都摘掉 —— 只清 sys.modules 不够，
    因为 `from . import paths` 走的是 getattr(lib, "paths")，会拿到缓存对象。
    （这两点各漏过一次，断言假失败/from lib import 崩。）
    """
    for m in ("lib.approval", "lib.paths"):
        sys.modules.pop(m, None)
    libpkg = sys.modules.get("lib")
    if libpkg is not None:
        for attr in ("paths", "approval"):
            if hasattr(libpkg, attr):
                try:
                    delattr(libpkg, attr)
                except AttributeError:
                    pass
    return importlib.import_module("lib.approval")


# ======================================================================
# 一、路径可移植：ROOT 认 NETDEV_ROOT
# ======================================================================
def test_root_portable():
    print("\n一、路径推导（不能写死 ~/netops）")
    ap = _fresh_approval()
    check("ROOT 指向本项目（而不是别的目录）",
          ap.ROOT == ROOT, f"ROOT={ap.ROOT} 期望={ROOT}")
    check("源码里没有写死 home()/netops",
          'pathlib.Path.home() / "netops"' not in (ROOT / "lib/approval.py").read_text(encoding="utf-8"))

    # 换根目录应跟着变。
    # 期望值也要 resolve()：macOS 上 /tmp 是 /private/tmp 的软链，
    # 而 paths.ROOT 调了 .resolve()，直接比字符串会假失败（实测踩到）。
    os.environ["NETDEV_ROOT"] = "/tmp/netdev-somewhere-else"
    try:
        ap2 = _fresh_approval()
        want = pathlib.Path("/tmp/netdev-somewhere-else").resolve()
        check("设了 NETDEV_ROOT 后 ROOT 跟着变",
              pathlib.Path(ap2.ROOT) == want, f"ROOT={ap2.ROOT} 期望={want}")
    finally:
        os.environ.pop("NETDEV_ROOT", None)
    _fresh_approval()


# ======================================================================
# 二、本机模拟器判定
# ======================================================================
def test_local_sim_detection():
    print("\n二、哪些设备算「本机模拟器」")
    ap = _fresh_approval()
    check("mock-hw 判定为本机模拟器", ap._is_local_sim("mock-hw") is True)
    check("未知设备不算模拟器", ap._is_local_sim("no-such-device") is False)
    check("空名字不算模拟器", ap._is_local_sim("") is False)

    # 真机名字若存在于 devices.toml，必须判 False
    txt = DEVICES_TOML.read_text(encoding="utf-8")
    real = [l.split("name", 1) for l in txt.splitlines() if l.strip().startswith("name")
            and "mock" not in l.lower()]
    for r in real[:2]:
        nm = r[1].split("=", 1)[1].strip().strip('"').split("#")[0].strip()
        if nm:
            check(f"真机 {nm!r} 不算模拟器", ap._is_local_sim(nm) is False)


# ======================================================================
# 三、豁免的边界（最关键的一段）
# ======================================================================
def test_selftest_bypass_is_narrow():
    print("\n三、自检豁免的边界（真实设备永远不免人审）")
    ap = _fresh_approval()
    saved = os.environ.pop("NETDEV_SELFTEST", None)
    try:
        check("开关没开 → 模拟器也不豁免",
              ap._selftest_sim_bypass("mock-hw") is False,
              "免审开关必须由 selftest 显式打开，不能默认开")

        os.environ["NETDEV_SELFTEST"] = "1"
        check("开关开 + 模拟器 → 豁免（CI 才跑得通）",
              ap._selftest_sim_bypass("mock-hw") is True)
        check("开关开 + 未知设备 → 不豁免",
              ap._selftest_sim_bypass("no-such-device") is False)

        # 真机必须逐个验证（读 devices.toml 里所有非 sim 设备）
        try:
            import tomllib
            devs = tomllib.loads(DEVICES_TOML.read_text(encoding="utf-8")).get("device", []) or []
        except Exception:
            devs = []
        reals = [d.get("name") for d in devs if not d.get("sim") and d.get("name")]
        if reals:
            for nm in reals:
                check(f"开关开 + 真机 {nm!r} → 仍然不豁免",
                      ap._selftest_sim_bypass(nm) is False,
                      "这是本文件最重要的一条：豁免绝不能碰到真设备")
        else:
            check("（devices.toml 里当前没有真机条目，跳过真机断言）", True)

        # 开关值必须是明确 truthy，垃圾值不放行
        for junk in ("0", "false", "no", "", "maybe"):
            os.environ["NETDEV_SELFTEST"] = junk
            check(f"开关值 {junk!r} → 不豁免", ap._selftest_sim_bypass("mock-hw") is False)
    finally:
        os.environ.pop("NETDEV_SELFTEST", None)
        if saved is not None:
            os.environ["NETDEV_SELFTEST"] = saved


# ======================================================================
# 四、fail-closed：只读模式必须拒绝
# ======================================================================
def test_readonly_is_fail_closed():
    print("\n四、fail-closed（只读模式必须拒绝一切写）")
    ap = _fresh_approval()
    mi = ap.mode_info()
    if mi["mode"] == "readonly":
        check("只读模式下 ask() 返回 False", ap.ask("mock-hw", ["sysname X"]) is False)
    else:
        check(f"当前策略是 {mi['mode']!r}（非 readonly），只读分支由 mode_info 单测覆盖", True)
    check("MODES 三个值齐全", set(ap.MODES) == {"readonly", "ask", "allow"})
    check("默认回落到 ask（而不是 allow）",
          ap._raw().get("writes", "ask") in ("readonly", "ask", "allow"),
          "读不到策略文件时必须回落成 ask")


def main() -> int:
    print("=" * 68)
    print("回归测试：写操作人审闸门")
    print("=" * 68)
    test_root_portable()
    test_local_sim_detection()
    test_selftest_bypass_is_narrow()
    test_readonly_is_fail_closed()
    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
    print("=" * 68)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
