#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：一轮对话的【回复必须写回上下文】（2026-10-05，P0）。

守的是一条被真机实测抓到的性质：

    AI 每一轮的【纯文本回复】必须进入 self.messages，
    否则下一轮它根本看不到自己说过什么。

事故现场（用户真机，2026-10-05 14:18-14:20，会话 mock-hw）：:

    14:19:20  AI 讲了 VLAN 888 的故事（流水里原文完整）
    14:20:02  它说「我上下文里没有讲过故事的记录——你上一条让我讲故事，我还没讲」
    14:20:42  更进一步：「我当时的动作是调用 netdev_list，没有输出任何故事内容」

根因不是提示词、也不是幻觉，而是 ui/server.py 的 _turn()：
    self.messages 只在【user】和【带 tool_calls 的 assistant】两处写入；
    纯文本结束时直接 break —— 回复只发给了本轮模型，从不进入上下文。
    于是 AI 的自我记忆里只有"调过什么工具"，没有"说过什么"。

    决定性旁证（本测试用最小场景复现）：事故对话第 2 轮 AI 主动说
    「先说设备，再说故事」——它不是加戏，而是它的上下文里
    用户第 1 轮"讲个故事"从未被回应，它以为还欠一个。

本测试全程离线：不发网络请求、不连 API、不写真实流水（_ai_audit 被替换）。

用法：
    python3 tests/test_ai_turn_context.py
"""
from __future__ import annotations

import os
import sys

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


# ── 事件构造（形状与 _handle_sse_event 的入参完全一致）────────────────
def _text(t: str) -> dict:
    return {"choices": [{"delta": {"content": t}}]}


def _tool(i: int, cid: str, name: str, args: str) -> dict:
    return {"choices": [{"delta": {"tool_calls": [
        {"index": i, "id": cid, "function": {"name": name, "arguments": args}}]}}]}


class _Resp:
    """假 HTTP 响应：_turn 只看 status / read()。"""
    status = 200

    def read(self, n=None):  # noqa: D401
        return b""


class _Harness:
    """把一趟 _turn 需要的网络面全部替换掉，只保留上下文拼接逻辑。"""

    def __init__(self, script: list[list[dict]], raise_at: int | None = None):
        self.script = script
        self.raise_at = raise_at
        self.step = 0
        self.captured: list[list[dict]] = []   # 每次 _chat 收到的 msgs
        self.events: list[dict] = []           # _ai_audit 收到的事件

        s = S.DirectSession("tctxTEST", tools="read")
        s.cfg = {"ok": True, "model": "fake", "note": ""}
        s.use_netdev = False
        s._tool_schemas = []
        s._emit = lambda ev: None              # 静音前端事件
        s._chat = self._chat
        s._read_sse = self._read_sse
        s._call_tool = lambda name, args: "FAKE_TOOL_RESULT"
        self.s = s

    def _chat(self, msgs, stream=True):
        self.captured.append([dict(m) for m in msgs])
        return _Resp()

    def _read_sse(self, resp):
        if self.raise_at is not None and self.step == self.raise_at:
            # 先吐一段文本，再抛异常 —— 模拟"中途报错"
            for ev in self.script[self.step] if self.step < len(self.script) else []:
                self.s._handle_sse_event(ev)
            self.step += 1
            raise RuntimeError("boom")
        if self.step >= len(self.script):
            return
        for ev in self.script[self.step]:
            self.s._handle_sse_event(ev)
        self.step += 1

    def run(self, text: str):
        _orig = S._ai_audit
        S._ai_audit = lambda aid, ev: self.events.append(ev)
        try:
            self.s._turn(text)
        finally:
            S._ai_audit = _orig
        return self.s

    # 便捷取用
    def assistants(self):
        return [m for m in self.s.messages if m.get("role") == "assistant"]

    def texts(self):
        return [m.get("content") for m in self.assistants() if m.get("content")]

    def audit(self, typ: str):
        return [e for e in self.events if e.get("type") == typ]


def main() -> int:
    print("== 纯文本轮：回复必须写回上下文 ==")
    h = _Harness([[_text("我不讲故事，只调设备。")]])
    s = h.run("给我讲个故事")
    check("messages 末尾是 assistant 文本",
          s.messages[-1] == {"role": "assistant", "content": "我不讲故事，只调设备。"},
          f"实际={s.messages[-1] if s.messages else None}")
    check("assistant 文本一条不多不少", h.texts() == ["我不讲故事，只调设备。"], str(h.texts()))
    check("turn_end 流水仍记录完整全文（未被清空）",
          (h.audit("turn_end") or [{}])[0].get("assistant") == "我不讲故事，只调设备。",
          str((h.audit("turn_end") or [{}])[0].get("assistant")))

    print("\n== 工具轮：assistant(tool_calls) + tool 结果照旧写回 ==")
    h2 = _Harness([
        [_tool(0, "c1", "netdev_list", "{}")],      # 迭代1：调工具
        [_text("设备 2 台。")],                      # 迭代2：纯文本收尾
    ])
    s2 = h2.run("我有几台设备")
    roles = [m.get("role") for m in s2.messages]
    check("含 tool 角色消息", "tool" in roles, str(roles))
    check("含带 tool_calls 的 assistant",
          any(m.get("role") == "assistant" and m.get("tool_calls") for m in s2.messages))
    check("工具轮之后的纯文本也写回了",
          s2.messages[-1] == {"role": "assistant", "content": "设备 2 台。"},
          str(s2.messages[-1]))

    print("\n== ★ 核心：第二轮必须能看见第一轮的回复 ==")
    h3 = _Harness([
        [_text("不负责讲故事。")],                      # 轮1
        [_tool(0, "c1", "netdev_list", "{}")],          # 轮2 迭代1
        [_text("先说设备，再说故事。")],                 # 轮2 迭代2
    ])
    h3.run("给我讲个故事")
    h3.run("我有几台设备")
    last_msgs = h3.captured[-1]
    check("第二轮发给模型的上下文里带着第一轮的回复",
          any(m.get("role") == "assistant" and m.get("content") == "不负责讲故事。"
              for m in last_msgs),
          "—— 修复前这里必然缺失（导致 AI 反复重答同一问题）")
    check("第二轮也带上了自己的工具调用记录",
          any(m.get("role") == "assistant" and m.get("tool_calls") for m in last_msgs))

    print("\n== 异常路径：中途报错也要保住已产出的文本 ==")
    h4 = _Harness([[_text("正在查……")]], raise_at=0)
    s4 = h4.run("查一下")
    check("异常后 messages 里仍有部分回复",
          s4.messages[-1] == {"role": "assistant", "content": "正在查……"},
          str(s4.messages[-1] if s4.messages else None))
    check("异常事件已落流水", bool(h4.audit("error")))

    print("\n== 中止路径：按停止也要保住已产出的文本 ==")
    h5 = _Harness([[_text("写到一半")]])
    _orig_read = h5.s._read_sse

    def _read_then_abort(resp):
        _orig_read(resp)
        h5.s._abort.set()          # 模拟用户按下停止
    h5.s._read_sse = _read_then_abort
    s5 = h5.run("继续")
    check("中止后 messages 里仍有部分回复",
          s5.messages[-1] == {"role": "assistant", "content": "写到一半"},
          str(s5.messages[-1] if s5.messages else None))
    check("turn_end 标记 aborted", bool((h5.audit("turn_end") or [{}])[0].get("aborted")))

    print("\n== 边界：空回复不写空消息 / 不重复写 ==")
    h6 = _Harness([[]])
    s6 = h6.run("你好")
    check("没有产出文本时不塞空 assistant",
          [m.get("role") for m in s6.messages] == ["user"], str(s6.messages))

    h7 = _Harness([[_text("唯一一条")]])
    s7 = h7.run("a")
    s7._read_sse = lambda resp: None          # 第二轮不再产出
    h7.run("b")
    check("正常路径不会重复写回（_stored 去重生效）",
          h7.texts() == ["唯一一条"], str(h7.texts()))

    print(f"\n共 {OK + NG} 项：OK {OK} / NG {NG}")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if NG else 0


if __name__ == "__main__":
    sys.exit(main())
