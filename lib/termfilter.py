"""终端输入过滤 —— 只放行"人类按得出来的东西"，丢掉终端模拟器的自动应答。

【背景：都是实测踩过的坑】
  浏览器里用 xterm.js 当终端时，它会自动"应答"设备的终端查询，例如：
      ESC[?1;2c          DA1 应答
      ESC[>0;276;0c      DA2 应答
      ESC[6n             DSR 查询
      ESC[1;1R           CPR（光标位置）应答
      ESC]10;?\\x07       OSC 查询/应答
  这些应答会混在 xterm 的 onData 里，被当成"用户输入"经桥写进串口。
  设备收到 "1;2c0;276;0c" / "ROc" 这类垃圾 ⇒ 报错、屏幕被污染 ⇒
  netdev 再也认不出提示符 ⇒ 快照 / apply 全部报"设备在 90s 内没回到提示符"。

  前端（netops/ui）也过滤了一道，但不能只靠前端：
    · 页面可能是旧版 / 没刷新
    · 别的客户端（IDE 终端、脚本）也可能发应答
  桥是唯一必经之路，在这里兜底才算彻底。

【为什么这样区分是安全的】
  人类按键序列【不会】以 ESC[? 或 ESC[> 开头（方向键是 ESC[A），
  也不会是 OSC(ESC]) 或 DSR/CPR(ESC[..n / ESC[..R) —— 这些只有程序会发。
  所以下面这条规则不会误伤任何真实按键。
"""
from __future__ import annotations

import re

# 明确属于"终端应答/报告"的模式（人按不出来）
_REPLY = re.compile(
    rb"\x1b\[[\?>][0-9;]*[a-zA-Z$]"          # DA1 / DA2 / DECRPM 等
    rb"|\x1b\[[0-9;]*[nR]"                    # DSR(…n) / CPR(…R)
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC … BEL / ST
    rb"|\x1bP[^\x1b]*\x1b\\"                  # DCS … ST
)


def strip(data: bytes) -> bytes:
    """丢掉终端应答，其余原样保留（方向键 / 功能键 / 中文都不受影响）。"""
    if not data or b"\x1b" not in data:
        return data
    return _REPLY.sub(b"", data)


def dropped_count(data: bytes) -> int:
    """被丢掉了多少字节（给桥写日志用）。"""
    if not data or b"\x1b" not in data:
        return 0
    return len(data) - len(_REPLY.sub(b"", data))


def describe(data: bytes) -> str:
    """把被丢掉的应答描述成人看得懂的一行（日志/提示用）。"""
    hits = _REPLY.findall(data)
    if not hits:
        return ""
    shown = [h.replace(b"\x1b", b"ESC", 1) for h in hits[:3]]
    return b" ".join(shown).decode("utf-8", "replace")
