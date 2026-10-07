"""串口独占锁：记录「谁持有 / 何时拿的 / 干什么用的」。

背景（2026-09-26 事故）
    原来判断"串口是否被占"靠 `tmux list-windows | grep <设备名>` —— 只认名字，
    有两个致命缺陷：
      1. **只认同屏桥**：MCP 的直连工具（netdev_serial_run 等）不经 tmux，锁完全看不见它；
      2. **没有业主概念**：不知道是谁占的、占了多久、干什么 —— 出事后无法追责，
         也无法判断"该不该抢占"（比如僵尸锁该清，正常作业不该清）。

现在
    * 任何要打开串口的一方都必须 `acquire(port, holder, purpose)`；
    * 锁文件 ~/netops/state/portlocks.json 记录 holder / pid / 时间 / 用途；
    * **僵尸锁自动清理**：持有者进程已不存在（或不是本机活着的过程）→ 视为过期，可被抢占；
    * `release` 只在 holder 匹配时生效（防止 A 释放了 B 的锁）。

锁的语义是"协作锁"（cooperative），不是内核强制锁 ——
它不阻止别人硬开串口，但能让所有走 netdev 的路径先看锁、先报错、先追责。
真正的硬防护仍是 engine.guard_serial_exclusive()（它在锁判断之外还会查同屏会话）。
"""
from __future__ import annotations

import json
import os
import pathlib
import time

from . import host, paths as _paths
ROOT = _paths.ROOT
STATE = ROOT / "state"
LOCKFILE = STATE / "portlocks.json"

# 锁在多少秒内"心跳"过算活着（桥会定期 touch）
STALE_AFTER = 90


def _now() -> float:
    return time.time()


def _load() -> dict:
    try:
        with open(LOCKFILE, encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = str(LOCKFILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, LOCKFILE)
    except Exception:
        pass


def _alive(pid: int) -> bool:
    """进程是否还活着（本机）。跨平台统一走 lib.host.pid_alive。"""
    return host.pid_alive(pid)


def status(port: str) -> dict | None:
    """返回该串口当前的锁信息；无锁返回 None。僵尸锁会被顺带清掉。"""
    d = _load()
    it = d.get(port)
    if not isinstance(it, dict):
        return None
    pid = int(it.get("pid") or 0)
    age = _now() - float(it.get("at") or 0)
    if not _alive(pid):
        d.pop(port, None)
        _save(d)
        return None
    it = dict(it)
    it["age"] = round(age, 1)
    it["stale"] = age > STALE_AFTER
    return it


def acquire(port: str, holder: str, purpose: str = "", force: bool = False) -> tuple[bool, str]:
    """尝试取得串口锁。返回 (是否成功, 说明)。

    已被别人持有时：若对方是僵尸（进程没了）→ 自动接管；
    若还是活的且 force=False → 拒绝并说明是谁占着。
    """
    cur = status(port)
    if cur and cur.get("holder") != holder:
        if not force:
            who = cur.get("holder", "?")
            pur = cur.get("purpose") or "未注明"
            mins = int(cur.get("age", 0) // 60)
            return False, (f"串口被占用：{who}（用途 {pur}，已持有 {mins} 分 {int(cur.get('age',0)%60)} 秒，pid {cur.get('pid')}）")
    d = _load()
    d[port] = {"holder": holder, "pid": os.getpid(), "purpose": purpose,
               "at": _now(), "heartbeat": _now()}
    _save(d)
    return True, "已取得锁"


def heartbeat(port: str, holder: str) -> bool:
    """续期（桥在跑就定期调，便于别人判断锁是否僵尸）。"""
    d = _load()
    it = d.get(port)
    if not isinstance(it, dict) or it.get("holder") != holder:
        return False
    it["heartbeat"] = _now()
    _save(d)
    return True


def release(port: str, holder: str) -> bool:
    """释放锁。只有 holder 匹配才生效（防止误放别人的锁）。"""
    d = _load()
    it = d.get(port)
    if not isinstance(it, dict):
        return False
    if it.get("holder") != holder:
        return False
    d.pop(port, None)
    _save(d)
    return True


def force_release(port: str, by: str = "operator") -> bool:
    """人工强制释放（网页上的「释放」按钮走这里）。"""
    d = _load()
    if port not in d:
        return False
    d.pop(port, None)
    _save(d)
    return True


def all_locks() -> list[dict]:
    """列出所有有效锁（顺带清理僵尸）。"""
    d = _load()
    out = []
    changed = False
    for port, it in list(d.items()):
        if not isinstance(it, dict):
            d.pop(port, None); changed = True; continue
        pid = int(it.get("pid") or 0)
        if not _alive(pid):
            d.pop(port, None); changed = True; continue
        age = _now() - float(it.get("at") or 0)
        hb = _now() - float(it.get("heartbeat") or it.get("at") or 0)
        out.append({"port": port, "holder": it.get("holder", "?"), "pid": pid,
                    "purpose": it.get("purpose", ""), "age": round(age, 1),
                    "idle": round(hb, 1), "stale": hb > STALE_AFTER})
    if changed:
        _save(d)
    return sorted(out, key=lambda x: x["port"])
