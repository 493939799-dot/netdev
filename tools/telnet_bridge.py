#!/usr/bin/env python3
"""Telnet raw 透传桥 —— 与 serial_bridge 同款体验，给「人机同屏」用。

特点：
  * 自带 IAC 协商（默认拒绝所有选项，兼容绝大多数网络设备）
  * 真·raw：不解释按键、不加前缀键；你敲的字节直接进设备
  * 全程留档：屏幕输出写日志（剥离 IAC 控制序列）
  * 退出：Ctrl+]
  * IP 地址橙色高亮（与串口桥一致）

用法: telnet_bridge.py <host> [port] [logfile]
"""
import os
import pathlib
import re
import select
import socket
import sys
import pathlib as _pathlib
import time as _time

# ── 自动登录（2026-09-26 补：原来 telnet 通道完全没有自动登录）──
#   认出 Username:/Login:/Password: 提示就用凭据填上；不打印密码、不写日志。
_AUTOLOGIN = {"sent_user": False}
try:
    sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
    from lib import creds as _creds
except Exception:
    _creds = None

_DEV = None
# ── 设备名从哪来（2026-09-27 修）────────────────────────────────────
# ⚠ 原来只读 NETDEV_DEVICE_JSON，但 netdev_cli.py 实际传的是 NETDEV_DEVICE
#   （见 netdev_cli.py 的 exec env NETDEV_DEVICE=<name>）。名字对不上 →
#   _DEV 落到"从 argv[1] 猜"的兜底分支 → 猜出的是【IP】而不是【设备名】→
#   按设备名存的凭据取不到 → 连接簿里明明存了用户名密码，却仍停在登录界面。
#   现在优先用 NETDEV_DEVICE（和 ssh_bridge.py 保持一致），JSON 仅作后备。
_nm = (os.environ.get("NETDEV_DEVICE") or "").strip()
if _nm:
    _DEV = {"name": _nm}
if _DEV is None:
    try:
        import json as _json
        _j = _json.loads(os.environ.get("NETDEV_DEVICE_JSON") or "{}")
        _DEV = _j or None
    except Exception:
        _DEV = None
if _DEV is None:
    # 最后兜底：从命令行猜（argv[1] 是 host/uri，只能猜个 IP，取凭据多半取不到）
    try:
        _DEV = {"name": (sys.argv[1].split("@")[-1].split(":")[0] if len(sys.argv) > 1 else "telnet")}
    except Exception:
        _DEV = {"name": "telnet"}


def maybe_autologin(data: bytes, sock=None) -> None:
    """看到登录提示就填凭据（只用一次，避免重复填）。

    ⚠ 2026-09-27 修：原来这里写的是 os.write(sys.stdout.fileno(), ...) ——
      那是【把用户名/密码打到屏幕上】，根本没发给设备！
      于是"连接簿里存了凭据"却仍然停在登录界面，用户得手工再输一遍。
      （ssh 桥用的是 os.write(fd, ...)，fd 是子进程 pty，那才是发给设备。）
      现在改成往 telnet 的 socket 里发。
    """
    if not _creds or not data or sock is None:
        return
    low = data.lower()
    try:
        if (b"username" in low or b"login:" in low or b"user name" in low) and not _AUTOLOGIN["sent_user"]:
            u, _src = _creds.get_username(_DEV, allow_popup=False)
            if u:
                sock.sendall((u + "\r\n").encode())
                _AUTOLOGIN["sent_user"] = True
                log_raw(("[自动登录] 已发送用户名（不打印、不落日志）\n").encode())
                _time.sleep(0.3)
                return
        if b"password" in low or b"passwd" in low:
            pw, _src = _creds.get_password(_DEV, allow_popup=False)
            if pw:
                sock.sendall((pw + "\r\n").encode())
                log_raw(("[自动登录] 已发送密码（来源：credfile，不打印、不落日志）\n").encode())
                _time.sleep(0.3)
    except Exception:
        pass

import termios
import time
import tty

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 23
logfile = sys.argv[3] if len(sys.argv) > 3 else None

sys.path.insert(0, os.path.expanduser("~/netops"))
try:
    from lib import colorize as _cz
    _painter = _cz.BytePainter()
    _paint_on = _cz.enabled()
except Exception:
    _painter, _paint_on = None, False

# ── 终端应答过滤：丢掉 xterm 等终端模拟器的自动应答 ──
#    （不丢的话会被当成"用户输入"写进设备 → 屏幕被 "1;2c0;276;0c" 这类垃圾污染
#      → netdev 认不出提示符 → 快照/apply 报"没回到提示符"。已实测踩到多次）
try:
    from lib import termfilter as _tf
except Exception:
    _tf = None

log = open(logfile, "ab", buffering=0) if logfile else None
BACKSPACE = os.environ.get("NETDEV_BACKSPACE", "bs").lower()
START = time.time()

sock = socket.create_connection((host, port), timeout=8)

# ── TCP 保活（2026-09-26 加）────────────────────────────────────────────
#   原则：**绝不改客户设备配置**，所有健壮性都做在桥侧。
#   设备侧 vty 有 idle-timeout，闲置超时后设备会关掉 TCP 连接；
#   中间的网络设备/NAT 也可能因为"长时间没流量"清掉会话表。
#   这里开 TCP keepalive（操作系统层，每 30 秒探测一次），
#   至少能让链路上的中间设备保持连接，不需要设备做任何配合。
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # macOS 用 TCP_KEEPALIVE（空闲多久开始探测），Linux 是 TCP_KEEPIDLE
    for _opt, _val in (("TCP_KEEPALIVE", 30), ("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10)):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, _opt), _val)
        except Exception:
            pass
except Exception:
    pass
sock.setblocking(False)

fd = sys.stdin.fileno()
old_attr = None
if os.isatty(fd):
    old_attr = termios.tcgetattr(fd)
    tty.setraw(fd)


# ── 密码不落日志（2026-09-27）─────────────────────────────────────────
# 为什么要这个：桥会把用户键入的每个字节记进 live/*.log 作留档，但登录时
#   Password: 后面跟的是【明文密码】—— 实测在 live/serial-huawei.screen.log
#   里能直接读到密码。这违反「密码绝不写入文件/日志」的硬约定。
# 做法：设备输出里出现过 Password: 提示后，紧跟的那一次输入只记掩码。
#   设备登录提示只出现一次，且时间上紧邻，用「最近 30 秒内见过提示」判定足够稳。
_PW_TAIL = [""]
_PW_AT = [0.0]


# 看门狗只在【登录完成后】才生效（2026-09-27 修）
#   为什么：登录期间设备【不回显输入】（Username:/Password: 阶段屏幕是死的），
#   而看门狗判据是"发了输入但 45 秒零回显" → 把正常登录误判成僵死 →
#   实测直接把刚接入的 telnet 会话杀了（Pane is dead）。
# 判据：设备输出里出现过提示符（<xxx> / [xxx] / xxx# / xxx>）才算进了系统。
_LOGGED_IN = [False]
_WD_WARNED = [False]   # 僵死提醒只说一次，别刷屏
_PROMPT_RE = re.compile(rb"[<\[][A-Za-z][\w\-./]{0,40}[>\]]|\n[\w\-./@]{1,40}[>#]\s*$")
def _note_logged_in(chunk):
    if _LOGGED_IN[0]:
        return
    if _PROMPT_RE.search(chunk):
        _LOGGED_IN[0] = True


def _note_output(chunk):
    """设备输出经过时调用：记住最近有没有出现 Password: 提示。"""
    try:
        s = chunk.decode("utf-8", "replace")
    except Exception:
        return
    _PW_TAIL[0] = (_PW_TAIL[0] + s)[-400:]
    last = _PW_TAIL[0].replace("\r", "").split("\n")[-1]
    if re.search(r"(?i)\b(?:password|passwd)\s*[:：]", last) or re.search(r"(?i)\b(?:password|passwd)\s*[:：]\s*$", last):
        _PW_AT[0] = time.time()


_CMD_PW_PATS = [
    # 华为/H3C：local-user xxx password [irreversible-]cipher|simple <值>
    (re.compile(r"(?i)(\b(?:password|passwd|secret)\s+(?:irreversible-cipher|cipher|simple)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:password|passwd|secret)\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(set\s+authentication\s+password\s+(?:simple|plain|cipher)?\s*)\S+"), r"\1[REDACTED]"),
    # SNMP community string 等同密码（能读甚至能写设备）
    (re.compile(r"(?i)(snmp-agent\s+community\s+(?:read|write)\s+)\S+"), r"\1[REDACTED]"),
    # 思科
    (re.compile(r"(?i)(\busername\s+\S+\s+(?:password|secret)\s+(?:\d+\s+)?)\S+"), r"\1[REDACTED]"),
]


def _input_for_log(data, style="[input] "):
    """把用户输入转成日志行；密码一律不落明文。

    两类都要管：
      ① 登录交互 —— 出现 Password: 提示后紧跟的那次输入（只掩一次）
      ② 配置命令 —— 命令里本身就带明文密码，例如
         local-user admin password irreversible-cipher <明文>
         （2026-09-27 实测：live/*.screen.log 里能直接读到这种明文；
          桥的日志不走 lib/mirror 的脱敏，所以必须在这里自己处理）
    """
    if _PW_AT[0] and (time.time() - _PW_AT[0]) < 30:
        _PW_AT[0] = 0.0                      # 只用一次
        return style + "******（密码，不写日志）\n"
    txt = data.replace(b"\r", b"\\r").decode("utf-8", "replace")
    for _pat, _rep in _CMD_PW_PATS:
        txt = _pat.sub(_rep, txt)
    return style + txt + "\n"


def screen(b: bytes):
    _note_logged_in(b)
    _note_output(b)      # 记录是否出现密码提示（密码不进日志）
    try:
        os.write(sys.stdout.fileno(), b)
    except OSError:
        pass


def log_raw(b: bytes):
    if log:
        try:
            log.write(b)
        except (ValueError, OSError):
            pass


def strip_iac(data: bytes) -> bytes:
    """剥掉服务端发来的 IAC 序列；顺带回应协商（全部拒绝）。"""
    out = bytearray()
    reply = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c != IAC:
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd == IAC:                     # 转义的 0xFF
            out.append(IAC)
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            if i + 2 >= n:
                break
            opt = data[i + 2]
            if cmd == DO:
                reply += bytes([IAC, WONT, opt])
            elif cmd == WILL:
                reply += bytes([IAC, DONT, opt])
            i += 3
        elif cmd == SB:                    # 子协商：跳到 IAC SE
            j = data.find(bytes([IAC, SE]), i)
            i = (j + 2) if j != -1 else n
        else:
            i += 2
    if reply:
        try:
            sock.sendall(bytes(reply))
        except OSError:
            pass
    return bytes(out)


def fix_keys(data: bytes) -> bytes:
    if BACKSPACE == "bs":
        return data.replace(b"\x7f", b"\x08")
    if BACKSPACE == "del":
        return data.replace(b"\x08", b"\x7f")
    return data


screen(f"\r\n[telnet 已连接] {host}:{port}\r\n"
       f"[人机同屏会话；退出 Ctrl+]   IP高亮={'开' if _paint_on else '关'}   日志: {logfile or '未开启'}]\r\n\r\n".encode())

last_data = time.time()
try:
    # ── 保活心跳（2026-09-26 加，纯桥侧、不动设备配置）──────────────────
    #   为什么需要：设备侧 vty 有 idle-timeout，按"多久没收到你的输入"算；
    #   闲置超时后设备会关掉连接 → 桥退出 → 窗格留空壳
    #   （用户反馈过："点 SSH/Telnet 不能自动连接"）。
    #   修法：定期发一个【无痕输入】——
    #     ① IAC NOP（telnet 协议层保活）
    #     ② "退格 + 空格 + 退格"：设备认为"有输入"（不会超时踢人），
    #        屏幕上光标右移一格又退回来，几乎看不出来。
    #   间隔可用 NETDEV_TELNET_KEEPALIVE 调（秒，默认 240；0 = 关）。
    import os as _os3
    try:
        _ka_interval = int(_os3.environ.get("NETDEV_TELNET_KEEPALIVE", "240"))
    except Exception:
        _ka_interval = 240
    _last_ka = time.time()

    # ── 看门狗（2026-09-27）：连接【静默僵死】自愈 ─────────────────────
    # 与串口不同：TCP 断了【无法原地重连】（要重新握手 + 登录），所以策略是
    # 【主动退出】—— 退出后宿主（netdev attach / UI）会检测到窗格已死并重建会话。
    # 判据：发出输入后 45 秒零字节回显（45s 是给 display current-configuration 留的余量）。
    _pending_since = [0.0]     # 最后一次发出输入的时刻
    _wd_last = [0.0]           # 上次检查时刻


    while True:
        # 到点就发一次保活
        if _ka_interval > 0 and (time.time() - _last_ka) > _ka_interval:
            _last_ka = time.time()
            try:
                sock.sendall(bytes([255, 241, 255, 242]))   # IAC NOP + IAC DM
            except Exception:
                pass
            try:
                sock.sendall(b"\x08 \x08")                  # 退格 空格 退格（无痕）
            except Exception:
                pass
        try:
            # 看门狗：发过输入但长时间零回显 → 连接疑似僵死，退出让宿主重建
            _wd_now = time.time()
            if _wd_now - _wd_last[0] > 2.0:
                _wd_last[0] = _wd_now
                if _LOGGED_IN[0] and _pending_since[0] and (_wd_now - _pending_since[0]) > 90.0:

                    _pending_since[0] = 0.0

                    # ⚠ 2026-09-27 改为【只提醒，不主动断开】：

                    #   之前这里是 break（退出会话让宿主重建），但实测"设备回显慢/不回显"

                    #   有好几种原因（设备忙、分页等待、登录阶段），自动断开反而把正常会话杀了。

                    #   现在只提示一句，由人决定要不要重连（或点界面上的「重连」按钮）。

                    if not _WD_WARNED[0]:

                        _WD_WARNED[0] = True

                        screen("\r\n[telnet_bridge] ⚠ 已 90 秒没有设备回显 —— 可能连接僵死，"

                            "建议重连（netdev shell <设备> --restart 或界面上的「重连」）\r\n".encode())
            r, _, _ = select.select([fd, sock], [], [], 0.2)
        except (OSError, ValueError) as e:
            screen(f"\r\n[telnet] select 失败: {type(e).__name__}\r\n".encode())
            break
        if fd in r:
            data = os.read(fd, 1024)
            if not data:
                # 原来这里是静默 break —— 实测会话会在 2 分钟左右无声消失，
                # 屏上什么都不留，排查时无从下手。补一条说明。
                screen(("\r\n[telnet] 输入通道已关闭（窗格被 detach / 宿主收回 stdin）"
                        "，桥退出。重连：netdev shell <设备> --restart，或点界面上的「重连」。\r\n").encode())
                break
            _pending_since[0] = time.time()   # 发了东西 → 等回音（看门狗用）
            if b"\x1d" in data:            # Ctrl+]
                break
            try:
                _d = fix_keys(data)
                if _tf is not None:                # 丢掉终端模拟器的自动应答
                    _n = _tf.dropped_count(_d); _d = _tf.strip(_d)
                    if _n: log_raw(("[丢弃终端应答 %d 字节]\n" % _n).encode())
                sock.sendall(_d)
            except OSError as e:
                screen(f"\r\n[telnet] 发送失败: {e}\r\n".encode())
                break
            log_raw(_input_for_log(data, "[input] ").encode())
        if sock in r:
            try:
                data = sock.recv(8192)
            except BlockingIOError:
                continue
                _pending_since[0] = 0.0   # 有回音 → 待决解除
            except OSError:
                break
            if not data:
                screen("\r\n[telnet] 远端已关闭连接\r\n".encode())
                break
            clean = strip_iac(data)
            if clean:
                last_data = time.time()
                log_raw(bytes(clean))
                screen(_painter.feed(clean) if _paint_on else clean)
                # 看到登录提示就自动填凭据（telnet 通道原来完全没有这一步）
                maybe_autologin(bytes(clean), sock)
        elif _paint_on and _painter and _painter.pending() and time.time() - last_data > 0.15:
            screen(_painter.flush())
except Exception as e:
    import traceback
    screen(f"\r\n[telnet] 异常: {type(e).__name__}: {e}\r\n".encode())
    screen(traceback.format_exc().encode())
finally:
    if old_attr is not None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass
    if log:
        try:
            log.write(f"\n[session end {time.time()-START:.0f}s]\n".encode())
            log.close()
        except Exception:
            pass
    screen("\r\n[telnet 已断开]\r\n".encode())
