#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib.pane.strip_ansi 单测：ANSI 控制序列清理 + ConPTY 模式串过滤。

背景（2026-10-05 Windows 移植优化）：
  * ConPTY 会向 stdout 注入终端模式串（如 \x1b[?1004h\x1b[?9001h），
    旧版 strip_ansi 正则只匹配单字节最终字符的 CSI，长参数序列清不干净，
    导致 screen-read 纯文本里残留「?1004h?9001h」等垃圾字符。
  * 新版正则覆盖完整 CSI 语法（参数+中间字节+最终字节）、OSC、字符集切换等。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import pane  # noqa: E402


class TestStripAnsiBasic(unittest.TestCase):
    """基础 ANSI 序列清理。"""

    def test_plain_text_unchanged(self):
        """纯文本不该被改动。"""
        self.assertEqual(pane.strip_ansi(b"Hello World\r\n"), "Hello World\n")
        self.assertEqual(pane.strip_ansi(b"display clock\r\n"), "display clock\n")

    def test_sgr_color_stripped(self):
        """SGR 颜色序列（\x1b[31m 等）应被清理。"""
        # 红色文字
        raw = b"\x1b[31mError\x1b[0m\r\n"
        out = pane.strip_ansi(raw)
        self.assertNotIn("\x1b", out)
        self.assertIn("Error", out)

    def test_cursor_move_stripped(self):
        """光标移动序列应被清理。"""
        raw = b"\x1b[2A\x1b[10Ctext\x1b[0J"
        out = pane.strip_ansi(raw)
        self.assertNotIn("\x1b", out)
        self.assertEqual(out.strip(), "text")

    def test_cr_normalized(self):
        """CRLF / CR 都应规整成 LF。"""
        self.assertEqual(pane.strip_ansi(b"a\r\nb"), "a\nb")
        self.assertEqual(pane.strip_ansi(b"a\rb"), "a\nb")
        self.assertEqual(pane.strip_ansi(b"a\nb"), "a\nb")


class TestStripAnsiConPTY(unittest.TestCase):
    """ConPTY 特有模式串清理（Windows 移植新增）。"""

    def test_dec_private_mode_set(self):
        """DEC 私有模式设置（?1004h 等）应被完整清理。"""
        # ConPTY 常见：开启焦点追踪 + 某些扩展模式
        raw = b"\x1b[?1004h\x1b[?9001h<prompt>"
        out = pane.strip_ansi(raw)
        self.assertNotIn("?1004h", out)
        self.assertNotIn("?9001h", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("<prompt>", out)

    def test_dec_private_mode_reset(self):
        """DEC 私有模式复位（?1004l 等）应被清理。"""
        raw = b"\x1b[?1004l\x1b[?25ltext"
        out = pane.strip_ansi(raw)
        self.assertNotIn("?1004l", out)
        self.assertNotIn("?25l", out)
        self.assertIn("text", out)

    def test_multi_param_csi(self):
        """多参数 CSI（如 \x1b[1;31;42m）应被完整清理。"""
        raw = b"\x1b[1;31;42mBold Red on Green\x1b[0m"
        out = pane.strip_ansi(raw)
        self.assertNotIn("\x1b", out)
        self.assertIn("Bold Red on Green", out)

    def test_osc_sequence(self):
        """OSC 序列（窗口标题等）应被清理。"""
        raw = b"\x1b]0;Window Title\x07content"
        out = pane.strip_ansi(raw)
        self.assertNotIn("\x1b]", out)
        self.assertNotIn("Window Title", out)
        self.assertIn("content", out)

    def test_osc_st_terminator(self):
        """OSC 用 ST（\x1b\\）终止的也应被清理。"""
        raw = b"\x1b]0;Title\x1b\\text"
        out = pane.strip_ansi(raw)
        self.assertNotIn("Title", out)
        self.assertIn("text", out)

    def test_charset_switch(self):
        """字符集切换（\x1b(B \x1b)0 等）应被清理。"""
        raw = b"\x1b(Bnormal\x1b)0lines"
        out = pane.strip_ansi(raw)
        self.assertNotIn("\x1b(B", out)
        self.assertNotIn("\x1b)0", out)
        self.assertIn("normal", out)
        self.assertIn("lines", out)

    def test_conpty_mixed_with_output(self):
        """ConPTY 模式串混在正常输出里的典型场景。"""
        # 模拟 SSH 登录后 ConPTY 注入的一串模式设置
        raw = (
            b"\x1b[?1004h"         # 焦点追踪开
            b"\x1b[?9001h"         # ConPTY 扩展
            b"\x1b[?25h"           # 光标显示
            b"Welcome to router\r\n"
            b"<AR111-S> "
            b"\x1b[?1004l"         # 焦点追踪关（某时刻）
        )
        out = pane.strip_ansi(raw)
        self.assertNotIn("?1004", out)
        self.assertNotIn("?9001", out)
        self.assertNotIn("?25h", out)
        self.assertIn("Welcome to router", out)
        self.assertIn("<AR111-S>", out)
        self.assertNotIn("\x1b", out)

    def test_empty_and_edge(self):
        """空输入和边界情况。"""
        self.assertEqual(pane.strip_ansi(b""), "")
        self.assertEqual(pane.strip_ansi(b"\x1b"), "")  # 裸 ESC
        self.assertEqual(pane.strip_ansi(b"\x07"), "")  # 裸 BEL
        # 不完整的 CSI（只有 ESC[ 没有最终字节）—— 保守处理
        out = pane.strip_ansi(b"\x1b[")
        self.assertNotIn("\x1b", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
