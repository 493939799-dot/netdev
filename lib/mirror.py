"""镜像流 + 会话留档 —— 可观察性第 ② 层的数据源。"""
import datetime as _dt
import os
import pathlib
import re
import time

from . import paths as _paths   # 路径统一真源（装到非 ~/netops 也必须对）

ROOT = _paths.ROOT
LIVE = ROOT / "live"
LOGS = ROOT / "logs"
LIVE.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

try:
    from lib import colorize as _cz
    _paint_ip = _cz.paint_str
except Exception:                  # 独立运行时兜底
    def _paint_ip(t):
        return t

# 颜色（watch 用）
C = {"reset": "\033[0m", "dim": "\033[2m", "red": "\033[31m", "grn": "\033[32m",
     "yel": "\033[33m", "blu": "\033[34m", "cyn": "\033[36m", "bold": "\033[1m"}


def _now():
    return _dt.datetime.now().strftime("%H:%M:%S")


def _stamp():
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ── 落盘前脱敏（2026-09-27，纵深防御）────────────────────────────
# 桥侧已经把登录密码掩码了（见 tools/*_bridge.py 的 _input_for_log），
# 这里再兜一层：任何"password/secret + 值"的命令回显都抹掉。
# 为什么要在这一层也做：mirror 是所有会话日志的统一出口，
# 将来新增的通道/工具只要走 mirror，就自动受保护。
_REDACT_PATS = [
    (re.compile(r"(?i)(\b(?:password|passwd|secret)\s+(?:irreversible-cipher|cipher|simple)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:password|passwd|secret)\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(set\s+authentication\s+password\s+(?:simple|plain|cipher)?\s*)\S+"), r"\1[REDACTED]"),
    # SNMP community string 等同密码（能读甚至能写设备）
    (re.compile(r"(?i)(snmp-agent\s+community\s+(?:read|write)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\bcommunity-name\s+)\S+"), r"\1[REDACTED]"),
]


def _redact(text: str) -> str:
    for pat, rep in _REDACT_PATS:
        text = pat.sub(rep, text)
    return text


class Mirror:
    """一次会话的镜像：同时写 live/<dev>.log（追加，供 watch）与 logs/<dev>_<ts>.log。"""

    def __init__(self, device: str, keep_live: bool = True):
        self.device = device
        self.keep_live = keep_live
        self.live_path = LIVE / f"{device}.log"
        self.log_path = LOGS / f"{device}_{_stamp()}.log"
        self._live = open(self.live_path, "a", encoding="utf-8") if keep_live else None
        self._log = open(self.log_path, "a", encoding="utf-8")
        self.entries = 0
        if self._live:
            self._live.write(f"\n===== {_now()} 会话开始 =====\n")
            self._live.flush()
        self._log.write(f"### {_dt.datetime.now().isoformat(timespec='seconds')} "
                        f"device={device}\n")

    def _write(self, line: str):
        if self._live:
            self._live.write(_redact(line) + "\n")
            self._live.flush()
        self._log.write(_redact(line) + "\n")
        self._log.flush()
        self.entries += 1

    def send(self, cmd: str, risk: str = "read_only"):
        tag = {"read_only": "只读", "view_nav": "视图", "write": "写", "blocked": "拒绝"}.get(risk, risk)
        self._write(f"{_now()}  ▶ {self.device}   {cmd}   [{tag}]")

    def recv(self, text: str, ok: bool = True):
        mark = "✔" if ok else "✘"
        body = (text or "").strip()
        if not body:
            body = "(无回显)"
        for i, ln in enumerate(body.splitlines()):
            self._write(f"{_now()}  {mark} {self.device}   {ln}" if i == 0
                        else f"            {ln}")

    def note(self, text: str):
        self._write(f"{_now()}  · {self.device}   {text}")

    def close(self):
        self._write(f"{_now()}  ■ {self.device}   会话结束（{self.entries} 条）")
        for f in (self._live, self._log):
            try:
                f and f.close()
            except Exception:
                pass


def tail_live(device: str | None, follow: bool = True, lines: int = 40):
    """watch 实现：读 live/<dev>.log（或全部设备），可跟随。"""
    files = ([LIVE / f"{device}.log"] if device
             else sorted(LIVE.glob("*.log"), key=lambda p: p.stat().st_mtime))
    files = [f for f in files if f.exists()]
    if not files:
        print("（暂无镜像流：还没有发起过任何会话）")
        return
    pos = {}
    for f in files:
        data = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in data[-lines:]:
            print(_colorize(ln))
        pos[f] = f.stat().st_size
    if not follow:
        return
    try:
        while True:
            for f in list(pos):
                try:
                    size = f.stat().st_size
                except FileNotFoundError:
                    continue
                if size > pos[f]:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos[f])
                        for ln in fh.read().splitlines():
                            print(_colorize(ln), flush=True)
                    pos[f] = size
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n（已停止跟读）")


def _colorize(ln: str) -> str:
    ln = _paint_ip(ln)          # IP 地址橙色高亮（用户要求）
    if " ▶ " in ln:
        return f"{C['bold']}{C['blu']}{ln}{C['reset']}"
    if " ✔ " in ln:
        return f"{ln}" if "save" not in ln else f"{C['grn']}{ln}{C['reset']}"
    if " ✘ " in ln:
        return f"{C['red']}{ln}{C['reset']}"
    if "⚠" in ln:
        return f"{C['yel']}{ln}{C['reset']}"
    if ln.startswith("====") or ln.startswith("###"):
        return f"{C['dim']}{ln}{C['reset']}"
    return f"{C['dim']}{ln}{C['reset']}"
