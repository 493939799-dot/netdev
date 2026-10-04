# -*- coding: utf-8 -*-
"""lib/colorize 单测：BytePainter 跨块不漏染 + EchoPainter 输入回显三色。

背景（2026-10-04 用户报障）：
  * ACL 大输出里块边界切中的 IP 全部漏色 —— 旧 BytePainter 只扣"后缀"，
    前缀已提前吐出，拼回去不再是完整 IP。修复后整段数字/点串扣住。
  * 用户要求「一眼分清哪是输入哪是输出」—— EchoPainter 在源头登记期待
    （桥看得见所有 stdin），回显里匹配到就上色：人=蓝 / AI=紫 / 系统=灰。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import colorize as cz  # noqa: E402

ORANGE = cz.DEFAULT_COLOR.encode()
RESET = cz.RESET.encode()
BLUE = cz.HUMAN_COLOR.encode()
PURPLE = cz.AI_COLOR.encode()
GRAY = cz.SYS_COLOR.encode()


def _painted(ip: bytes) -> bytes:
    return ORANGE + ip + RESET


class TestBytePainterNoMiss(unittest.TestCase):
    """修复目标：任何块边界切法，IP 都恰好被染一次，不多不少不错拼。"""

    LINE = b"rule 5 permit ip source 192.168.10.0 0.0.0.255 destination 10.1.1.1 0.0.0.0\r\n"

    def _assert_all_ips(self, chunks):
        p = cz.BytePainter()
        out = b"".join(p.feed(c) for c in chunks) + p.flush()
        plain = out.replace(ORANGE, b"").replace(RESET, b"")
        self.assertEqual(plain, self.LINE)               # 不改一个字节
        self.assertIn(_painted(b"192.168.10.0"), out)
        self.assertIn(_painted(b"0.0.0.255"), out)
        self.assertIn(_painted(b"10.1.1.1"), out)
        self.assertIn(_painted(b"0.0.0.0"), out)

    def test_single_chunk(self):
        self._assert_all_ips([self.LINE])

    def test_split_inside_first_ip(self):
        # 边界切在 192.168.10|0 —— 旧版必漏这条
        self._assert_all_ips([self.LINE[:28], self.LINE[28:]])

    def test_split_inside_wildcard(self):
        self._assert_all_ips([self.LINE[:45], self.LINE[45:]])

    def test_split_inside_last_ip_tail(self):
        self._assert_all_ips([self.LINE[:-3], self.LINE[-3:]])

    def test_split_every_byte(self):
        # 最狠：逐字节喂 —— 兜底正确性
        self._assert_all_ips([self.LINE[i:i + 1] for i in range(len(self.LINE))])

    def test_complete_ip_at_chunk_end_then_newline(self):
        # 旧版第二类漏染：块尾恰是完整 IP，下一块以 \r 开头 → 整个 IP 不染
        p = cz.BytePainter()
        out = p.feed(b"  <sysname>ping 10.1.1.1") + p.feed(b"\r\n") + p.flush()
        self.assertIn(_painted(b"10.1.1.1"), out)

    def test_long_digit_run_at_chunk_end_no_hang(self):
        # 末尾一大段数字/点串也要扣住并正常吐出
        p = cz.BytePainter()
        out = p.feed(b"aaa 111.222.333.444.555") + p.flush()
        self.assertIn(b"111.222.333.444.555", out.replace(ORANGE, b"").replace(RESET, b""))

    def test_plain_text_unaffected(self):
        p = cz.BytePainter()
        out = p.feed(b"  GigabitEthernet0/0/1 current state : UP\r\n") + p.flush()
        self.assertNotIn(ORANGE, out)


class TestEchoPainter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netdev-colorize-")
        self.marks = os.path.join(self.tmp, "echo-marks.json")
        self.ep = cz.EchoPainter(device="huawei", marks=self.marks)

    def mark(self, text, src="ai", device="huawei"):
        cz._write_marks_file(self.marks, [
            {"text": text, "src": src, "device": device, "ts": time.time()},
        ])

    def read_marks(self):
        with open(self.marks, "r", encoding="utf-8") as fp:
            return json.load(fp)

    # ── 人输入：蓝 ──
    def test_human_echo_blue(self):
        self.ep.expect(b"display clock")
        out = self.ep.feed(b"display clock\r\n")
        self.assertIn(BLUE + b"display clock" + RESET, out)

    def test_human_echo_char_by_char(self):
        # 网页终端逐键回显：每键一小块
        self.ep.expect(b"d")
        self.ep.expect(b"i")
        self.ep.expect(b"s")
        out = b"".join(self.ep.feed(c) for c in (b"d", b"i", b"s", b"\r\n"))
        self.assertIn(BLUE + b"d" + b"i" + b"s" + RESET, out)

    # ── AI 输入：紫 ──
    def test_ai_mark_purple_and_consumed(self):
        self.mark("display version")
        self.ep.expect(b"display version")
        out = self.ep.feed(b"display version\r\n")
        self.assertIn(PURPLE + b"display version" + RESET, out)
        self.assertEqual(self.read_marks(), [])          # 标记被消费

    def test_ai_mark_chunked_stdin(self):
        # send-keys 长文本被 pty 分块：前缀也按标记色
        self.mark("display current-configuration | include vlan")
        self.ep.expect(b"display current-config")
        out = self.ep.feed(b"display current-config")
        self.assertIn(PURPLE, out)

    # ── 系统代发：灰 ──
    def test_sys_mark_gray(self):
        self.mark("display arp", src="sys")
        self.ep.expect(b"display arp")
        out = self.ep.feed(b"display arp\r\n")
        self.assertIn(GRAY + b"display arp" + RESET, out)

    # ── 设备输出：IP 橙照常 ──
    def test_output_ip_orange(self):
        out = self.ep.feed(b"  ping 10.1.1.1 : 56  data bytes\r\n") + self.ep.flush()
        self.assertIn(_painted(b"10.1.1.1"), out)
        self.assertNotIn(BLUE, out)

    # ── 失配：宁缺毋滥 ──
    def test_no_echo_expectation_dropped(self):
        # 密码不回显：期待被放弃，后面的输出不受牵连
        self.ep.expect(b"Admin@123")
        out = self.ep.feed(b"<sysname>display clock\r\n")
        self.assertNotIn(BLUE, out)
        self.assertIn(b"clock", out)
        # 后续正常输出继续走 IP 橙
        out2 = self.ep.feed(b"next hop 192.168.1.1\r\n") + self.ep.flush()
        self.assertIn(_painted(b"192.168.1.1"), out2)

    def test_unsolicited_output_between_echo(self):
        # 回显中途设备输出插队 → 当条放弃，输出原样
        self.ep.expect(b"display clock")
        out = self.ep.feed(b"%Oct  4 18:00:00 log line\r\n")
        self.assertNotIn(BLUE, out)
        self.assertIn(b"log line", out)

    # ── 标记隔离 ──
    def test_mark_other_device_not_matched(self):
        self.mark("display clock", device="h3c")
        self.ep.expect(b"display clock")
        out = self.ep.feed(b"display clock\r\n")
        self.assertIn(BLUE + b"display clock" + RESET, out)   # 不是紫

    def test_stale_mark_ignored(self):
        cz._write_marks_file(self.marks, [{"text": "display clock", "src": "ai",
                         "device": "huawei", "ts": time.time() - 999}])
        self.ep.expect(b"display clock")
        out = self.ep.feed(b"display clock\r\n")
        self.assertIn(BLUE + b"display clock" + RESET, out)

    # ── 混合流 ──
    def test_ai_then_output_sequence(self):
        self.mark("ping 10.1.1.1")
        self.ep.expect(b"ping 10.1.1.1")
        out = self.ep.feed(b"ping 10.1.1.1\r\n")
        self.assertIn(PURPLE + b"ping 10.1.1.1" + RESET, out)
        out2 = self.ep.feed(b"  Reply from 10.1.1.1: bytes=56\r\n") + self.ep.flush()
        self.assertIn(_painted(b"10.1.1.1"), out2)

    def test_flush_mid_echo_closes_color(self):
        self.ep.expect(b"display clock")
        out = self.ep.feed(b"display clo")
        self.assertIn(BLUE, out)
        tail = self.ep.flush()
        self.assertIn(RESET, tail)

    def test_write_mark_and_roundtrip(self):
        p = os.path.join(self.tmp, "wm.json")
        orig = cz.marks_file
        cz.marks_file = lambda: p
        try:
            cz.write_mark("display arp", src="sys", device="huawei")
            ep = cz.EchoPainter(device="huawei", marks=p)
            ep.expect(b"display arp")
            out = ep.feed(b"display arp\r\n")
            self.assertIn(GRAY + b"display arp" + RESET, out)
        finally:
            cz.marks_file = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
