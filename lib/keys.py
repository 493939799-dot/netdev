"""退格键模式自动识别 —— 不猜，直接问设备。

原理：在 CLI 提示符下打一个字符，再发退格键字节，看设备是否回了"擦除动作"
（`\\x08 \\x08` / `ESC[1D` / ` \\x08` 之类）。两种字节各试一次，谁有效用谁。

安全约束：
  * 只在能看到 CLI 提示符（<...> / [...]）时探测；
  * 探测用的字符不回车（不会执行任何命令）；
  * 探测完用 Ctrl+U 清掉试探内容；
  * 结果缓存到 ~/netops/state/keys.json（按设备名），可用 --redo 重测。
"""
from __future__ import annotations

import json
import pathlib
import re
import select
import time

from . import paths as _paths   # 路径统一真源

STATE = _paths.state_dir()
STATE.mkdir(parents=True, exist_ok=True)
CACHE = STATE / "keys.json"

PROMPT = re.compile(rb"[<\[]\w[\w\-]*[>\]]\s*$")
ERASE_PATTERNS = (b"\x08 \x08", b"\x1b[1D", b" \x08", b"\x1b[D")
BEL = b"\x07"

MODE_DESC = {
    "bs": "把终端退格键 0x7F 翻成 0x08（设备只认 BS）",
    "del": "把 0x08 翻成 0x7F（设备只认 DEL）",
    "pass": "原样透传（设备两种都认）",
    "unknown": "未能识别（沿用默认 bs）",
}


# ───────────────────────────────────────────── 缓存
def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def save_cache(d: dict):
    CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def cached_mode(device: str):
    return load_cache().get(device, {}).get("mode")


def remember(device: str, mode: str, evidence: str, port: str = ""):
    d = load_cache()
    d[device] = {"mode": mode, "evidence": evidence, "port": port,
                 "detected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_cache(d)


# ───────────────────────────────────────────── 探测
def _drain(ser, wait=0.35, echo=None) -> bytes:
    buf = b""
    end = time.time() + wait
    while time.time() < end:
        r, _, _ = select.select([ser.fileno()], [], [], 0.1)
        if r:
            d = ser.read(8192)
            if d:
                buf += d
                if echo:
                    echo(d)
    return buf


def at_prompt(ser, timeout=6.0, echo=None) -> bool:
    ser.write(b"\r\n")          # 先敲个回车，让设备把提示符吐出来
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([ser.fileno()], [], [], 0.2)
        if r:
            d = ser.read(8192)
            if d:
                buf += d
                if echo:
                    echo(d)
                if PROMPT.search(buf.rstrip()):
                    return True
    return False


def _try(ser, seq: bytes, echo=None) -> tuple[bool, bytes]:
    """打一个字符，发给定退格字节，判断设备是否接受。"""
    ser.write(b"x")
    _drain(ser, 0.35, echo)
    ser.write(seq)
    resp = _drain(ser, 0.5, echo)
    ok = any(p in resp for p in ERASE_PATTERNS)
    beep = BEL in resp
    # 清掉试探内容（Ctrl+U 实测可清整行；不行再用退格刷几次）
    ser.write(b"\x15")
    _drain(ser, 0.3, echo)
    ser.write(b"\x08\x08\x08")
    _drain(ser, 0.3, echo)
    return (ok and not beep), resp


def detect(ser, echo=None) -> tuple[str, str]:
    """返回 (mode, evidence)。ser 必须是已打开的、可读写的串口对象。"""
    if not at_prompt(ser, echo=echo):
        return "unknown", "没看到 CLI 提示符（可能在登录界面），跳过探测"

    bs_ok, bs_resp = _try(ser, b"\x08", echo)
    del_ok, del_resp = _try(ser, b"\x7f", echo)

    ev = (f"0x08->{'接受' if bs_ok else '不接受'}  "
          f"0x7F->{'接受' if del_ok else '不接受'}")
    if bs_ok and del_ok:
        return "pass", ev + "  ⇒ 两者都认，原样透传"
    if bs_ok:
        return "bs", ev + "  ⇒ 设备只认 BS(0x08)"
    if del_ok:
        return "del", ev + "  ⇒ 设备只认 DEL(0x7F)"
    return "unknown", ev + "  ⇒ 都没识别到，沿用默认 bs"
