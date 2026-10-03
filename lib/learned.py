"""学习档案：把 AI 提议的"解析规则"存下来，一次学会、长期受益

定位
    lib/platforms.py 是**手写的**平台档案（华为已真机验证，其余待校准）。
    这个模块是**学来的**档案 —— 当手写档案遇到没见过的设备/版本、
    解析不出指标时，可以让 AI 看原始回显提议一条正则；
    自检通过 + 人批准后，存到这里，以后直接复用。

    两者关系：先查学习档案（更贴近这台设备），再查手写档案。

为什么"学一次"而不是"每次问 AI"
    每次采集都调 AI → 慢（秒级）、贵、且依赖 AI 在线。
    学一次存下来 → 之后是纯本地正则，零延迟零成本。

三道护栏（AI 不是随便就能改规则）
    ① 只提议、不自动生效 —— 必须有人在界面上点"采纳"
    ② 必须自检 —— AI 给的正则要能在那段原始回显上取出它所声称的值
    ③ 长度与回溯限制 —— 防止 AI 给出灾难性回溯的正则把服务卡死

存储
    ~/netops/state/platforms_learned.json，可读可改可删，界面上也能清空。
    格式：
    {
      "h3c_comware": {
        "cpu": {"pattern": "...", "value": 5, "sample": "原始回显片段",
                "at": "2026-09-26 12:00:00", "source": "ai", "enabled": true}
      }
    }
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time

from . import paths as _paths
ROOT = _paths.ROOT
STATE = ROOT / "state"
FILE = STATE / "platforms_learned.json"

# ── 安全限制 ──────────────────────────────────────────────────────────
MAX_PATTERN_LEN = 300          # 正则长度上限
MAX_SAMPLE_LEN = 4000          # 用于自检的样例长度上限（防慢匹配）
MATCH_TIMEOUT_HINT = 20000     # 文本切片上限（re 本身无超时，用这个限制输入）


def _load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = str(FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE)
    except Exception:
        pass


# ── 校验：AI 给的正则能不能用 ─────────────────────────────────────────
def validate(pattern: str, sample: str, expect_value=None) -> tuple[bool, str, object]:
    """校验一条候选正则。返回 (通过?, 说明, 取到的值)。

    检查项：
      ① 长度不超限
      ② 能编译
      ③ 在样例上能匹配到
      ④ 若给了 expect_value，取到的值必须与之一致（AI 自证）
      ⑤ 有捕获组（否则取不到值）
    """
    pat = (pattern or "").strip()
    if not pat:
        return False, "正则为空", None
    if len(pat) > MAX_PATTERN_LEN:
        return False, f"正则过长（{len(pat)} > {MAX_PATTERN_LEN}）", None
    try:
        rx = re.compile(pat, re.I)
    except re.error as e:
        return False, f"正则编译失败：{e}", None
    if rx.groups < 1:
        return False, "正则里没有捕获组 (...)，取不到值", None
    text = (sample or "")[:MATCH_TIMEOUT_HINT]
    if not text.strip():
        return False, "没有可用来自检的样例回显", None
    try:
        t0 = time.time()
        m = rx.search(text)
        if time.time() - t0 > 3.0:              # 匹配太慢 → 疑似灾难性回溯
            return False, "匹配耗时超过 3 秒，疑似灾难性回溯（拒绝）", None
    except Exception as e:
        return False, f"匹配异常：{e}", None
    if not m:
        return False, "在样例回显里匹配不到 —— AI 的提议不成立", None
    got = m.group(1)
    # 数值化尝试（CPU/内存这类指标必须是数字）
    num = None
    try:
        num = float(re.sub(r"[^\d.\-]", "", got))
    except Exception:
        num = None
    if expect_value is not None:
        try:
            if abs(float(num) - float(expect_value)) > 1e-6:
                return False, f"取出的值({num})与 AI 声称的值({expect_value})不一致", None
        except Exception:
            return False, f"AI 声称的值无法比较：{expect_value!r}", None
    return True, "自检通过", num


# ── 读写 ─────────────────────────────────────────────────────────────
def get(platform: str, key: str) -> str | None:
    """取学到的正则（启用状态）。没有返回 None。"""
    d = _load()
    it = (d.get(platform or "") or {}).get(key or "")
    if isinstance(it, dict) and it.get("enabled", True):
        return it.get("pattern") or None
    return None


def put(platform: str, key: str, pattern: str, sample: str = "",
        value=None, source: str = "ai") -> tuple[bool, str]:
    """存一条学到的规则（会先校验）。"""
    ok, why, got = validate(pattern, sample, value)
    if not ok:
        return False, why
    d = _load()
    d.setdefault(platform or "unknown", {})[key] = {
        "pattern": pattern.strip(),
        "value": (value if value is not None else got),
        "sample": (sample or "")[:600],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "enabled": True,
    }
    _save(d)
    return True, "已保存"


def remove(platform: str, key: str) -> bool:
    d = _load()
    if (d.get(platform) or {}).pop(key, None) is not None:
        if not d.get(platform):
            d.pop(platform, None)
        _save(d)
        return True
    return False


def clear() -> int:
    """清空全部学到的规则。返回清掉的条数。"""
    d = _load()
    n = sum(len(v) for v in d.values() if isinstance(v, dict))
    _save({})
    return n


def all_rules() -> list[dict]:
    """列出来（给界面展示/管理）。"""
    d = _load()
    out = []
    for plat, keys in sorted(d.items()):
        if not isinstance(keys, dict):
            continue
        for k, it in sorted(keys.items()):
            if isinstance(it, dict):
                out.append({"platform": plat, "key": k,
                            "pattern": it.get("pattern", ""),
                            "value": it.get("value"),
                            "at": it.get("at", ""),
                            "source": it.get("source", ""),
                            "enabled": it.get("enabled", True),
                            "sample": (it.get("sample") or "")[:200]})
    return out


def patterns_for(platform: str, key: str) -> list[str]:
    """给解析器用：学到的正则排在手写档案之前（更贴近这台设备）。"""
    p = get(platform, key)
    return [p] if p else []

# ── 作用域（2026-09-26 加）────────────────────────────────────────────
#   问题：原来只按 platform 存。若设备没标 platform（比如临时接入的目标），
#         会被塞进同一个 "(未标注)" 桶 —— 华为的规则可能被锐捷设备用上。
#   现在分两级：
#     dev:<设备名>   这台设备专属（最精确，永不串到别的设备）
#     plat:<平台名>  平台共享（同厂商同代设备批量受益）
#   查找顺序：先 dev → 再 plat。未标 platform 时【强制】只存设备级。
SCOPE_DEV = "dev"
SCOPE_PLAT = "plat"


def scope_key(scope: str, name: str) -> str:
    return f"{SCOPE_DEV}:{name}" if scope == SCOPE_DEV else f"{SCOPE_PLAT}:{name}"


def get_scoped(dev: str, platform: str, key: str) -> tuple[str | None, str]:
    """两级查找：设备级优先，再平台级。返回 (pattern, 来源说明)。"""
    p1 = get(scope_key(SCOPE_DEV, dev or ""), key)
    if p1:
        return p1, f"设备级({dev})"
    if platform:
        p2 = get(scope_key(SCOPE_PLAT, platform), key)
        if p2:
            return p2, f"平台级({platform})"
    return None, ""


def put_scoped(scope: str, name: str, key: str, pattern: str, sample: str = "",
               value=None, source: str = "ai") -> tuple[bool, str]:
    """按作用域保存。未标平台的设备会被强制降级为设备级。"""
    if not name:
        return False, "缺少作用域名称"
    if scope == SCOPE_PLAT and name in ("(未标注)", "unknown", ""):
        scope, name = SCOPE_DEV, name or "unknown"
    return put(scope_key(scope, name), key, pattern, sample, value, source)


def all_rules_scoped() -> list[dict]:
    """列出来时把 scope 解析成人能看懂的形式。"""
    out = []
    for r in all_rules():
        plat = r.get("platform", "")
        if plat.startswith("dev:"):
            r2 = {**r, "scope": "设备级", "scope_name": plat[4:]}
        elif plat.startswith("plat:"):
            r2 = {**r, "scope": "平台级", "scope_name": plat[5:]}
        else:
            # 兼容老档案：没有前缀的按平台级看
            r2 = {**r, "scope": "平台级(旧)", "scope_name": plat}
        out.append(r2)
    return out
