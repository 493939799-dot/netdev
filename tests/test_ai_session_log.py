#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：AI 会话流水 + 上下文压缩（2026-10-05）。

覆盖四组「一坏就全坏」的性质：

  1. 流水必须落盘、append-only、一行一事件（含时间戳）—— 这是 AI 行为的
     唯一证据链；实测事故是「AI 先编内容、后以绝对语气否认讲过」
     （根因：上下文内存态 + 压缩无痕丢），流水就是给这类问题兜底的。
  2. 压缩必须【不丢可追溯性】：真摘要优先；摘要失败要如实写明
     "未生成摘要 + 原文见流水"，而不是塞个占位符假装压缩过；
     且保留最近 4 条原始消息（旧实现只留 2 条）。
  3. 提示词必须包含"自证纪律"三条（禁止绝对断言 / 被问历史先查证据 /
     前提冲突先澄清）—— 人设是行为的第一道防线，删了就要红。
  4. 会话清单（/api/ai/list 的数据源）能看到活着会话的 aid/name/device ——
     前端刷新恢复依赖它。

用法：
    python3 tests/test_ai_session_log.py
全程离线：不连 API、不起服务、不发网络请求。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ui.server as S  # noqa: E402

OK = 0
NG = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, NG
    if cond:
        OK += 1
        print(f"  OK  {name}")
    else:
        NG += 1
        FAILS.append(name + (f" —— {detail}" if detail else ""))
        print(f"  NG  {name}" + (f"  ← {detail}" if detail else ""))


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="netdev-ai-log-"))
    S.AI_LOG_DIR = tmp                      # 隔离：不污染真实 logs/

    print("== 1. 流水落盘（append-only / 一行一事件）==")
    S._ai_audit("aidTEST1", {"type": "session_start", "model": "m1",
                             "device": "mock-hw", "tools": "read+netdev"})
    S._ai_audit("aidTEST1", {"type": "user", "text": "你好"})
    S._ai_audit("aidTEST1", {"type": "tool_call", "name": "netdev_run",
                             "args": {"device": "mock-hw", "command": "display clock"},
                             "result_chars": 123, "result_head": "2026-09-16"})
    S._ai_audit("aidTEST1", {"type": "turn_end", "assistant": "现在是 14:04",
                             "seconds": 2.5, "aborted": False})
    f = tmp / "aidTEST1" / "session.jsonl"
    check("流水文件已创建", f.is_file())
    lines = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("一行一事件（4 行）", len(lines) == 4, f"实际 {len(lines)}")
    recs = [json.loads(x) for x in lines]
    check("每条都有时间戳（ts/at）", all(r.get("ts") and r.get("at") for r in recs))
    check("事件类型齐全", [r["type"] for r in recs] ==
          ["session_start", "user", "tool_call", "turn_end"])
    check("工具调用记了名称+参数",
          recs[2]["name"] == "netdev_run" and recs[2]["args"]["command"] == "display clock")

    # append-only：再写一条不覆盖旧的
    S._ai_audit("aidTEST1", {"type": "user", "text": "第二条"})
    lines2 = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("append-only（旧事件仍在）", len(lines2) == 5 and
          json.loads(lines2[0])["type"] == "session_start")

    # 恶意 aid（路径穿越）必须被净化：不得越出 logs/ 目录树
    S._ai_audit("../../etc/passwd", {"type": "user", "text": "x"})
    bad_names = [p.name for p in tmp.glob("*") if ".." in p.name or "/" in p.name]
    check("aid 被净化（无路径穿越 / 不越界）", not bad_names,
          f"出现异常目录: {bad_names}")

    print("== 2. 会话清单 ==")
    S._ai_audit("aidTEST2", {"type": "session_start", "model": "m2", "device": "hw"})
    S._ai_audit("aidTEST2", {"type": "user", "text": "hi"})
    sess = S.ai_log_sessions()
    got = {s["aid"]: s for s in sess}
    check("列出两个会话", {"aidTEST1", "aidTEST2"} <= set(got))
    check("统计事件条数", got["aidTEST1"]["events"] == 5 and got["aidTEST2"]["events"] == 2,
          f"实际 {[ (s['aid'], s['events']) for s in sess ]}")
    check("带出设备与模型", got["aidTEST2"]["device"] == "hw" and
          got["aidTEST2"]["model"] == "m2")
    check("带出起止时间", bool(got["aidTEST1"]["first"]) and bool(got["aidTEST1"]["last"]))

    print("== 3. 上下文压缩：真摘要 / 回退如实 / 保留 4 条 ==")
    s = S.DirectSession("aidCMP", model="m", tools="read")
    s.messages = [{"role": "user", "content": f"旧消息{i}"} for i in range(10)]
    called = {"n": 0}

    def fake_sum(_old, _ins=""):
        called["n"] += 1
        return "①设备 mock-hw 时钟 14:04；②执行过 display clock（只读）"
    s._summarize_messages = fake_sum
    ok = s.compact()
    check("压缩执行成功", ok is True)
    check("调用了真摘要", called["n"] == 1)
    check("保留最近 4 条原文", s.messages[-1]["content"] == "旧消息9" and
          len([m for m in s.messages if m["role"] == "user"]) == 4,
          f"用户消息数 {len([m for m in s.messages if m['role']=='user'])}")
    head = s.messages[0]
    check("压缩提示是 system 且含摘要", head["role"] == "system" and
          "display clock" in head["content"])
    check("压缩提示写明原文可查", "session.jsonl" in head["content"])
    comp = [json.loads(x) for x in
            (tmp / "aidCMP" / "session.jsonl").read_text(encoding="utf-8").splitlines()]
    cev = [r for r in comp if r["type"] == "compact"]
    check("压缩事件落流水（含丢弃条数）", len(cev) == 1 and cev[0]["dropped"] == 6)

    # 摘要失败 → 如实说明，不许假装
    s2 = S.DirectSession("aidCMP2", model="m", tools="read")
    s2.messages = [{"role": "user", "content": f"x{i}"} for i in range(10)]
    s2._summarize_messages = lambda *a, **k: ""
    check("摘要失败也返回 ok", s2.compact() is True)
    check("失败时如实写明未生成摘要",
          "未生成摘要" in s2.messages[0]["content"])

    # 消息不够多 → 不动
    s3 = S.DirectSession("aidCMP3", model="m", tools="read")
    s3.messages = [{"role": "user", "content": "只有两条"}]
    check("消息少时不压缩", s3.compact() is False and len(s3.messages) == 1)

    print("== 4. 提示词自证纪律（删了就要红）==")
    p = S.DEBUG_SYSTEM_PROMPT
    check("提示词含『禁止绝对断言』", "绝对断言" in p)
    check("提示词指向流水文件", "session.jsonl" in p)
    check("提示词含『前提冲突先澄清』", "先澄清" in p)
    check("提示词仍保留『禁止翻留档』", "禁止翻留档" in p)

    print("== 5. /api/ai/list 数据源 ==")
    alive = S.DirectSession("aidLIVE", model="m", tools="read")
    alive.name, alive.device = "mock-hw-2", "mock-hw"
    with S.AI_LOCK:
        S.AI_SESSIONS.clear()
        S.AI_SESSIONS["aidLIVE"] = alive
    h = S.Handler.__new__(S.Handler)          # 不启服务，只测 handler 逻辑
    h._json = lambda obj, code=200: obj       # 截获 JSON 输出
    r = h._api_ai_list()
    items = r["sessions"]
    check("列出活会话", any(x["aid"] == "aidLIVE" for x in items))
    it = [x for x in items if x["aid"] == "aidLIVE"][0]
    check("带 name/device（前端恢复要用）",
          it["name"] == "mock-hw-2" and it["device"] == "mock-hw")
    with S.AI_LOCK:
        S.AI_SESSIONS.clear()

    print(f"\n共 {OK + NG} 项：OK {OK} / NG {NG}")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if NG else 0


if __name__ == "__main__":
    sys.exit(main())
