#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：run 的逐条结果（partial）+ mock 的接口/VLAN 明细 + 提示词（2026-10-05）。

守三件事：
  A. netdev run 的「一条命令失败 = 整批失败」语义被修正 —— CLI `--json` 逐条回结果，
     netdev_mcp.t_run 解析成 ok / partial / n_ok，界面据此不再把「部分成功」打红叉。
  B. mock 补齐接口明细 / display port vlan / display vlan <id> 精确查询 ——
     AI 查明细时不再撞 Unrecognized。
  D. 提示词加「闲聊不要伪造设备回显」约束。

纯单元断言，不起任何网络监听。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def main() -> int:
    print("=" * 66)
    print("回归测试：run 逐条结果（partial）/ mock 明细 / 提示词")
    print("=" * 66)

    import netdev_cli
    import netdev_mcp

    # ── A1. CLI 汇总：三种情况 ──
    print("\n一、netdev_cli._run_json（逐条汇总）")
    allok = netdev_cli._run_json([{"command": "a", "ok": True}, {"command": "b", "ok": True}])
    check("全部成功 → ok=True / partial=False",
          allok["ok"] is True and allok["partial"] is False and allok["n_ok"] == 2, str(allok))
    part = netdev_cli._run_json([{"command": "a", "ok": True}, {"command": "b", "ok": False}])
    check("部分成功 → ok=False / partial=True / n_ok=1",
          part["ok"] is False and part["partial"] is True and part["n_ok"] == 1
          and part["n_total"] == 2, str(part))
    none = netdev_cli._run_json([{"command": "a", "ok": False}, {"command": "b", "ok": False}])
    check("全部失败 → ok=False / partial=False",
          none["ok"] is False and none["partial"] is False and none["n_ok"] == 0, str(none))

    print("\n二、netdev_cli._cmd_rejected（同屏回显判定）")
    check("Unrecognized 判失败",
          netdev_cli._cmd_rejected("        ^\nError: Unrecognized command found at '^' position."))
    check("正常回显判成功",
          not netdev_cli._cmd_rejected("GigabitEthernet0/0/0 current state : UP"))
    check("VLAN 不存在的报错判失败",
          netdev_cli._cmd_rejected("Error: The specified VLAN does not exist."))

    # ── A2. MCP 解析 ──
    print("\n三、netdev_mcp._parse_run_json（解析 CLI --json）")
    payload = {"ok": False, "partial": True, "n_ok": 1, "n_total": 2,
               "commands": [{"command": "display interface brief", "ok": True,
                             "output": "IF", "error": None},
                            {"command": "display vlan 888", "ok": False,
                             "output": "Err", "error": "Error: ..."}]}
    out = "人类可读回显\n▷ display interface brief\n" + json.dumps(payload, ensure_ascii=False)
    parsed = netdev_mcp._parse_run_json(out)
    check("能从输出尾部取出 JSON", parsed is not None)
    check("逐条命令保留各自 ok（不再整批同值）",
          bool(parsed) and [r["ok"] for r in parsed["results"]] == [True, False],
          str(parsed and parsed["results"]))
    check("人类可读部分被剥离到 raw",
          bool(parsed) and "人类可读回显" in parsed["raw"] and "commands" not in parsed["raw"],
          str(parsed and parsed["raw"])[:120])
    check("拿不到 JSON → 返回 None（调用方回退，不崩）",
          netdev_mcp._parse_run_json("只有人类可读文本，没有 JSON") is None)
    check("空输出 → None", netdev_mcp._parse_run_json("") is None)

    # ── A3. server 的 partial 判定（源码守门）──
    print("\n四、ui/server.py：partial 不再判 error（源码守门）")
    src = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    check("_call_tool 读取 partial 字段", '_partial = bool(payload.get("partial"))' in src)
    check("_bad 排除 partial（部分成功不算失败）",
          '_bad = bool(payload.get("ok") is False and not _partial)' in src)
    check("tool_execution_end 带 partial 字段", '"isError": _bad, "partial": _partial' in src)

    idx = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    check("前端对 partial 显示琥珀提示（不再一律红叉）",
          "ev.isError || ev.partial" in idx and "部分成功" in idx)

    # ── B. mock 明细 ──
    print("\n五、mock_vrp：接口明细 / port vlan / vlan 精确查询")
    import mock_vrp
    st = mock_vrp.State()
    h = lambda c: mock_vrp.handle(st, c)
    check("display interface GigabitEthernet0/0/0 → UP 明细",
          "current state : UP" in h("display interface GigabitEthernet0/0/0"))
    check("display interface GigabitEthernet0/0/1 → DOWN",
          "current state : DOWN" in h("display interface GigabitEthernet0/0/1"))
    check("display interface vlanif 1 → 带 IP",
          "192.168.1.1/24" in h("display interface vlanif 1"))
    check("未知接口 → 明确报不存在",
          "does not exist" in h("display interface GigabitEthernet9/9/9"))
    check("display port vlan → 有端口行",
          "GigabitEthernet0/0/0" in h("display port vlan"))
    check("display vlan 110 → 精确显示描述（不回全表）",
          "WaiWang" in h("display vlan 110") and "VID   Status" not in h("display vlan 110"))
    check("display vlan 888 → 报不存在",
          "does not exist" in h("display vlan 888"))
    check("display vlan（无参）→ 仍回全表",
          "WaiWang" in h("display vlan"))
    check("display interface brief 未被明细分支吃掉",
          "GE0/0/0" in h("display interface brief") and "current state" not in h("display interface brief"))

    # ── D. 提示词 ──
    print("\n六、提示词：禁止闲聊伪造设备回显")
    check("含「不要生成看起来像真实设备回显的内容」",
          "不要生成看起来像真实设备回显的内容" in src)
    check("含 partial 处置指引（失败也要说清）", "partial=true" in src)

    print("\n" + "=" * 66)
    print(f"共 {len(PASS) + len(FAIL)} 项：OK {len(PASS)} / NG {len(FAIL)}")
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
