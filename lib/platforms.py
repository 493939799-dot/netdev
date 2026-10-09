"""平台档案：把"怎么读设备"从代码里抽出来，按厂商分派

为什么需要
    原来的监控是**华为专用**的：
      · MON_CMDS 写死 `display cpu-usage` / `display memory-usage`
      · 解析正则只认 `CPU Usage:` / `Memory Using Percentage Is:`
      · 接口名甚至写死 `GigabitEthernet0/0/2`
    换一台华三：命令能跑但输出句式不同 → 正则失配 → 全 `--`
    换一台锐捷：`display ...` 命令本身不存在 → `Error: Unrecognized command` → 全空

设计
    每个平台一份档案 = 命令表 + 解析规则（每指标多条正则，按序试）。

    三层保障：
      ① 按 devices.toml 的 platform 选档案（命令与解析都对得上厂商）
      ② 同一指标挂多条正则 —— 兼容同厂商不同版本，也能兜住 platform 标错的情况
      ③ 全都匹配不上 → 返回 None（界面显示「—」并标注未适配）
         ★ 绝不返回 0 或任何编造值：解析失败时显示"没读到"，
           比显示一个假的 0 安全得多（假的 0 会让人以为设备正常）

关于"未校准"
    华为部分是在真机（AR111-S, VRP V200R010C10SPC700）上验证过的。
    H3C / 锐捷 / Cisco 的命令与句式依据公开手册编写，**尚未在真机校准** ——
    档案里用 `verified` 字段标明，界面对未校准平台会提示"该平台指标待真机校准"。
    将来接上那类设备，把实际回显贴进来，改这里的正则即可，不用动业务代码。
"""
from __future__ import annotations

import re

# ── 接口名匹配（用于挑出"物理网口"，各家前缀不同）──────────────────────
PHYS_IF_RE = re.compile(
    r"^(GigabitEthernet|GE|XGigabitEthernet|10GE|25GE|40GE|100GE|"
    r"FastEthernet|Fa|Ethernet|Eth|Cellular|Serial|M-GigabitEthernet)",
    re.I,
)


def is_physical(name: str) -> bool:
    return bool(PHYS_IF_RE.match(name or ""))


# ── 平台档案 ────────────────────────────────────────────────────────────
#   cmds  : 指标 → 命令
#   parse : 指标 → [正则...]（按序试，第一个匹配上的生效）
#   brief_parse : 接口概览的专用解析（可选；没有就用通用的空白分词法）
PROFILES: dict[str, dict] = {
    # ══════════════ 华为 VRP（★ 已在真机验证）══════════════
    "huawei_vrp": {
        "vendor": "华为",
        "verified": True,
        "cmds": {
            "cpu":   "display cpu-usage",
            "mem":   "display memory-usage",
            "brief": "display interface brief",
            "nat":     "display nat session statistics",
            "dhcp":    "display ip pool",
            "arp":     "display arp all",
            "optical": "display transceiver diagnosis",
        },
        "parse": {
            "cpu": [
                r"CPU utilization for (?:ten|five) seconds:\s*([\d.]+)%",
                r"CPU\s+Usage\s*:\s*([\d.]+)%",
                r"CPU utilization\s*[:：]\s*([\d.]+)%",
            ],
            "mem": [
                r"Memory Using Percentage Is:\s*(\d+)\s*%",
                r"Memory\s+Usage\s*[:：]\s*(\d+)\s*%",
                r"Memory utilization.*?(\d+)\s*%",
            ],
        },
    },

    # ══════════════ 华三 Comware（待真机校准）══════════════
    "h3c_comware": {
        "vendor": "华三",
        "verified": False,
        "cmds": {
            "cpu":   "display cpu-usage",
            "mem":   "display memory",
            "brief": "display interface brief",
            "nat":     "display nat statistics",
            "dhcp":    "display dhcp server statistics",
            "arp":     "display arp",
            "optical": "display transceiver diagnosis",
        },
        "parse": {
            "cpu": [
                r"CPU utilization in five seconds:\s*(\d+)\s*%",
                r"(\d+)%\s*in last 5 seconds",
                r"CPU\s+usage\s*[:：]\s*(\d+)\s*%",
                r"CPU\s+Usage\s*[:：]\s*([\d.]+)\s*%",
            ],
            "mem": [
                r"Memory usage:\s*(\d+)\s*%",
                r"Memory\s+utilization\s*[:：]\s*(\d+)\s*%",
                r"(\d+)%\s*used",
            ],
        },
    },

    # ══════════════ 锐捷 RGOS（待真机校准）══════════════
    "ruijie_os": {
        "vendor": "锐捷",
        "verified": False,
        "cmds": {
            "cpu":   "show cpu",
            "mem":   "show memory",
            "brief": "show interfaces status",
            "nat":     "show ip nat statistics",
            "dhcp":    "show ip dhcp binding",
            "arp":     "show arp",
            "optical": "show interfaces transceiver",
        },
        "parse": {
            "cpu": [
                r"CPU utilization.*?:\s*(\d+)\s*%",
                r"(\d+)%\s*in 5 seconds",
                r"CPU\s+using\s+rate\s*[:：]\s*(\d+)\s*%",
            ],
            "mem": [
                r"Memory utilization.*?:\s*(\d+)\s*%",
                r"Memory\s+using\s+rate\s*[:：]\s*(\d+)\s*%",
                r"Used\s+rate\s*[:：]\s*(\d+)\s*%",
            ],
        },
    },

    # ══════════════ 思科 IOS（待真机校准）══════════════
    "cisco_ios": {
        "vendor": "思科",
        "verified": False,
        "cmds": {
            "cpu":   "show processes cpu | include CPU utilization",
            "mem":   "show memory statistics",
            "brief": "show ip interface brief",
            "nat":     "show ip nat statistics",
            "dhcp":    "show ip dhcp binding",
            "arp":     "show ip arp",
            "optical": "show interfaces transceiver",
        },
        "parse": {
            "cpu": [
                r"CPU utilization for five seconds:\s*(\d+)%",
                r"one minute:\s*(\d+)%",
            ],
            "mem": [
                r"Processor Pool Total.*?(\d+)",
                r"Used\s+Free\s+.*?(\d+)%",
            ],
        },
    },

    # ══════════════ 迈普 MyPower S 系列（类华为 CLI，待真机校准）══════════════
    # 迈普 S 系列（MyPower S 系列交换机）命令体系跟华为 VRP 高度同源：
    #   display cpu-usage / display memory-usage / display interface brief 都有，
    #   所以命令表直接复用华为；只把解析正则放宽成"兼容迈普回显措辞"的并集。
    "maipu_s": {
        "vendor": "迈普",
        "verified": False,
        "cmds": {
            "cpu":   "display cpu-usage",
            "mem":   "display memory-usage",
            "brief": "display interface brief",
            "nat":     "display nat session statistics",
            "dhcp":    "display ip pool",
            "arp":     "display arp all",
            "optical": "display transceiver diagnosis",
        },
        "parse": {
            "cpu": [
                r"CPU utilization for (?:ten|five) seconds:\s*([\d.]+)%",
                r"CPU\s+(?:Usage|usage|using\s+rate)\s*[:：]\s*([\d.]+)\s*%",
                r"CPU utilization\s*[:：]\s*([\d.]+)%",
                r"(\d+)\s*%\s*in\s+(?:last\s+)?5\s*seconds",
            ],
            "mem": [
                r"Memory\s+Using\s+Percentage\s+Is:\s*(\d+)\s*%",
                r"Memory\s+(?:Usage|usage|using\s+rate)\s*[:：]\s*(\d+)\s*%",
                r"Memory\s+utilization.*?(\d+)\s*%",
                r"(\d+)\s*%\s*used",
            ],
        },
    },
}

DEFAULT_PLATFORM = "huawei_vrp"


# ── 平台自动识别（开箱即用的关键：用户不用懂 platform 代号）─────────────
#   从 `display version`（或各家等价命令）的回显里，按厂商特征字判断厂商。
#   各家 banner 里的稳定特征词：
#     华为 → "Huawei VRP" / "VRP (R) software"
#     华三 → "H3C Comware" / "Comware Software"
#     锐捷 → "Ruijie" / "RGOS"
#     思科 → "Cisco IOS" / "Cisco Internetwork Operating System"
#     迈普 → "MyPower" / "Maipu"
_VENDOR_HINTS: list[tuple[str, str]] = [
    ("huawei_vrp",  r"Huawei\s+VRP|VRP\s*\(R\)\s+software|Quidway"),
    ("h3c_comware", r"H3C\s+Comware|Comware\s+Software"),
    ("ruijie_os",   r"Ruijie|RGOS"),
    ("cisco_ios",   r"Cisco\s+IOS|Cisco\s+Internetwork\s+Operating\s+System"),
    ("maipu_s",     r"MyPower|Maipu|迈普"),
]


def detect_platform(text: str) -> str | None:
    """从版本回显里识别厂商，返回 platform key；识别不出返回 None。

    顺序很重要：华三 Comware 回显里也可能出现 "Huawei" 字样（很少），
    但更常见的是华为回显不带 H3C/Ruijie/Cisco。按特征词特异性排序即可，
    这里直接按表序试，命中即返回。
    """
    if not text:
        return None
    for key, pat in _VENDOR_HINTS:
        try:
            if re.search(pat, text, re.I):
                return key
        except re.error:
            continue
    return None


# 各家"看版本"的命令（识别时优先按已知 platform，未知时逐个试）
VERSION_CMDS: list[str] = [
    "display version",
    "show version",
]


def profile_of(platform: str | None) -> dict:
    """取平台档案；不认识就退回默认（华为），但标注 'assumed' 供界面提示。"""
    p = (platform or "").strip().lower()
    if p in PROFILES:
        return {**PROFILES[p], "key": p, "assumed": False}
    # 常见别名
    alias = {
        "huawei": "huawei_vrp", "vrp": "huawei_vrp",
        "h3c": "h3c_comware", "comware": "h3c_comware", "hp_comware": "h3c_comware",
        "ruijie": "ruijie_os", "rgos": "ruijie_os",
        "cisco": "cisco_ios", "ios": "cisco_ios",
        "maipu": "maipu_s", "mypower": "maipu_s", "maipu_s": "maipu_s",
    }
    if p in alias:
        k = alias[p]
        return {**PROFILES[k], "key": k, "assumed": False}
    return {**PROFILES[DEFAULT_PLATFORM], "key": DEFAULT_PLATFORM, "assumed": True}


def pick(patterns: list[str], text: str, cast=float):
    """按序试多条正则，返回第一个匹配到的值；全不匹配返回 None。

    ★ 关键：返回 None 而不是 0 —— 调用方据此显示「—」，
      绝不把"没读到"伪装成"数值是 0"。
    """
    if not text:
        return None
    for pat in patterns or []:
        try:
            m = re.search(pat, text, re.I)
        except re.error:
            continue
        if m:
            try:
                return cast(m.group(1))
            except Exception:
                continue
    return None


def commands_for(platform: str | None, keys: list[str] | None = None) -> dict:
    """取该平台的命令表（可只要其中几个指标）。"""
    prof = profile_of(platform)
    cmds = prof.get("cmds", {})
    if keys:
        return {k: cmds[k] for k in keys if k in cmds}
    return dict(cmds)


# ── 命令探测（第 2 层：不靠 platform 也能找对命令）─────────────────────
#   为什么需要：devices.toml 里 platform 可能写错/写空，或者同厂商不同形态
#   （交换机跟路由器、老版本与新版本）。与其让监控全空，不如把各家的候选
#   命令依次试一遍，取第一条“不报 Unrecognized command 且有输出”的。
#   实测串口每条命令 1~2 秒，所以：① 先按 platform 猜，命中就不探测
#   ② 探测结果缓存起来（同一台设备只探一次）
#
#   ★ 2026-10-01 修（原来这套缓存是【单向】的，白探一遍又一遍）：
#     - `_CMD_CACHE` 只在内存里，UI 服务一重启就丢；
#     - 更严重的是 collect_metrics 里虽然 `cache_get` 了，塞进一个 `retry` 字典
#       就**再也没被用过**，`mon_cmds()` 也完全无视缓存 →
#       每次采集都拿平台默认命令先撞一次墙，再逐条试候选。
#     - 现在：① 在建计划阶段就用缓存覆盖默认命令（命中即零探测成本）；
#             ② 缓存落盘到 <root>/config/cmd-cache.json，重启不丢。
#   持久化位置跟着 NETDEV_ROOT，与 netdev_cli 的 ROOT 口径一致（支持 --prefix 安装）。
import json as _json
import os as _os
import pathlib as _pathlib
import threading as _threading

_CMD_CACHE: dict[str, str] = {}
_CACHE_LOCK = _threading.Lock()
_CACHE_LOADED = False


def _cache_file() -> _pathlib.Path:
    root = _os.environ.get("NETDEV_ROOT") or str(_pathlib.Path(__file__).resolve().parent.parent)
    return _pathlib.Path(root).expanduser() / "config" / "cmd-cache.json"


def _cache_load() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    _CACHE_LOADED = True
    try:
        d = _json.loads(_cache_file().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _CMD_CACHE.update({str(k): str(v) for k, v in d.items() if isinstance(v, str)})
    except Exception:
        pass          # 没有 / 坏了 → 空缓存起步，不影响功能


def _cache_save() -> None:
    """原子写：先写 .tmp 再 replace —— 避免半截文件把缓存读坏。"""
    try:
        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(_CMD_CACHE, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        _os.replace(tmp, f)
    except Exception:
        pass


# 各指标的候选命令（按“先华为、再华三、再锐捷、再思科”的顺序）
CANDIDATES: dict[str, list[str]] = {
    "cpu": [
        "display cpu-usage",                 # 华为 / 华三 / 迈普 S
        "show cpu",                          # 锐捷
        "show cpu monitor",                  # 迈普 MP 系列
        "show processes cpu | include CPU utilization",   # 思科
        "display cpu",                       # 兜底
    ],
    "mem": [
        "display memory-usage",              # 华为 / 迈普 S
        "display memory",                    # 华三
        "show memory",                       # 锐捷 / 迈普 MP 系列
        "show memory statistics",            # 思科
    ],
    "brief": [
        "display interface brief",           # 华为 / 华三
        "show interfaces status",            # 锐捷
        "show ip interface brief",           # 思科
        "display ip interface brief",        # 华为三层口视角
    ],
    # ── 2026-10-04 状态面板三层指标（体验/变化层）。全部只读；
    #    设备不支持时走候选探测 + 静默降级为「--」，绝不当 0 用。
    "nat": [
        "display nat session statistics",    # 华为 AR（会话统计）
        "show ip nat statistics",            # 思科 / 锐捷
        "display nat statistics",            # 华三
        "display nat session all",           # 兜底（输出大，放最后）
    ],
    "dhcp": [
        "display ip pool",                   # 华为 / 迈普 S
        "show ip dhcp binding",              # 思科 / 锐捷
        "display dhcp server statistics",    # 华三
        "display dhcp server used",          # 华三兜底
    ],
    "arp": [
        "display arp all",                   # 华为 / 华三 / 迈普
        "show ip arp",                       # 思科
        "show arp",                          # 锐捷
    ],
    "optical": [
        "display transceiver diagnosis",     # 华为 / 华三
        "show interfaces transceiver",       # 思科 / 锐捷
    ],
}

# 认错命令时的典型回显（命中就说明这条不能用）
BAD_CMD_RE = re.compile(
    r"(Unrecognized command|Invalid command|Unknown command|"
    r"Ambiguous command|Incomplete command|"
    r"% ?Invalid input|Error:\s*Wrong parameter|Too many parameters found)",
    re.I,
)


def looks_like_bad_command(text: str) -> bool:
    """这段回显看起来是“命令不被支持”吗。"""
    return bool(BAD_CMD_RE.search(text or ""))


def cache_get(dev: str, key: str) -> str | None:
    _cache_load()
    return _CMD_CACHE.get(f"{dev}:{key}")


def cache_put(dev: str, key: str, cmd: str) -> None:
    """探测命中 → 记住。同时落盘，下次（含重启后）直接用，不再重复探测。"""
    _cache_load()
    with _CACHE_LOCK:
        _CMD_CACHE[f"{dev}:{key}"] = cmd
        _cache_save()


def cache_all(dev: str, keys: list[str] | None = None) -> dict:
    """取某台设备已学到的命令（键为指标名）。用于「学到的规则」面板展示。"""
    _cache_load()
    pre = f"{dev}:"
    out = {k[len(pre):]: v for k, v in _CMD_CACHE.items() if k.startswith(pre)}
    if keys:
        out = {k: v for k, v in out.items() if k in keys}
    return out


_SHOW_PLATFORMS = {"cisco_ios", "ruijie_os"}


def candidates_for(key: str, prefer_platform: str | None = None) -> list[str]:
    """候选命令：先放该平台的首选命令，再补上其它家的。

    ★ 2026-10-09：候选按 CLI 方言过滤 —— 平台已知是 display 系厂商
    （华为/华三/迈普）就不再试 show 系候选（思科/锐捷），反之亦然。
    真机实测：AR111-S 的 cpu 候选 5 条里 4 条是 show 系报错，全是纯噪音
    （用户在屏幕上看到的全是这些无意义的 Unrecognized）。
    平台未知时仍全部候选都试 —— 探测是唯一的发现手段。
    """
    first = ""
    try:
        first = (profile_of(prefer_platform).get("cmds") or {}).get(key) or ""
    except Exception:
        first = ""
    fam = "show" if prefer_platform in _SHOW_PLATFORMS else (
        "display" if prefer_platform else "")
    out: list[str] = []
    if first:
        out.append(first)
    for c in CANDIDATES.get(key, []):
        if c in out:
            continue
        if fam and c.split(" ", 1)[0].lower() != fam:
            continue
        out.append(c)
    return out


def plan_commands(platform: str | None, dev: str = "", keys: list[str] | None = None) -> dict:
    """建采集计划：平台默认命令 **叠加** 这台设备已学到的命令。

    ★ 2026-10-01 新增。此前「探测命中 → cache_put」的结果从来没人用，
      每轮采集都要先把平台默认命令撞一次墙再逐条试候选（串口下每条约 1~2 秒）。
      现在学到的直接生效：一台需要 `show cpu` 的锐捷，第二次采集就是零探测。
    """
    cmds = commands_for(platform, keys or ["cpu", "mem", "brief"])
    if not cmds:
        cmds = dict(DEFAULT_MON_CMDS)
    if dev:
        for k, v in cache_all(dev).items():
            if v and (not keys or k in keys):
                cmds[k] = v
    return cmds


# 兜底命令表（档案里没有该指标时的最后一道）
DEFAULT_MON_CMDS: dict[str, str] = {
    "cpu": "display cpu-usage",
    "mem": "display memory-usage",
    "brief": "display interface brief",
}
