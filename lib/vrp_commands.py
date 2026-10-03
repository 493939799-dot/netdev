"""命令提示与拼写建议 —— 让 netdev 也能"提示"你。

两块能力：
  1) hint()      ：拼写建议（按内置常用命令表模糊匹配）
  2) CHEATSHEET  ：厂商常用命令速查（netdev cmds）
设备侧的完整补全仍然用它自己的 `?`（netdev hint <设备> "前缀" 会把设备的 ? 帮助取回来给你看）。
"""
import difflib

COMMON = [
    # 只读
    "display version", "display clock", "display device", "display esn",
    "display current-configuration", "display saved-configuration",
    "display interface brief", "display interface GigabitEthernet0/0/0",
    "display ip interface brief", "display ip routing-table", "display ip pool",
    "display vlan", "display port vlan", "display mac-address",
    "display acl 2000", "display nat outbound", "display nat session all",
    "display arp all", "display users", "display ssh server status",
    "display ssh user-information", "display telnet server status",
    "display cpu-usage", "display memory-usage", "display logbuffer",
    "display reboot-info", "display startup", "display controller", "dir",
    # 视图/配置
    "system-view", "sysname", "clock timezone BJ add 08:00:00",
    "ntp-service unicast-server", "interface Vlanif1", "ip address",
    "ip route-static", "acl 2000", "rule 5 permit source", "rule 5 deny source",
    "nat outbound", "dhcp select global", "ip pool", "network",
    "gateway-list", "dns-list", "stelnet server enable", "ssh server acl",
    "undo telnet server enable", "undo http server enable",
    "user-interface vty 0 4", "authentication-mode aaa", "protocol inbound ssh",
    "user privilege level 15", "local-user", "service-type ssh", "quit", "return",
    "save vrpcfg.zip", "reset saved-configuration", "reload",
]

CHEATSHEET = {
    "huawei": [
        ("看版本/序列号", ["display version", "display esn"]),
        ("看配置", ["display current-configuration", "display saved-configuration",
                    "display current-configuration | include <关键字>"]),
        ("看接口/IP/VLAN", ["display interface brief", "display ip interface brief",
                            "display vlan", "display port vlan"]),
        ("看路由/ARP", ["display ip routing-table", "display arp all"]),
        ("看 NAT/ACL", ["display nat outbound", "display nat session all", "display acl 2000"]),
        ("排障", ["display cpu-usage", "display memory-usage", "display logbuffer",
                  "display reboot-info", "ping -c 5 8.8.8.8", "ping -a <源IP> -c 5 <目标>",
                  "tracert -m 8 8.8.8.8"]),
        ("改配置骨架", ["system-view", "sysname <名字>", "interface Vlanif1",
                       "ip address <IP> <掩码>", "ip route-static 0.0.0.0 0.0.0.0 <网关>",
                       "quit", "save vrpcfg.zip"]),
        ("开 SSH", ["stelnet server enable", "rsa local-key-pair create",
                    "aaa", "local-user <用户> password irreversible-cipher <密码>",
                    "local-user <用户> service-type ssh",
                    "user-interface vty 0 4", "authentication-mode aaa",
                    "protocol inbound ssh", "user privilege level 15"]),
        ("设备侧求助", ["?（当前位置能敲什么）", "display ?（display 后面能接什么）",
                       "display vlan ?（还能接什么参数）", "Tab（自动补全当前单词）"]),
    ],
    "h3c": [
        ("看版本", ["display version", "display device manuinfo"]),
        ("看配置", ["display current-configuration", "display saved-configuration"]),
        ("看接口/IP", ["display interface brief", "display ip interface brief",
                       "display vlan", "display ip routing-table"]),
        ("改配置", ["system-view", "sysname <名字>", "interface Vlan-interface1",
                    "ip address <IP> <掩码>", "quit", "save force"]),
        ("开 SSH", ["ssh server enable", "public-key local create rsa",
                    "local-user <用户> class manage",
                    "password simple <密码>", "service-type ssh",
                    "authorization-attribute user-role network-admin",
                    "line vty 0 63", "authentication-mode scheme",
                    "protocol inbound ssh"]),
    ],
    "ruijie": [
        ("看版本", ["show version", "show version slots"]),
        ("看配置", ["show running-config", "show startup-config"]),
        ("看接口/IP", ["show interfaces status", "show ip interface brief",
                       "show vlan", "show ip route"]),
        ("改配置", ["configure terminal", "hostname <名字>", "interface vlan 1",
                    "ip address <IP> <掩码>", "end", "write"]),
        ("开 SSH", ["enable service ssh-server", "crypto key generate rsa",
                    "username <用户> privilege 15 password <密码>",
                    "line vty 0 4", "login local", "transport input ssh"]),
    ],
    "cisco": [
        ("看版本", ["show version", "show inventory"]),
        ("看配置", ["show running-config", "show startup-config"]),
        ("看接口/IP", ["show ip interface brief", "show vlan brief",
                       "show interfaces status", "show ip route"]),
        ("改配置", ["configure terminal", "hostname <名字>", "interface vlan 1",
                    "ip address <IP> <掩码>", "end", "write memory"]),
        ("开 SSH", ["ip domain-name <域名>", "crypto key generate rsa modulus 2048",
                    "username <用户> privilege 15 secret <密码>",
                    "line vty 0 4", "login local", "transport input ssh"]),
    ],
}

VENDOR_ALIAS = {"华为": "huawei", "hw": "huawei", "vrp": "huawei",
                "华三": "h3c", "comware": "h3c", "锐捷": "ruijie", "rgos": "ruijie",
                "思科": "cisco", "ios": "cisco"}


def resolve_vendor(name: str) -> str:
    n = (name or "huawei").strip().lower()
    return VENDOR_ALIAS.get(n, n if n in CHEATSHEET else "huawei")


SYNONYM = {"show": "display", "sh": "display", "dis": "display", "disp": "display"}


def _first_tokens():
    return sorted({c.split()[0].lower() for c in COMMON})


def _repair_word(w: str) -> set:
    """常见打字错误修复：相邻字符调换 / 删一个字符（用于把 shwo → show）。"""
    out = set()
    for i in range(len(w) - 1):
        out.add(w[:i] + w[i + 1] + w[i] + w[i + 2:])
    for i in range(len(w)):
        out.add(w[:i] + w[i + 1:])
    return out


def hint(cmd: str, n: int = 3) -> list[str]:
    """对可能是拼错的命令给出建议：先修首词（含跨厂商同义词），再拿整条命令模糊匹配。"""
    c = (cmd or "").strip()
    if not c:
        return []
    parts = c.split()
    first = parts[0].lower()
    tokens = _first_tokens()

    fixed = None
    if first in tokens:
        fixed = first
    elif first in SYNONYM:
        fixed = SYNONYM[first]
    else:
        cand = difflib.get_close_matches(first, tokens, n=1, cutoff=0.75)
        if not cand:
            for r in _repair_word(first):
                if r in SYNONYM:
                    cand = [SYNONYM[r]]
                    break
                cand = difflib.get_close_matches(r, tokens + list(SYNONYM), n=1, cutoff=0.85)
                if cand:
                    cand = [SYNONYM.get(cand[0], cand[0])]
                    break
        fixed = cand[0] if cand else None

    pool = [x for x in COMMON if x.split()[0].lower() == fixed] if fixed else COMMON
    rest = " ".join(parts[1:])
    if fixed and rest:
        rebuilt = f"{fixed} {rest}"
        m = difflib.get_close_matches(rebuilt, pool, n=n, cutoff=0.35)
        if m:
            return m
        subs = [x.split(None, 1)[1] for x in pool if len(x.split(None, 1)) > 1]
        return [f"{fixed} {x}" for x in difflib.get_close_matches(rest, subs, n=n, cutoff=0.45)]
    if fixed:
        return pool[:n]
    return difflib.get_close_matches(c, COMMON, n=n, cutoff=0.6)


def looks_like_typo(text: str) -> bool:
    return ("Unrecognized command" in text) or ("Incomplete command" in text) \
        or ("Ambiguous command" in text)


def parse_caret(text: str, cmd: str):
    """设备用 ^ 指错的位置 → 转成列号，方便告诉用户"错在第几个字符"。"""
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("^"):
            col = ln.index("^") + 1
            seg = cmd[:col - 1]
            return col, seg
    return None, None
