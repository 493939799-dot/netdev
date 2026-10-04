"""IP 地址高亮（橙色）+ 输入回显著色 —— 观察口 / 同屏 shell / AI 读屏 共用。

要点：
  * 只给「看起来真的是 IPv4」的串染色（前后不能是字母/点/数字）；
  * 流式输出时，块边界可能切进 IP 中间。feed() 会把缓冲区末尾【整段】数字/点串
    扣住不发（不只是 IP 的后缀），等下一块数据再决定是否染色——
    这样任何 IP 必然完整地出现在某一次染色里，绝不漏染、也绝不跨块错拼
    （带超时 flush，不会卡住显示）。旧版只扣后缀，实测 ACL 大输出里
    块边界切中的 IP 全部漏色（2026-10-04 修）；
  * EchoPainter：把「人/AI 敲进设备的字」在设备回显里染成不同颜色，
    一眼分清哪是输入哪是输出（详见类注释）；
  * 关掉：环境变量 NETDEV_NO_COLOR=1；换色：NETDEV_IP_COLOR='\\033[33m'
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

DEFAULT_COLOR = "\033[38;5;208m"      # 256 色里的橙色
RESET = "\033[39m"      # 只重置前景色，保留外层样式（如暗色/加粗）

# 输入回显三色（256 色）：
#   人   = 浅蓝 —— 明显区别于默认前景，又不刺眼
#   AI   = 紫   —— 和蓝拉开色相距离
#   系统 = 灰   —— 监控探测这类 netdev 代发的噪音，压暗即可
HUMAN_COLOR = os.environ.get("NETDEV_HUMAN_COLOR") or "\033[38;5;75m"
AI_COLOR = os.environ.get("NETDEV_AI_COLOR") or "\033[38;5;141m"
SYS_COLOR = os.environ.get("NETDEV_SYS_COLOR") or "\033[38;5;245m"


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
# 末尾「可能还没到齐的 IP 残片」：一段纯数字/点（至少含一个数字）。
# ★ 整段扣住，而不是只扣「最多两组点分数」的后缀 —— 后者的致命伤：
#   块边界切进 IP 时前缀已经被吐出去了，后缀拼回去不再是完整 IP，永远漏染。
_RUN_B = re.compile(rb"[0-9.]*[0-9][0-9.]*$")


def paint_bytes(data: bytes) -> bytes:
    c = _color().encode()
    r = RESET.encode()
    return _IP_B.sub(lambda m: c + m.group(1) + r, data)


class BytePainter:
    """流式染色：扣住可能被切断的 IP 残片（整段数字/点串），flush() 强制吐出。"""

    def __init__(self):
        self.hold = b""

    def feed(self, data: bytes) -> bytes:
        buf = self.hold + data
        self.hold = b""
        m = _RUN_B.search(buf)
        if m and m.end() == len(buf):
            # 末尾是数字/点串：它可能是一个还没收完的 IP（或 IP 的中段）。
            # 整段扣住等下一块 —— 拼回去永远是设备发来的原始字节流，不会错。
            self.hold = buf[m.start():]
            buf = buf[:m.start()]
        return paint_bytes(buf)

    def pending(self) -> bool:
        return bool(self.hold)

    def flush(self) -> bytes:
        out, self.hold = paint_bytes(self.hold), b""
        return out


# ══════════════════════════════════════════════════════════════════════════
#  输入回显著色：人=蓝 / AI=紫 / 系统代发=灰（设备输出保持默认+IP 橙）
#
#  为什么不靠「行首是提示符」猜输入：
#    人敲键时设备是逐字符回显的，敲 `dis` 时行里只有 `dis`，提示符
#    `<sysname>` 早就作为输出流过去了 —— 按行猜永远猜不到。
#  正确的锚点在【源头】：桥本来就看得见所有发往设备的键盘字节
#    （人的击键和 AI 的 tmux send-keys 都从窗格 stdin 进桥）。
#    发出去时记一笔「期待回显」，设备回显里匹配到的字节就上色；
#    AI/系统代发靠 state/echo-marks.json 里的标记区分（netdev screen send
#    是唯一收口，发命令前写标记）。
#  宁缺毋滥：匹配不上（密码不回显、控制键、输出插队）就放弃该条着色。
# ══════════════════════════════════════════════════════════════════════════

MARKS_TTL = 30.0        # echo-marks.json 里标记的有效期（秒）
EXPECT_MAX = 32         # 期待回显队列上限（防设备不回显时无限堆积）


def marks_file() -> str:
    """AI/系统代发标记文件路径（state 目录，两套安装副本经软链指向同一处）。"""
    try:
        from lib import paths as _paths
        return str(_paths.state_dir() / "echo-marks.json")
    except Exception:
        return os.path.expanduser("~/netops/state/echo-marks.json")


def write_mark(text: str, src: str = "ai", device: str = "") -> None:
    """screen-send 发命令前记一笔：这些字是 AI/系统敲的，回显要染 src 色。

    尽力而为：写失败不影响命令发送（着色只是观感，不能挡设备操作）。
    """
    try:
        p = marks_file()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        now = time.time()
        marks = [m for m in _read_marks_file(p)
                 if isinstance(m, dict) and now - m.get("ts", 0) < MARKS_TTL]
        marks.append({"text": text, "src": src, "device": device or "", "ts": now})
        _write_marks_file(p, marks)
    except Exception:
        pass


def _read_marks_file(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            v = json.load(fp)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _write_marks_file(path: str, marks: list) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(marks[-64:], fp, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


class EchoPainter:
    """输出流着色器 = 输入回显三色 + 设备输出 IP 橙。

    桥的用法：
      ep = EchoPainter()            # 进程起一个
      ep.expect(data)               # stdin 转发去设备前：这串字要发出去
      ep.feed(设备回显) -> bytes    # 上屏的字节（已着色）
      ep.flush() -> bytes           # 空闲时把扣住的尾巴吐出来
    """

    def __init__(self, device: str = "", marks: str | None = None):
        self.device = device or ""
        self.marks_path = marks if marks is not None else marks_file()
        self._painter = BytePainter()      # 设备输出仍走 IP 橙（含扣残片）
        self._expect: list[tuple[bytes, str]] = []   # (待回显字节, 颜色)
        self._in_color = b""               # 当前正在连续着色的颜色（b""=不在）
        self._marks_cache: tuple[float, list] = (0.0, [])

    # ── 标记文件（AI/系统代发）─────────────────────────────────────────
    def _load_marks(self) -> list:
        try:
            st = os.stat(self.marks_path)
            if self._marks_cache[0] != st.st_mtime:
                self._marks_cache = (st.st_mtime, _read_marks_file(self.marks_path))
        except Exception:
            self._marks_cache = (0.0, [])
        now = time.time()
        return [m for m in self._marks_cache[1]
                if isinstance(m, dict) and now - m.get("ts", 0) < MARKS_TTL]

    def _match_mark(self, data: bytes) -> str:
        """stdin 字节对上 fresh 标记 → 返回 src 颜色并消费标记；对不上 → 人色。"""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return HUMAN_COLOR
        marks = self._load_marks()
        for i, m in enumerate(marks):
            mt = str(m.get("text", ""))
            if self.device and m.get("device") and m.get("device") != self.device:
                continue
            src = str(m.get("src", "ai"))
            if text == mt:
                marks.pop(i)
                _write_marks_file(self.marks_path, marks)
                self._marks_cache = (self._marks_cache[0], marks)
                return AI_COLOR if src == "ai" else SYS_COLOR
            if mt.startswith(text):       # send-keys 长文本被 pty 分块：先按 src 色
                return AI_COLOR if src == "ai" else SYS_COLOR
        return HUMAN_COLOR

    # ── 源头登记（桥转发 stdin 前调）──────────────────────────────────
    def expect(self, data: bytes) -> None:
        """这串字要发给设备了：记进期待回显队列，并按标记/来源定色。"""
        if not data:
            return
        color = self._match_mark(data)
        self._expect.append((data, color))
        if len(self._expect) > EXPECT_MAX:
            self._expect.pop(0)

    # ── 输出着色 ──────────────────────────────────────────────────────
    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        rst = RESET.encode()
        while data:
            if self._expect:
                exp, color = self._expect[0]
                cb = color.encode()
                n = _common_prefix_len(data, exp)
                if n > 0:
                    if not self._in_color:
                        out += cb
                        self._in_color = color
                    out += data[:n]
                    if n == len(exp):
                        self._expect.pop(0)          # 这条回显齐了
                        # 下一条同色 → 连着染不出 RESET；异色/没了 → 收色
                        if not self._expect or self._expect[0][1] != color:
                            out += rst
                            self._in_color = b""
                    else:
                        self._expect[0] = (exp[n:], color)
                    data = data[n:]
                    continue
                # 和期待对不上（输出插队 / 设备不回显）→ 放弃这条，看下一条
                if self._in_color:
                    out += rst
                    self._in_color = b""
                self._expect.pop(0)
                continue
            # 设备输出：交给 IP 橙（BytePainter 自己扣数字残片）
            out += self._painter.feed(data)
            data = b""
        return bytes(out)

    def pending(self) -> bool:
        return bool(self._painter.hold) or bool(self._in_color)

    def flush(self) -> bytes:
        """空闲兜底：把 BytePainter 扣住的数字残片吐出来（按输出染色）。"""
        if self._in_color:            # 正在着色中被掐断 → 先收色
            self._in_color = b""
            return RESET.encode() + self._painter.flush()
        return self._painter.flush()


def _common_prefix_len(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i
