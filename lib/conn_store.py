"""连接清单（SSH / Telnet / 串口）—— 带端口管理的统一底座。

与 devices.toml 的分工：
  * devices.toml      = 「正式设备」（netmiko 用：平台、ESN、钥匙串、tags…）
  * connections.json  = 「连接簿」（给人/网页/AI 的接入清单：协议+地址+端口），可一键升级为正式设备
路径由 lib/paths.py 统一解析；每次写入前自动备份。
"""
import json
import pathlib
import re
import time

# 原来写死 `home() / "netops" / "connections.json"` —— 装到别处就连不上网了。
# 装在哪由 NETDEV_ROOT 决定；文件优先用安装根的软链，没有就用 config/（源码安装）。
from . import paths as _paths

FILE = _paths.cfg("connections.json")
PROTOCOLS = ("ssh", "telnet", "serial")
DEFAULT_PORT = {"ssh": 22, "telnet": 23}


def load() -> list:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save(items: list):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    if FILE.exists():
        _bd = FILE.parent / "backups"          # 备份统一进 backups/，不在根目录散落
        _bd.mkdir(parents=True, exist_ok=True)
        bak = _bd / f"connections.json.bak-{time.strftime('%Y%m%d_%H%M%S')}"
        bak.write_text(FILE.read_text(encoding="utf-8"), encoding="utf-8")
    FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _id(name: str, proto: str, host: str, port) -> str:
    base = name or f"{proto}-{host or 'serial'}-{port or ''}"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-").lower() or "conn"


def add(proto: str, host: str = "", port=None, username: str = "", name: str = "",
        device: str = "", baud="auto", note: str = "", platform: str = "huawei_vrp") -> dict:
    proto = proto.lower()
    if proto not in PROTOCOLS:
        raise ValueError(f"协议只能是 {'/'.join(PROTOCOLS)}")
    if proto == "serial":
        if not device:
            from . import host as _host
            _eg = "COM3" if _host.IS_WIN else "/dev/cu.usbserial-XXXX"
            raise ValueError(f"串口需要 --device（如 {_eg}）")
        # baud 支持 "auto"（与 devices.toml 模板一致）：写死 9600 会让设备实际
        # 115200 时接入先自检再切档。数字照旧存 int。
        _baud = str(baud or "auto").strip().lower()
        _baud = "auto" if _baud in ("", "auto") else int(_baud)
        entry = {"protocol": "serial", "device": device, "baud": _baud}
        addr = f"{device}@{_baud}"
    else:
        if not host:
            raise ValueError(f"{proto} 需要主机地址")
        port = int(port or DEFAULT_PORT[proto])
        entry = {"protocol": proto, "host": host, "port": port, "username": username or ""}
        addr = f"{username + '@' if username else ''}{host}:{port}"
    items = load()
    _new_id = _id(name, proto, host, port)      # 先算好 id 再引用（原来直接引用 entry["id"]，而它还没写进去）
    entry.update({"id": _new_id, "name": name or _new_id,
                  "address": addr, "note": note, "platform": platform,
                  "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if any(x["id"] == entry["id"] for x in items):
        raise ValueError(f"已存在同名连接: {entry['id']}（先 rm 或换个 --name）")
    items.append(entry)
    save(items)
    return entry


def remove(key: str) -> dict:
    items = load()
    hit = [x for x in items if key in (x["id"], x.get("name"))]
    if not hit:
        raise ValueError(f"找不到连接: {key}")
    items = [x for x in items if x is not hit[0]]
    save(items)
    return hit[0]


def find(key: str):
    for x in load():
        if key in (x["id"], x.get("name")):
            return x
    return None


def uri(entry: dict) -> str:
    """转成 netdev 统一 URI（可直接喂给 netdev shell/run/screen-send…）"""
    if entry["protocol"] == "serial":
        return f"serial:{entry['device']}@{entry.get('baud') or 'auto'}"
    u = entry.get("username")
    return f"{entry['protocol']}://{u + '@' if u else ''}{entry['host']}:{entry['port']}"
