"""命令分级闸门 —— 只读免确认 / 写操作需确认 / 黑名单直接拒绝。

设计原则：
  * 任何 `netdev run` 默认只允许只读命令；
  * 写操作必须走 `netdev apply`/`save`，且需要显式确认；
  * 黑名单命令（reload/format/delete/…）永不执行，连询问都没有。
"""
import re

READ_ONLY = "read_only"
VIEW_NAV = "view_nav"
WRITE = "write"
BLOCKED = "blocked"

# 黑名单：永不执行
_BLOCKED_RE = [
    r"^reload\b", r"^reset\s+saved-configuration\b", r"^reset\b.*\bsaved\b",
    r"^format\b", r"^delete\b", r"^undelete\b", r"^startup\s+saved-configuration\b",
    r"^undo\s+startup\b", r"^patch\b", r"^reset\s+factory",
    r"^shutdown\s+chassis", r"^poweroff\b", r"^reboot\b",
    r"^undo\s+flash", r"^fixdisk\b", r"^schedule\s+reboot\b",
]

# 只读：免确认
_READ_ONLY_RE = [
    r"^display\b", r"^dis\b", r"^show\b", r"^dir\b", r"^more\b", r"^pwd\b",
    r"^ping\b", r"^tracert\b", r"^traceroute\b", r"^telnet\b", r"^ssh\b",
    r"^nslookup\b",                       # DNS 查询（只读探针，2026-10-04 状态面板加）
    r"^screen-length\s+0\s+temporary\b", r"^undo\s+terminal\s+monitor\b",
    r"^terminal\s+", r"^return\s*$", r"^quit\s*$",
    r"^exit\s*$", r"^\?\s*$", r"^help\s*$",
]

# 视图导航：进入某配置视图本身不产生变更，但只能在 apply 计划内使用
_VIEW_NAV_RE = [
    r"^system-view\b", r"^aaa\s*$", r"^interface\s+\S+", r"^vlan\s+[\d ]+$",
    r"^acl\s+\S+", r"^user-interface\s+\S+", r"^isis\b", r"^ospf\b", r"^bgp\b",
    r"^ip\s+pool\s+\S+", r"^acl\s+name\s+\S+", r"^nat\s+address-group\s+\S+",
    r"^quit\s*$", r"^return\s*$",
]

_WRITE_HINT_RE = [
    r"^save\b", r"^undo\b", r"^no\b", r"^reset\b", r"^clear\b",
    r"^sysname\b", r"^ip\s+address\b", r"^rule\b", r"^nat\b", r"^shutdown\b",
    r"^port\s+", r"^description\b", r"^info-center\b", r"^clock\b",
    r"^ntp-service\b", r"^stelnet\b", r"^ssh\s+server\b", r"^telnet\s+server\b",
    r"^http\s+server\b", r"^local-user\b", r"^authentication-mode\b",
    r"^protocol\s+inbound\b", r"^user\s+privilege\b", r"^ip\s+route-static\b",
]


def _match(pats, cmd):
    return any(re.search(p, cmd, re.I) for p in pats)


def classify(cmd: str) -> str:
    """返回 READ_ONLY / VIEW_NAV / WRITE / BLOCKED。"""
    c = cmd.strip()
    if not c:
        return READ_ONLY
    if _match(_BLOCKED_RE, c):
        return BLOCKED
    if re.match(r"^save\b", c, re.I):
        # save 是独立的写操作，走 save 闸门
        return WRITE
    if _match(_READ_ONLY_RE, c):
        return READ_ONLY
    # ★ 修正：`vlan 888` 在系统视图下会**创建/修改 VLAN**（是写操作），不是单纯"进视图"
    if re.match(r"^vlan\s+\d+$", c, re.I):
        return WRITE
    if _match(_VIEW_NAV_RE, c) and not _match(_WRITE_HINT_RE, c):
        return VIEW_NAV
    return WRITE


def classify_text(text: str):
    """把"要发进同屏会话的一整段文本"分级（支持多行/分号分隔），返回 (风险, 触发它的那一行)。

    取"最危险的那一行"：blocked > write > view_nav > read_only。
    用途：screen-send 这类"往控制台打字"的通道，写操作要拦下来。
    """
    order = {READ_ONLY: 0, VIEW_NAV: 1, WRITE: 2, BLOCKED: 3}
    worst, culprit = READ_ONLY, ""
    for raw in (text or "").replace(";", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        k = classify(line)
        if order[k] > order[worst]:
            worst, culprit = k, line
        if worst == BLOCKED:
            break
    return worst, culprit


def classify_plan(commands) -> dict:
    """对整份计划分级，供 apply 前展示变更卡。"""
    out = {"read_only": [], "view_nav": [], "write": [], "blocked": []}
    for c in commands:
        out[classify(c)].append(c)
    return out


def guard_read_only(commands):
    """run 通路：只放行只读命令，返回 (可执行列表, 被拒列表)。"""
    ok, bad = [], []
    for c in commands:
        k = classify(c)
        if k == READ_ONLY:
            ok.append(c)
        else:
            bad.append((c, k))
    return ok, bad
