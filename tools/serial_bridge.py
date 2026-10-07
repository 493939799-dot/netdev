#!/usr/bin/env python3
"""串口 raw 透传桥 —— 给「人机同屏」用。

特点：
  * 真·raw：不解释任何按键、不注入任何命令、不加前缀键（对比 screen 的 Ctrl+A 冲突）
  * 双向 pump：你在窗口里敲的字 → 设备；设备的回显 → 你的屏幕
  * 全程留档：所有字节同时写入日志文件（人和 AI 的操作都在里面）
  * 退出：Ctrl+]

用法: serial_bridge.py <串口> <波特率> [日志文件]
"""
import os
import pathlib
import queue
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import host  # noqa: E402

if not host.IS_WIN:
    import select
    import termios
    import tty

import serial
import signal

# 退出闸：收到 HUP/TERM 或发现"窗格已被销毁"时，干净地退出并释放串口。
# 教训：窗格若被强杀（tmux server 被 SIGKILL 等），进程会被 launchd 收养（PPID=1）
# 却继续握着串口 → 后续接入要么打不开、要么互相抢字节把对方打崩。
_exit_now = [False]


def _on_signal(signum, _frame):
    _exit_now[0] = True


for _sig in ("SIGHUP", "SIGTERM", "SIGINT"):
    try:
        signal.signal(getattr(signal, _sig), _on_signal)
    except Exception:
        pass

port = sys.argv[1]
# 波特率：数字 or "auto"
#   ★ 2026-09-29 修：原来这里是 int(sys.argv[2]) —— 只认数字，传 "auto" 会直接 ValueError 崩掉。
#   于是 auto 完全依赖上游（resolve_target 先探测再替换成数字）才成立；
#   而那个探测【失败时会静默 fallback 到 9600】，用户看到的就是"连上了但屏上空白"。
#   现在桥自己也认 auto：上游给了数字就用数字，给了 auto 就自己扫一遍候选。
_baud_arg = (sys.argv[2] if len(sys.argv) > 2 else "9600").strip().lower()
if _baud_arg in ("auto", "", "0"):
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from lib import engine as _eng
        _b, _ev = _eng.probe_serial_baud(port)
        baud = _b or 115200
    except Exception:
        baud = 115200
else:
    try:
        baud = int(_baud_arg)
    except ValueError:
        baud = 115200
logfile = sys.argv[3] if len(sys.argv) > 3 else None

ser = serial.Serial(port, baud, timeout=0, bytesize=8, parity="N",
                    stopbits=1, rtscts=False, xonxoff=False)
fd = sys.stdin.fileno()
old_attr = None
if host.IS_WIN:
    # Windows：被 pane-daemon 以管道拉起，无需（也没有）termios/raw 设置
    if not os.isatty(fd):
        sys.stderr.write("[serial_bridge] 提示：stdin 管道模式（pane-daemon 宿主）\n")
        sys.stderr.flush()
elif os.isatty(fd):
    old_attr = termios.tcgetattr(fd)
    tty.setraw(fd)
else:
    sys.stderr.write("[serial_bridge] 警告：stdin 不是终端，按管道模式运行\n")
    sys.stderr.flush()

log = open(logfile, "ab", buffering=0) if logfile else None

# 退格键适配：macOS 终端退格键发 0x7F(DEL)，很多网络设备只认 0x08(BS)
#   auto（默认）= 启动时自动探测；bs = 0x7F→0x08；del = 0x08→0x7F；pass = 原样透传
BACKSPACE = os.environ.get("SERIAL_BACKSPACE", "auto").lower()
DEVICE_NAME = os.environ.get("SERIAL_DEVICE", "")

# ── 自动探测退格键模式（只问设备，不猜；不回车、探测完清掉痕迹）
if BACKSPACE == "auto":
    sys.path.insert(0, os.path.expanduser("~/netops"))
    try:
        from lib import keys as _keys
        _cached = _keys.cached_mode(DEVICE_NAME) if DEVICE_NAME else None
        if _cached:
            BACKSPACE, mode_note = _cached, f"用缓存结果 {_cached}"
        else:
            _mode, _ev = _keys.detect(ser)
            BACKSPACE = _mode if _mode != "unknown" else "bs"
            if DEVICE_NAME:
                _keys.remember(DEVICE_NAME, BACKSPACE, _ev, port)
            mode_note = f"自动探测 {BACKSPACE}（{_ev}）"
    except Exception as _e:
        BACKSPACE, mode_note = "bs", f"探测异常({type(_e).__name__})，回退 bs"
else:
    mode_note = BACKSPACE


def fix_keys(data: bytes) -> bytes:
    if BACKSPACE == "bs":
        return data.replace(b"\x7f", b"\x08")
    if BACKSPACE == "del":
        return data.replace(b"\x08", b"\x7f")
    return data


# IP 橙色高亮 + 输入回显著色（人=蓝/AI=紫/系统=灰；日志仍保留原始字节）
# ★ 路径按【桥脚本所在仓】推导，不再写死 ~/netops —— 否则开发仓的桥
#   会加载到安装副本的旧 lib，"改了不生效"还查不出原因（2026-10-04 踩）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from lib import colorize as _cz
    _echo = _cz.EchoPainter(device=DEVICE_NAME)
    _paint_on = _cz.enabled()
except Exception:
    _cz = None
    _echo = None
    _paint_on = False

# ── 终端应答过滤：丢掉 xterm 等终端模拟器的自动应答 ──
#    （不丢的话会被当成"用户输入"写进设备 → 屏幕被 "1;2c0;276;0c" 这类垃圾污染
#      → netdev 认不出提示符 → 快照/apply 报"没回到提示符"。已实测踩到多次）
try:
    from lib import termfilter as _tf
except Exception:
    _tf = None

# ── 自动登录（与 ssh 桥对齐）──────────────────────────────────────────────
#    设备停在登录提示时，用钥匙串里的凭据替你填。
#    关键：密码是"桥主动写入"串口的，而日志只记录从 stdin 读到的人类输入
#          ⇒ 密码既不打印、也不落镜像日志。关掉：NETDEV_AUTOLOGIN=0
AUTOLOGIN = os.environ.get("NETDEV_AUTOLOGIN", "1") != "0"
DEV_USER, DEV_PW, PW_SRC = "", "", ""
if AUTOLOGIN and DEVICE_NAME:
    try:
        from lib import creds as _creds, engine as _engine
        _dev = _engine.get_device(DEVICE_NAME)
        if _dev:
            _u, _ = _creds.get_username(_dev, allow_popup=False)
            _pw, PW_SRC = _creds.get_password(_dev, allow_popup=False)
            DEV_USER, DEV_PW = (_u or "").strip(), (_pw or "")
    except Exception as _e:
        PW_SRC = f"凭据查询异常({type(_e).__name__})"

_USER_PROMPT = re.compile(rb"[Uu]sername:\s*$")
_PW_PROMPT = re.compile(rb"[Pp]assword:\s*$")
_LOGIN = {"stage": 0}          # 0=待用户名 1=已发用户名待密码 2=已完成


def maybe_autologin(data: bytes) -> None:
    """看到登录提示就自动填（只看数据尾部，避免翻旧账）。"""
    if not DEV_PW:
        return
    tail = data[-240:]
    st = _LOGIN["stage"]
    if st == 2:
        if not _USER_PROMPT.search(tail):
            return
        _LOGIN["stage"] = st = 0          # 又回到登录提示 → 允许再试一次
    if st == 0 and _USER_PROMPT.search(tail):
        if not DEV_USER:
            return
        ser.write((DEV_USER + "\r").encode())
        _LOGIN["stage"] = 1
        out(b"\r\n\x1b[2m[\xe8\x87\xaa\xe5\x8a\xa8\xe7\x99\xbb\xe5\xbd\x95] \xe5\xb7\xb2\xe5\xa1\xab\xe5\x85\xa5\xe7\x94\xa8\xe6\x88\xb7\xe5\x90\x8d\xef\xbc\x88\xe5\x87\xad\xe6\x8d\xae\xe6\x9d\xa5\xe8\x87\xaa\xe9\x92\xa5\xe5\x8c\x99\xe4\xb8\xb2\xef\xbc\x89\x1b[0m\r\n")
    elif st == 1 and _PW_PROMPT.search(tail):
        ser.write((DEV_PW + "\r").encode())
        _LOGIN["stage"] = 2
        out(("\r\n\x1b[2m[\u81ea\u52a8\u767b\u5f55] \u5df2\u586b\u5165\u5bc6\u7801"
             "\uff08\u6765\u6e90\uff1a" + (PW_SRC or "\u94a5\u5319\u4e32") +
             "\uff0c\u4e0d\u6253\u5370\u3001\u4e0d\u843d\u65e5\u5fd7\uff09\x1b[0m\r\n").encode())


_last_data = [0.0]
_pending_since = [0.0]   # 最后一次【发输入】的时刻（在等设备回音）
_wd_last = [0.0]       # 上一次看门狗检查的时刻


def emit_screen(b: bytes):
    """只写屏幕（可染色）。"""
    try:
        os.write(sys.stdout.fileno(), b)
    except OSError:
        pass


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
_WD_RETRY = [0.0]      # 上次因看门狗重开串口的时刻（节流，避免反复重开）
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
    if log:
        try:
            log.write(b)
        except (ValueError, OSError):
            pass


# ─────────────────────────────────────────────────────────────────────
# 串口自动恢复（2026-09-26 事故后加固）
#   事故：别的进程（MCP 工具直连）与桥同时读同一个串口 → 桥抛
#   SerialException "multiple access on port" 后【直接 break 退出】
#   → 用户看到的是"串口莫名断开"，还得手工重连。
#   修法：读失败先【原地重开串口】，失败则【退避重试】；
#         连试 MAX_RETRY 次仍打不开才退出。
#   理由：抢串口多半是瞬态的（对方用完就放手），桥不该因此自杀。
# ─────────────────────────────────────────────────────────────────────
MAX_RETRY = 12
_read_fails = [0]


def _serial_readable(wait: float = 2.5) -> bool:
    """重开之后，串口真的读得回来吗？

    为什么必须验：2026-09-26 实测事故 —— 只看 ser.open() 不报错就宣布“已恢复”，
    但那个串口其实已经读不到任何数据了（桥能收键盘、设备零回显）。
    用户看到的就是“屏上一行行 [输入] 却永远没有提示符”，比直接退出更难排查。
    判据：发一个回车，wait 秒内能收到任何字节 = 读通了。
    """
    try:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(b"\r\n")
        t0 = time.time()
        while time.time() - t0 < wait:
            if host.IS_WIN:
                try:
                    if ser.in_waiting:
                        d = ser.read(4096)
                        if d:
                            return True
                except Exception:
                    return False
                time.sleep(min(0.03, wait / 10))
                continue
            try:
                r, _, _ = select.select([ser.fileno()], [], [], 0.25)
            except Exception:
                return False
            if r:
                d = ser.read(4096)
                if d:
                    return True
    except Exception:
        return False
    return False


def reopen_serial() -> bool:
    """原地重开串口（不退出进程），并且【验证真的读得回来】。

    只重开不验证 = 假成功 —— 桥会活在一个读不到设备的僵尸状态。
    """
    for attempt in range(3):
        try:
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(0.25 * (attempt + 1))
            ser.port = port
            ser.open()
            try:
                ser.baudrate = baud          # 重开后确保波特率没丢
            except Exception:
                pass
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            if _serial_readable(2.0):            # ★ 关键：读得回来才算成功
                return True
            # 读不通 → 继续下一轮（下一轮会先 close 再 open）
        except Exception:
            continue
    return False


# ── 上锁：告诉其他路径"这个口归我了"（带业主/用途/时间，可追责）──
_HOLDER = f"同屏桥:{os.path.basename(sys.argv[0]) or 'serial_bridge'}"
try:
    import sys as _sys0
    _sys0.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from lib import portlock as _pl
    _ok, _msg = _pl.acquire(port, _HOLDER, purpose="人机同屏会话（串口桥）")
    if not _ok:
        out(f"\r\n[serial_bridge] ⚠ 锁未取得：{_msg}\r\n".encode())
        out("  仍会继续尝试（同屏是主用途），但请注意可能有别的路径在读同一个口。\r\n".encode())
except Exception:
    _pl = None

# ═══ 接入自检（2026-09-29）═══════════════════════════════════════════
# 为什么需要：波特率不对 / 线没插好 / 设备没开机 时，用户看到的都是
#   【一屏空白】—— 完全不知道是哪种原因，只能靠猜（今天就踩了这个坑）。
# 做法：接入后主动发一个回车，观察几秒：
#   · 有可读回应   → 打一行 ✓（顺带确认波特率）
#   · 无回应/乱码  → 自动轮试其它候选波特率；命中就切过去并说明
#   · 全都不行     → 明确列出可能原因
# 只发回车、只观察，不改设备任何配置。
def _self_check_and_fix():
    _cands = [b for b in (115200, 9600, 38400, 57600, 19200) if b != baud]

    def _probe(secs):
        """在当前波特率下轮换发唤醒字节，返回可读字符数。

        ★ 2026-09-30：停在 Username:/Password: 时设备对单个 \r\n 不重画提示符，
          只发一个回车会漏判（本机 huawei 115200 实测：几十个回车全沉默）。
          改成 \r / \n / \r\n / 空格 轮换，累计多轮结果再判。
        """
        acc = 0
        for w in (b"\r", b"\n", b"\r\n", b" "):
            try:
                ser.reset_input_buffer()
                ser.write(w)
            except Exception:
                continue
            t0, buf = time.time(), b""
            while time.time() - t0 < secs:
                try:
                    if host.IS_WIN:
                        if ser.in_waiting:
                            d = ser.read(4096)
                            if d:
                                buf += d
                        time.sleep(min(0.03, secs / 10))
                        continue
                    r, _, _ = select.select([ser.fileno()], [], [], 0.2)
                    if r:
                        d = ser.read(4096)
                        if d:
                            buf += d
                except Exception:
                    break
            txt = buf.decode("utf-8", "ignore")
            acc += sum(1 for ch in txt if 32 <= ord(ch) < 127 or ch in "\r\n")
            if acc >= 3:
                break                       # 已有可读回应，不必把四个唤醒字节发完
        return acc

    if _probe(1.2) >= 3:
        out(("[串口自检] ✓ 设备有回应（波特率 %d）\r\n" % baud).encode())
        return
    for _b in _cands:                       # 自动换档重试
        try:
            ser.baudrate = _b
        except Exception:
            continue
        if _probe(1.2) >= 3:
            globals()["baud"] = _b
            out(("[串口自检] ⚠ 原波特率无回应，已自动切换到 %d ✓\r\n" % _b).encode())
            return
    try:
        ser.baudrate = baud
    except Exception:
        pass
    _tried = " / ".join(str(x) for x in [baud] + _cands)
    _occ = ("关掉别的串口程序（串口助手 / PuTTY / SecureCRT 等），"
            "或在设备管理器里把该 COM 口「禁用→启用」一次"
            if host.IS_WIN else f"lsof {port}")
    out(("\r\n[串口自检] ✗ 已试过 %s 各档，设备都没有回应。\r\n"
         "   请依次确认：\r\n"
         "     ① 波特率：设备侧 console 速率（华为常见 9600 或 115200）\r\n"
         "     ② 串口线 / USB 转接头：换根线或换个 USB 口\r\n"
         "     ③ 设备是否已开机、console 口是否接对\r\n"
         "     ④ 串口是否被别的程序占用：%s\r\n"
         "   注：本自检只发了一个回车，未改动设备任何配置。\r\n" % (_tried, _occ)).encode())

out(f"\r\n[串口已连接] {port} @ {baud}\r\n"
    f"[人机同屏会话；退出 Ctrl+]  IP高亮+输入着色={'开' if _paint_on else '关'}   退格适配={mode_note}   日志: {logfile or '未开启'}]\r\n\r\n".encode())

_self_check_and_fix()   # 接入自检：确认设备真的有回应


# ════════════════════════════════════════════════════════════════════
# Windows 主循环：stdin 泵线程喂 queue（替代 select on fd），
# 串口用 in_waiting 轮询（替代 select on ser.fileno）；节拍仍是 0.2s。
# POSIX 主循环在下面，一行不改。
# ════════════════════════════════════════════════════════════════════
if host.IS_WIN:
    # 输入来自守护喂送通道（本环境 stdin 管道写入被拦截）
    from lib import pane as _pane_mod
    _feed_port = int(os.environ.get("NETDEV_FEED_PORT", "0"))
    if not _feed_port:
        raise SystemExit("[serial_bridge] 缺少 NETDEV_FEED_PORT")
    q_in, _feed_sock = _pane_mod.feed_client(_feed_port)

    def _handle_input(data: bytes):
        """处理一批 stdin 字节。返回 False 表示要退出循环。"""
        if not data:
            return False
        if b"\x1d" in data:                    # Ctrl+]
            return False
        send = fix_keys(data)
        if _tf is not None:                    # 丢掉终端模拟器的自动应答
            send = _tf.strip(send)
        ser.write(send)
        if _paint_on and _echo:
            _echo.expect(send)                 # 登记期待回显 → 输入着色
        _pending_since[0] = time.time()        # 发了东西 → 开始等回音
        if log:
            log.write(_input_for_log(data, "[输入] ").encode())
            if send != data:
                log.write("[键位适配] 0x7f -> 0x08\n".encode())
        return True

    def _handle_serial():
        """读并分发一批串口数据。"""
        try:
            data = ser.read(8192)
        except Exception as e:                  # 拔线 / 别的程序同时打开
            _read_fails[0] += 1
            n = _read_fails[0]
            if n == 1:
                out(f"\r\n[serial_bridge] 读串口失败：{type(e).__name__}: {e}\r\n".encode())
                out("  可能原因：① USB 转串口被拔出/松动 ② 有别的程序也在读同一个口\r\n".encode())
                out("  ↳ 正在尝试自动恢复（原地重开串口 → 退避重试）…\r\n".encode())
            if reopen_serial():
                _read_fails[0] = 0
                out("  ✓ 串口已恢复，继续工作\r\n".encode())
                return
            if n >= MAX_RETRY:
                out(f"\r\n[serial_bridge] 已重试 {n} 次仍打不开串口，放弃。\r\n".encode())
                _exit_now[0] = True
                return
            _wait = min(0.4 * n, 3.0)
            out(f"  · 第 {n}/{MAX_RETRY} 次重试未成功，{_wait:.1f}s 后再试…\r\n".encode())
            time.sleep(_wait)
            return
        else:
            if _read_fails[0]:
                _read_fails[0] = 0
        if data:
            _last_data[0] = time.time()
            _pending_since[0] = 0.0             # 有回音 → 待决解除
            maybe_autologin(data)
            emit_screen(_echo.feed(data) if _paint_on and _echo else data)
            if log:
                try:
                    log.write(data)             # 日志保留原始字节
                except (ValueError, OSError):
                    pass

    def _win_main() -> int:
        # ★ 2026-10-07 修（网页终端「输入很卡」）：原节拍是 0.2s —— 主循环每 200ms 才看
        #   一次串口，设备回显（你敲的字符、回车后的提示符）最多要等 0.2s 才被读走转发到
        #   网页（平均 ~100ms）。POSIX 版用 select(timeout=0.2)，有数据立刻醒；Windows 版
        #   换成 in_waiting 轮询 + 无条件 sleep(0.2)，等于给每个回显加了固定延迟。
        #   这里把节拍降到 10ms（Python 3.11+ 在 Windows 用高精度定时器，10ms 是准的），
        #   并把原来「每轮都做」的孤儿判定（OpenProcess）降到每秒一次，避免空转开销。
        _TICK = 0.01
        _last_heartbeat = 0.0
        _last_orphan = 0.0
        try:
            while not _exit_now[0]:
                _now = time.time()
                # 锁心跳：每 8 秒续一次
                if _now - _last_heartbeat > 8:
                    _last_heartbeat = _now
                    try:
                        if _pl is not None:
                            _pl.heartbeat(port, _HOLDER)
                    except Exception:
                        pass
                # 孤儿判定：父守护没了 → 退出释放串口（每秒看一次即可）
                if _now - _last_orphan > 1.0:
                    _last_orphan = _now
                    if not host.pid_alive(os.getppid()):
                        out("\r\n[serial_bridge] 宿主守护已退出 → 自动退出（释放串口）\r\n".encode())
                        break
                # ── 看门狗：串口静默失效自愈 ──
                if (_LOGGED_IN[0] and _pending_since[0] and _last_data[0] > 0
                    and (_now - _pending_since[0]) > 20.0
                    and (_now - _last_data[0]) > 20.0
                    and (_now - _WD_RETRY[0]) > 30.0):
                    _pending_since[0] = 0.0
                    _WD_RETRY[0] = _now
                    out("\r\n[serial_bridge] 看门狗：发出输入后长时间零回显 → 自动重开串口…\r\n".encode())
                    if reopen_serial():
                        out("  ✓ 串口已自动恢复（看门狗）\r\n".encode())
                        _read_fails[0] = 0
                    else:
                        out("  ✗ 重开未成功；请检查 USB 线 / 是否有别的程序占用串口\r\n".encode())
                # stdin 队列（非阻塞排空）
                while True:
                    try:
                        d = q_in.get_nowait()
                    except queue.Empty:
                        break
                    if not _handle_input(d):
                        _exit_now[0] = True
                        break
                if _exit_now[0]:
                    out("\r\n[serial_bridge] 释放串口\r\n".encode())
                    break
                # 串口数据
                try:
                    if ser.in_waiting:
                        _handle_serial()
                    elif _paint_on and _echo and _echo.pending() and _now - _last_data[0] > 0.15:
                        emit_screen(_echo.flush())
                except Exception:
                    pass
                time.sleep(_TICK)
        except Exception as e:
            import traceback
            out(f"\r\n[serial_bridge] 异常退出: {type(e).__name__}: {e}\r\n".encode())
            out(traceback.format_exc().encode())
        finally:
            try:
                ser.close()
            except Exception:
                pass
            try:
                if _pl is not None:
                    _pl.release(port, _HOLDER)
            except Exception:
                pass
            if log:
                try:
                    log.write("\n[会话结束]\n".encode())
                    log.close()
                except Exception:
                    pass
            out("\r\n[串口已断开]\r\n".encode())
        return 0

    raise SystemExit(_win_main())

try:
    _last_orphan_check = 0.0
    _last_heartbeat = 0.0
    while True:
        if _exit_now[0]:
            out("\r\n[serial_bridge] 收到退出信号，释放串口\r\n".encode())
            break
        # 每 1.5 秒看一眼"我的窗格还在吗"：父进程变成 1 = 已被 launchd 收养 = 孤儿
        _now = time.time()
        if _now - _last_heartbeat > 8:                 # 每 8 秒续一次锁
            _last_heartbeat = _now
            try:
                if _pl is not None:
                    _pl.heartbeat(port, _HOLDER)
            except Exception:
                pass
        if _now - _last_orphan_check > 1.5:
            _last_orphan_check = _now
            if os.getppid() == 1:
                out("\r\n[serial_bridge] 宿主窗格已销毁 → 自动退出（释放串口）\r\n".encode())
                break
        # ── 看门狗（2026-09-27）：串口【静默失效】自愈 ──────────────────
        # 实测事故：桥能发不能收，但 ser.read() 返回空、不抛异常 ——
        # 于是原来那套"只有异常才重开"的逻辑完全不触发，用户看到的是
        # "屏上一行行 [输入]，设备零回显"，只能手工重接。
        # 判据：发过输入后 6 秒内设备零字节（且此前也已 6 秒无数据）→ 判读通道死，主动重开。
        if _now - _wd_last[0] > 1.0:
            _wd_last[0] = _now
            # 只在【登录后】且【登录后已成功读过一次】才判死 ——
            # 否则登录阶段/设备忙时会被误判，反复重开串口反而更糟。
            # 阈值从 6s 放宽到 20s，并加 30s 节流。
            if (_LOGGED_IN[0] and _pending_since[0] and _last_data[0] > 0
                and (_now - _pending_since[0]) > 20.0
                and (_now - _last_data[0]) > 20.0
                and (_now - _WD_RETRY[0]) > 30.0):
                _pending_since[0] = 0.0
                _WD_RETRY[0] = _now
                out("\r\n[serial_bridge] 看门狗：发出输入后 6 秒零回显 → 疑似读通道失效，自动重开串口…\r\n".encode())
                if reopen_serial():
                    out("  ✓ 串口已自动恢复（看门狗）\r\n".encode())
                    _read_fails[0] = 0
                else:
                    out("  ✗ 重开未成功；若持续如此请检查 USB 线 / 是否有别的程序占用串口\r\n".encode())

        try:
            r, _, _ = select.select([fd, ser.fileno()], [], [], 0.2)
        except (OSError, ValueError) as e:
            out(f"\r\n[serial_bridge] select 失败: {type(e).__name__}: {e}\r\n".encode())
            break
        if fd in r:
            data = os.read(fd, 1024)
            if not data:
                break
            if b"\x1d" in data:            # Ctrl+]
                break
            send = fix_keys(data)
            _n = 0
            if _tf is not None:                    # 丢掉终端模拟器的自动应答
                _n = _tf.dropped_count(send)
                send = _tf.strip(send)
            ser.write(send)
            if _paint_on and _echo:
                _echo.expect(send)                # 登记期待回显 → 输入着色
            _pending_since[0] = time.time()   # 发了东西 → 开始等回音（看门狗用）
            if log:
                log.write(_input_for_log(data, "[输入] ").encode())
                if send != data:
                    log.write("[键位适配] 0x7f -> 0x08\n".encode())
        if ser.fileno() in r:
            try:
                data = ser.read(8192)
            except Exception as e:          # 拔线 / 被别的程序同时打开（macOS 常见）
                _read_fails[0] += 1
                n = _read_fails[0]
                if n == 1:
                    out(f"\r\n[serial_bridge] 读串口失败：{type(e).__name__}: {e}\r\n".encode())
                    out("  可能原因：① USB 转串口被拔出/松动 ② 有别的程序也在读同一个口\r\n".encode())
                    _occ2 = ("关掉别的串口程序（串口助手 / PuTTY 等），或在设备管理器里禁用→启用该 COM 口"
                             if host.IS_WIN else f"lsof {port}")
                    out(f"  查占用：{_occ2}\r\n".encode())
                    out("  ↳ 先别急：正在尝试自动恢复（原地重开串口 → 退避重试）…\r\n".encode())
                # ① 原地重开（最常见：抢口的那方已退出，口可重新打开）
                if reopen_serial():
                    _read_fails[0] = 0
                    out("  ✓ 串口已恢复，继续工作\r\n".encode())
                    continue
                # ② 重不开 → 退避等待对方放手
                if n >= MAX_RETRY:
                    out(f"\r\n[serial_bridge] 已重试 {n} 次仍打不开串口，放弃。\r\n".encode())
                    out("  处理：① 确认线插好 ② 关掉其它串口程序（screen / NyaTerm / 另一个 netdev 会话）\r\n".encode())
                    out("        ③ 然后在网页里重新接入串口\r\n".encode())
                    break
                _wait = min(0.4 * n, 3.0)
                out(f"  · 第 {n}/{MAX_RETRY} 次重试未成功，{_wait:.1f}s 后再试…\r\n".encode())
                time.sleep(_wait)
                continue
            else:
                if _read_fails[0]:
                    _read_fails[0] = 0
            if data:
                _last_data[0] = time.time()
                _pending_since[0] = 0.0        # 有回音 → 待决解除
                maybe_autologin(data)
                emit_screen(_echo.feed(data) if _paint_on and _echo else data)
                if log:
                    try:
                        log.write(data)          # 日志保留原始字节（留档用）
                    except (ValueError, OSError):
                        pass
        elif _paint_on and _echo and _echo.pending() and time.time() - _last_data[0] > 0.15:
            emit_screen(_echo.flush())          # 尾巴等超时就吐出来，不卡显示
except Exception as e:
    import traceback
    out(f"\r\n[serial_bridge] 异常退出: {type(e).__name__}: {e}\r\n".encode())
    out(traceback.format_exc().encode())
finally:
    if old_attr is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
    try:
        ser.close()
    except Exception:
        pass
    # 释放锁：让别的路径知道这个口空出来了（放在最前，保证一定执行）
    try:
        if _pl is not None:
            _pl.release(port, _HOLDER)
    except Exception:
        pass
    if log:
        try:
            log.write("\n[会话结束]\n".encode())
            log.close()
        except Exception:
            pass
    out("\r\n[串口已断开]\r\n".encode())
