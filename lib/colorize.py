"""IP 地址高亮（橙色）—— 观察口 / 同屏 shell / AI 读屏 共用。

要点：
  * 只给「看起来真的是 IPv4」的串染色（前后不能是字母/点/数字）；
  * 流式输出时，可能被切断的 IP 尾巴（如 `9.9`、`9.9.9.`）先扣住，等下一块数据再决定是否染色，
    避免把半截 IP 染错、也避免漏染（带超时 flush，不会卡住显示）；
  * 关掉：环境变量 NETDEV_NO_COLOR=1；换色：NETDEV_IP_COLOR='\\033[33m'
"""
from __future__ import annotations

import os
import re
import sys

DEFAULT_COLOR = "\033[38;5;208m"      # 256 色里的橙色
RESET = "\033[39m"      # 只重置前景色，保留外层样式（如暗色/加粗）


def _color() -> str:
    return os.environ.get("NETDEV_IP_COLOR") or DEFAULT_COLOR


def enabled(stream=None) -> bool:
    if os.environ.get("NETDEV_NO_COLOR"):
        return False
    if os.environ.get("NETDEV_FORCE_COLOR"):     # 强制开（脚本/自检用）
        return True
    s = stream or sys.stdout
    try:
        return bool(s.isatty())
    except Exception:
        return False


# ── 字符串版（CLI 打印用）
_IP_S = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")


def paint_str(text: str) -> str:
    if not text:
        return text
    c = _color()
    return _IP_S.sub(lambda m: f"{c}{m.group(1)}{RESET}", text)


# ── 字节版（串口桥实时流用）
_IP_B = re.compile(rb"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
_TAIL_B = re.compile(rb"\d{1,3}(?:\.\d{0,3}){0,2}\.?$")
_SEP_B = (b"\n", b"\r", b" ", b"\t", b")", b"]", b",", b";", b"/")


def paint_bytes(data: bytes) -> bytes:
    c = _color().encode()
    r = RESET.encode()
    return _IP_B.sub(lambda m: c + m.group(1) + r, data)


class BytePainter:
    """流式染色：扣住可能被切断的 IP 尾巴，flush() 强制吐出。"""

    def __init__(self):
        self.hold = b""

    def feed(self, data: bytes) -> bytes:
        buf = self.hold + data
        self.hold = b""
        m = _TAIL_B.search(buf)
        if m and m.end() == len(buf) and not buf.endswith(_SEP_B):
            self.hold = buf[m.start():]
            buf = buf[:m.start()]
        return paint_bytes(buf)

    def pending(self) -> bool:
        return bool(self.hold)

    def flush(self) -> bytes:
        out, self.hold = paint_bytes(self.hold), b""
        return out
