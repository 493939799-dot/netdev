#!/usr/bin/env python3
"""SSH 透传桥 —— 与 serial_bridge / telnet_bridge 同款体验，给「人机同屏」用。

为什么 SSH 也要一个桥（而不是直接 `exec ssh`）：
  * 退格适配：浏览器/macOS 终端的退格发 0x7F(DEL)，网络设备（华为 VRP 等）普遍只认
    0x08(BS)，收到 0x7F 只会蜂鸣 —— 这里按 devices.toml 的 backspace 设置翻译，
    与串口 / telnet 桥完全一致（auto 会像串口那样"直接问设备"，结果缓存到 state/keys.json）
  * 全程留档：屏幕留档写 live/<设备>.screen.log（原来 SSH 通道没有任何屏幕留档）
  * IP 地址橙色高亮（与其它桥一致）
  * 窗口尺寸透传：把 tmux 窗格的真实行列数同步给 ssh（设备分页/全屏显示才正确）
  * 退出方式统一：Ctrl+]

用法:  ssh_bridge.py <logfile> -- <ssh 命令...>
环境:  NETDEV_BACKSPACE=auto|bs|del|pass（默认 auto）
       NETDEV_DEVICE=<设备名>（探测结果按设备缓存）
"""
from __future__ import annotations

import os
import pathlib
import queue
import re
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import host  # noqa: E402

if not host.IS_WIN:
    import fcntl
    import pty
    import select
    import termios
    import tty

# ── 参数：<logfile> -- <ssh ...>
argv = sys.argv[1:]
if "--" not in argv:
    sys.stderr.write("用法: ssh_bridge.py <logfile> -- <ssh 命令...>\n")
    raise SystemExit(2)
i = argv.index("--")
logfile = argv[i - 1] if i >= 1 else None
child_argv = argv[i + 1:]
if not child_argv:
    raise SystemExit("没有给 ssh 命令")

# ── SSH 保活（2026-09-26 加）────────────────────────────────────────────
#   背景：设备侧 vty 有 idle-timeout（华为默认 20 分钟，客户可能更短）。
#        闲置超时后【设备主动关闭 TCP】→ SSH 桥收到 EOF 退出 →
#        tmux 窗格留下空壳（侧栏仍显示"窗格在线"）→ 用户再点就复用了空壳，
#        屏上什么都没有（用户反馈："点 SSH 不能自动连接"）。
#   修法：让 SSH 自己定期发心跳，设备就不会认为你闲置。
#        ServerAliveInterval=30  每 30 秒发一次
#        ServerAliveCountMax=3   连续 3 次没回应才断开
#   （可关：设 NETDEV_SSH_KEEPALIVE=0）
import os as _os2
if _os2.environ.get("NETDEV_SSH_KEEPALIVE", "1") != "0":
    if child_argv and _os2.path.basename(child_argv[0]).startswith("ssh"):
        # 应用层心跳（防设备按"会话无输入"踢人）
        # + TCPKeepAlive（防中间 NAT/防火墙清会话表）—— 都在客户端，不动设备
        _ka = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
               "-o", "TCPKeepAlive=yes"]
        # 插在 "ssh" 之后、目标之前
        child_argv = [child_argv[0]] + _ka + child_argv[1:]

BACKSPACE = os.environ.get("NETDEV_BACKSPACE", "auto").lower()
DEVICE_NAME = os.environ.get("NETDEV_DEVICE", "")

# ── IP 高亮 + 输入回显著色（与其它桥共用 lib/colorize；人=蓝/AI=紫/系统=灰）
# ★ 路径按【桥脚本所在仓】推导，不写死 ~/netops（同 serial_bridge 注释）
try:
    from lib import colorize as _cz
    _echo = _cz.EchoPainter(device=DEVICE_NAME)
    _paint_on = _cz.enabled()
except Exception:
    _echo, _paint_on = None, False

# ── 终端应答过滤：丢掉 xterm 等终端模拟器的自动应答 ──
#    （不丢的话会被当成"用户输入"写进设备 → 屏幕被 "1;2c0;276;0c" 这类垃圾污染
#      → netdev 认不出提示符 → 快照/apply 报"没回到提示符"。已实测踩到多次）
try:
    from lib import termfilter as _tf
except Exception:
    _tf = None

log = open(logfile, "ab", buffering=0) if logfile else None
START = time.time()

# ── 自动登录：钥匙串（/环境变量）里有凭据就替你把密码填上（不弹窗、不打印密码）
#    为什么需要：设备端密码必须逐字符敲对，敲错会被 SSH 服务端断开（设备日志 SSH_FAIL），
#    而 netdev 的凭据本来就在钥匙串里（同一条凭据串口/直连都在用）。
AUTOLOGIN = os.environ.get("NETDEV_AUTOLOGIN", "1") != "0"
DEV_PW, PW_SRC = "", ""
if AUTOLOGIN and DEVICE_NAME:
    try:
        from lib import creds as _creds, engine as _engine
        _dev = None
        try:
            _dev = _engine.get_device(DEVICE_NAME)
        except KeyError:
            _dev = None
        if _dev is None:
            # ★ 2026-10-10 修（连接簿临时目标 SSH 也要手输密码）：
            #   临时目标不在 devices.toml，get_device 抛 KeyError → DEV_PW 空
            #   → 自动登录静默失效（与 serial_bridge 同款病、同款修法）：
            #   回退裸 {"name": ...}，creds 按 service "netdev-<名字>" 查凭据文件。
            _dev = {"name": DEVICE_NAME}
        _pw, PW_SRC = _creds.get_password(_dev, allow_popup=False)
        DEV_PW = _pw or ""
    except Exception as _e:
        PW_SRC = f"凭据查询异常({type(_e).__name__})"
PW_PROMPT = re.compile(rb"[Pp]assword:\s*$")
# ★ 2026-10-10 修：OpenSSH 8.5+ 的首见提示是 "(yes/no/[fingerprint])?" ——
#   旧正则 \(yes/no(\[[^\]]*\])?\) 吃不掉 yes/no 后面的 "/" → 匹配失败
#   → 桥不回 yes → 停在指纹确认上，后面的密码自动填根本没机会跑。
YN_PROMPT = re.compile(rb"\(yes/no(?:/\[[^\]]*\])?\)\?\s*$")

# ── 退格模式解析：auto → 先看探测缓存（state/keys.json），没有就按 bs
#    （与 telnet 桥一致：SSH 通道不自己探测。想逐字节问设备就用串口通道的自动探测，
#      或在 devices.toml 里写死 backspace = bs / del / pass）
mode_note = BACKSPACE
if BACKSPACE == "auto":
    _cached = None
    if DEVICE_NAME:
        try:
            from lib import keys as _keys
            _cached = _keys.cached_mode(DEVICE_NAME)
        except Exception:
            _cached = None
    if _cached:
        BACKSPACE, mode_note = _cached, f"用缓存结果 {_cached}（state/keys.json）"
    else:
        BACKSPACE = "bs"
        mode_note = "auto→bs（SSH 通道不自动探测；要改就写 devices.toml 的 backspace）"


def fix_keys(data: bytes) -> bytes:
    if BACKSPACE == "bs":
        return data.replace(b"\x7f", b"\x08")
    if BACKSPACE == "del":
        return data.replace(b"\x08", b"\x7f")
    return data


# 终端能力应答的碎片：tmux 接入新客户端时会问外层的 xterm（DA/标题查询），
# 浏览器回的应答如果没被 tmux 吃掉，就会当成“你敲的字”流进来（ESC 被 tmux 吃了，只剩尾巴），
# 典型如 `;2c`、`0;276;0c` —— 它们被原样转给设备，就出现
# `<Huawei>;2c0;276;0csave vrpcfg.zip → Unrecognized`。
# 收紧：必须带分号（`;2c` / `0;276;0c`），免得把用户真敲的 "c" 之类也丢掉
_TERM_REPLY_FRAG = re.compile(rb"^[\x07\r\n]*[>?]?[0-9]*;[0-9;]*[cnR][\x07\r\n]*$")


def drop_term_reply(data: bytes) -> bytes:
    """若这块输入只是“终端应答碎片”就丢掉（并留一笔日志），不污染设备命令行。"""
    if not data or not data.strip(b"\x07\r\n"):
        return data
    if data.startswith(b"\x1b") and data[-1:] in (b"c", b"R", b"n"):
        return b""
    if _TERM_REPLY_FRAG.match(data):
        return b""
    return data


# ── 密码不落日志（2026-09-27）─────────────────────────────────────────
# 为什么要这个：桥会把用户键入的每个字节记进 live/*.log 作留档，但登录时
#   Password: 后面跟的是【明文密码】—— 实测在 live/<设备名>.screen.log
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


def out(b: bytes):
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


def emit(b: bytes):
    """给屏幕 + 留档（探测回显也走这里，保证不漏字节）。"""
    if not b:
        return
    log_raw(b)
    out(_echo.feed(b) if _paint_on and _echo else b)


# ════════════════════════════════════════════════════════════════════
# Windows 分支：pywinpty 承载 ssh（替代 pty.fork），stdin 泵线程喂
# queue（替代 select），0.2s 节拍；所有着色/脱敏/自动登录逻辑共用。
# POSIX 分支在下面，一行不改。
# ════════════════════════════════════════════════════════════════════
if host.IS_WIN:
    import json as _json

    def _win_rows_cols() -> tuple[int, int]:
        """从 pane 注册表读尺寸（守护写），失败则 40x140。"""
        try:
            if DEVICE_NAME:
                rp = pathlib.Path(__file__).resolve().parent.parent / "state" / "panes" / f"{DEVICE_NAME}.json"
                reg = _json.loads(rp.read_text(encoding="utf-8"))
                sz = str(reg.get("size", ""))
                if "x" in sz:
                    c_, r_ = sz.split("x", 1)
                    return int(r_), int(c_)
        except Exception:
            pass
        return 40, 140

    rows_, cols_ = _win_rows_cols()
    try:
        from winpty import PtyProcess
        ptyproc = PtyProcess.spawn(child_argv, dimensions=(rows_, cols_))
    except Exception as e:
        sys.stderr.write(f"[ssh_bridge] pty 启动失败: {type(e).__name__}: {e}\n")
        raise SystemExit(1)

    # 输入来自守护的喂送通道（本环境 stdin 管道写入被拦截）
    from lib import pane as _pane_mod
    _feed_port = int(os.environ.get("NETDEV_FEED_PORT", "0"))
    if not _feed_port:
        raise SystemExit("[ssh_bridge] 缺少 NETDEV_FEED_PORT")
    q_in, _feed_sock = _pane_mod.feed_client(_feed_port)

    # pywinpty 的 read() 是阻塞的 → 专用泵线程，读到的字节塞进 q_out；
    # b"" 哨兵 = pty 已结束。
    q_out: queue.Queue = queue.Queue()

    def _pty_pump():
        while True:
            try:
                s = ptyproc.read(8192)
            except EOFError:
                q_out.put(b"")
                return
            except Exception:
                q_out.put(b"")
                return
            if s:
                q_out.put(s.encode("utf-8", "replace"))

    threading.Thread(target=_pty_pump, daemon=True).start()

    def _pty_write(b: bytes) -> bool:
        try:
            ptyproc.write(b.decode("utf-8", "replace"))
            return True
        except Exception:
            return False

    _hdr = (f"\r\n[ssh 已接入] {' '.join(child_argv)}\r\n"
            f"[人机同屏会话；退出 Ctrl+]   退格适配={mode_note}   "
            f"IP高亮+输入着色={'开' if _paint_on else '关'}   "
            f"自动登录={'开（'+PW_SRC+'）' if DEV_PW else '关（未找到凭据，请手工输密码）'}   "
            f"日志: {logfile or '未开启'}]\r\n\r\n")
    out(_hdr.encode())
    log_raw(f"\n===== {time.strftime('%F %T')} ssh {' '.join(child_argv)}\n".encode())

    _pending_since = [0.0]
    _wd_last = [0.0]
    injected = 0
    tail = b""
    _dead = False

    def _win_finish():
        try:
            ptyproc.terminate()
        except Exception:
            pass
        try:
            ptyproc.close()
        except Exception:
            pass
        if log:
            try:
                log.write(f"\n[session end {time.time()-START:.0f}s]\n".encode())
                log.close()
            except Exception:
                pass
        out("[ssh 桥已退出]\r\n".encode())

    # ★ 2026-10-07 修（网页终端「输入很卡」，与 serial_bridge 同款全局优化）：
    #   原节拍 0.2s —— 主循环每 200ms 才排空一次输入/输出队列，键击下发与回显
    #   最多各等 0.2s（平均 ~100ms）。POSIX 版用 select(timeout=0.2)，有数据立刻醒；
    #   Windows 版换成队列轮询 + 无条件 sleep(0.2)，等于给每次交互加固定延迟。
    #   降到 10ms 后与串口通道一致（串口实测 median 150ms → 8.7ms）。
    _TICK = 0.01
    try:
        while not _dead:
            _now = time.time()
            # 看门狗（只提醒，不主动断开——与 POSIX 同）
            if _now - _wd_last[0] > 2.0:
                _wd_last[0] = _now
                if _LOGGED_IN[0] and _pending_since[0] and (_now - _pending_since[0]) > 90.0:
                    _pending_since[0] = 0.0
                    if not _WD_WARNED[0]:
                        _WD_WARNED[0] = True
                        out("\r\n[ssh_bridge] ⚠ 已 90 秒没有设备回显 —— 可能连接僵死，"
                            "建议重连（netdev shell <设备> --restart 或界面上的「重连」）\r\n".encode())
            # stdin 队列
            while True:
                try:
                    d = q_in.get_nowait()
                except queue.Empty:
                    break
                if not d:
                    _dead = True
                    break
                if b"\x1d" in d:                     # Ctrl+]
                    _dead = True
                    break
                d = drop_term_reply(d)
                if not d:
                    _pending_since[0] = time.time()
                    log_raw(b"[drop] terminal reply fragment\n")
                    continue
                try:
                    sd = fix_keys(d)
                    if _tf is not None:
                        _n = _tf.dropped_count(sd)
                        sd = _tf.strip(sd)
                        if _n:
                            log_raw(("[丢弃终端应答 %d 字节]\n" % _n).encode())
                    _pty_write(sd)
                    if _paint_on and _echo:
                        _echo.expect(sd)
                except OSError as e:
                    out(f"\r\n[ssh] 发送失败: {e}\r\n".encode())
                    _dead = True
                    break
                _pending_since[0] = time.time()
                log_raw(_input_for_log(d, "[input] ").encode())
            # pty 输出：排空队列
            got = False
            while True:
                try:
                    chunk = q_out.get_nowait()
                except queue.Empty:
                    break
                got = True
                if not chunk:
                    out("\r\n[ssh] 远端已断开\r\n".encode())
                    _dead = True
                    break
                _pending_since[0] = 0.0
                emit(chunk)
                tail = (tail + chunk)[-256:]
            if not got:
                # 残片着色与自动登录互不依赖（着色关掉时自动登录也必须生效）
                if _paint_on and _echo and _echo.pending():
                    emit(_echo.flush())
                if injected < 3 and YN_PROMPT.search(tail):
                    _pty_write(b"yes\r")
                    injected += 1
                    tail = b""
                    msg = "\r\n[主机密钥首见：已自动回 yes（以后不再问）]\r\n"
                    out(msg.encode())
                    log_raw(msg.encode())
                elif DEV_PW and injected < 3 and PW_PROMPT.search(tail):
                    time.sleep(0.15)
                    _pty_write(DEV_PW.encode() + b"\r")
                    injected += 1
                    tail = b""
                    msg = f"\r\n[已用{PW_SRC}凭据自动登录；要手工输密码就用 NETDEV_AUTOLOGIN=0]\r\n"
                    out(msg.encode())
                    log_raw(msg.encode())
            if not ptyproc.isalive():
                out("\r\n[ssh] 远端已断开\r\n".encode())
                _dead = True
            time.sleep(_TICK)
    except Exception as e:
        import traceback
        out(f"\r\n[ssh] 异常: {type(e).__name__}: {e}\r\n".encode())
        out(traceback.format_exc().encode())
    finally:
        _win_finish()
    raise SystemExit(0)

# ── 起 ssh 子进程（给它一个 pty，ssh 才认为是交互终端）
pid, fd = pty.fork()
if pid == 0:                                   # 子进程：执行 ssh
    try:
        os.execvp(child_argv[0], child_argv)
    except Exception as e:
        os.write(2, f"exec {child_argv[0]} 失败: {e}\n".encode())
        os._exit(127)


def copy_winsize(src: int, dst: int):
    try:
        sz = fcntl.ioctl(src, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst, termios.TIOCSWINSZ, sz)
    except Exception:
        pass


stdin_fd = sys.stdin.fileno()
copy_winsize(stdin_fd, fd)                     # 首次同步窗口尺寸
old_attr = None
if os.isatty(stdin_fd):
    old_attr = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)


def _winch(_sig, _frm):
    copy_winsize(stdin_fd, fd)


try:
    signal.signal(signal.SIGWINCH, _winch)
except Exception:
    pass


class _PtyLike:                                       # noqa: F401  （保留给以后的探测用）
    """把 pty fd 包成 keys.py 要的"串口对象"（fileno/read/write）。"""

    def __init__(self, f):
        self.fd = f

    def fileno(self):
        return self.fd

    def write(self, b: bytes):
        os.write(self.fd, b)

    def read(self, n: int = 8192) -> bytes:
        try:
            return os.read(self.fd, n)
        except OSError:
            return b""


hdr = (f"\r\n[ssh 已接入] {' '.join(child_argv)}\r\n"
       f"[人机同屏会话；退出 Ctrl+]   退格适配={mode_note}   "
       f"IP高亮+输入着色={'开' if _paint_on else '关'}   "
       f"自动登录={'开（'+PW_SRC+'）' if DEV_PW else '关（未找到凭据，请手工输密码）'}   "
       f"日志: {logfile or '未开启'}]\r\n\r\n")
out(hdr.encode())
log_raw(f"\n===== {time.strftime('%F %T')} ssh {' '.join(child_argv)}\n".encode())

tail = b""              # 最近输出（认登录提示符用）
# ── 看门狗（2026-09-27）：连接【静默僵死】自愈 ─────────────────────
# 与串口不同：TCP 断了【无法原地重连】（要重新握手 + 登录），所以策略是
# 【主动退出】—— 退出后宿主（netdev attach / UI）会检测到窗格已死并重建会话。
# 判据：发出输入后 45 秒零字节回显（45s 是给 display current-configuration 留的余量）。
_pending_since = [0.0]     # 最后一次发出输入的时刻
_wd_last = [0.0]           # 上次检查时刻


injected = 0           # 已经自动填过几次（防死循环）

try:
    while True:
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

                        out("\r\n[ssh_bridge] ⚠ 已 90 秒没有设备回显 —— 可能连接僵死，"

                            "建议重连（netdev shell <设备> --restart 或界面上的「重连」）\r\n".encode())
            r, _, _ = select.select([stdin_fd, fd], [], [], 0.2)
        except (OSError, ValueError) as e:
            out(f"\r\n[ssh] select 失败: {type(e).__name__}\r\n".encode())
            break
        if stdin_fd in r:
            data = os.read(stdin_fd, 1024)
            if not data:
                break
            if b"\x1d" in data:                # Ctrl+] 退出
                break
            data = drop_term_reply(data)        # 丢掉终端应答碎片，别让它变成“命令前缀”
            if not data:
                _pending_since[0] = time.time()   # 发了东西 → 等回音
                log_raw(b"[drop] terminal reply fragment\n")
                continue
            try:
                _d = fix_keys(data)
                if _tf is not None:                # 丢掉终端模拟器的自动应答
                    _n = _tf.dropped_count(_d); _d = _tf.strip(_d)
                    if _n: log_raw(("[丢弃终端应答 %d 字节]\n" % _n).encode())
                os.write(fd, _d)
                if _paint_on and _echo:
                    _echo.expect(_d)           # 登记期待回显 → 输入着色
            except OSError as e:
                out(f"\r\n[ssh] 发送失败: {e}\r\n".encode())
                break
            log_raw(_input_for_log(data, "[input] ").encode())
        if fd in r:
            _pending_since[0] = 0.0   # 有回音 → 待决解除
            try:
                data = os.read(fd, 8192)
            except OSError:                    # 子进程退出时 pty 会 EIO
                data = b""
            if not data:
                out("\r\n[ssh] 远端已断开\r\n".encode())
                break
            emit(data)
            tail = (tail + data)[-256:]
        else:
            # ★ 空闲兜底：扣住的 IP 残片/回显收色要吐出来
            #   （自动登录不许依赖着色开关——着色关掉时也必须能填密码）
            if _paint_on and _echo and _echo.pending():
                emit(_echo.flush())        # （大输出块尾的 IP 不染问题）
            # ── 自动登录：认出 ssh 的提示符就替你把密码填上（不打印密码）
            if injected < 3 and YN_PROMPT.search(tail):
                os.write(fd, b"yes\r")
                injected += 1
                tail = b""
                msg = "\r\n[主机密钥首见：已自动回 yes（以后不再问）]\r\n"
                out(msg.encode())
                log_raw(msg.encode())
            elif DEV_PW and injected < 3 and PW_PROMPT.search(tail):
                time.sleep(0.15)
                os.write(fd, DEV_PW.encode() + b"\r")
                injected += 1
                tail = b""
                msg = (f"\r\n[已用{PW_SRC}凭据自动登录；要手工输密码就用 NETDEV_AUTOLOGIN=0]\r\n")
                out(msg.encode())
                log_raw(msg.encode())
except Exception as e:
    import traceback
    out(f"\r\n[ssh] 异常: {type(e).__name__}: {e}\r\n".encode())
    out(traceback.format_exc().encode())
finally:
    if old_attr is not None:
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attr)
        except Exception:
            pass
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, os.WNOHANG)
    except Exception:
        pass
    if log:
        try:
            log.write(f"\n[session end {time.time()-START:.0f}s]\n".encode())
            log.close()
        except Exception:
            pass
    out("[ssh 桥已退出]\r\n".encode())
