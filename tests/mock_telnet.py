#!/usr/bin/env python3
"""本机 Telnet 模拟器 —— 供 netdev 的 telnet 通路离线自检（不接触真实设备）。

行为贴近华为 VRP：先发一轮 IAC 协商（考验客户端的 IAC 处理），再 Username/Password，
提示符 <Huawei> / [Huawei]，回显逐字符，支持 display 系列 / system-view / save 的 y-n。

用法: mock_telnet.py [port] [noauth]
"""
import socket
import sys
import threading
import time

IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2323
NOAUTH = len(sys.argv) > 2 and sys.argv[2] == "noauth"
USER, PASSWORD = "admin", "admin"

VERSION = """Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (AR111-S V200R010C10SPC700)
HUAWEI AR111-S Router uptime is 0 day, 2 hours, 3 minutes
"""


class State:
    def __init__(self):
        self.view = []
        self.saved = False

    @property
    def prompt(self):
        return f"<Huawei>" if not self.view else f"[Huawei]"


def handle(st: State, cmd: str) -> str:
    c, low = cmd.strip(), cmd.strip().lower()
    if low == "display version":
        return VERSION
    if low == "display clock":
        return "\n2026-09-18 17:20:00\nFriday\nTime Zone(BJ) : UTC+08:00\n"
    if low == "display esn":
        return "\nESN of slot 0:\n    21500102842SH8602524\n"
    if low.startswith("display current-configuration"):
        return "\n#\nsysname AR111-S\n#\nvlan batch 110 220 330\n#\nreturn\n"
    if low in ("system-view", "sys"):
        st.view = ["system"]
        return "Enter system view, return user view with Ctrl+Z."
    if low in ("quit", "exit"):
        if st.view:
            st.view.pop()
        return ""
    if low == "return":
        st.view = []
        return ""
    if low.startswith("save"):
        return "  Configuration file had been saved successfully\n"
    return f"\n        ^\nError: Unrecognized command found at '^' position.\n"


def serve_one(conn, addr):
    try:
        # 先来一轮 IAC 协商（WILL ECHO / WILL SGA / DO TTYPE）—— 考验客户端
        conn.sendall(bytes([IAC, WILL, 1, IAC, WILL, 3, IAC, DO, 24]))
        time.sleep(0.4)
        buf = ""
        esc = 0          # ANSI 转义序列吞字节预算（0 = 不在序列里）

        def w(s):
            conn.sendall(s.replace("\n", "\r\n").encode())

        w("\n  Huawei Versatile Routing Platform\n")
        st = State()

        if NOAUTH:
            w(st.prompt)
        else:
            w("\nLogin authentication\n\nUsername:")

        state = "ready" if NOAUTH else "user"
        while True:
            data = conn.recv(1024)
            if not data:
                break
            # 客户端回给我们的 IAC 应答，直接丢弃
            cleaned = bytearray()
            i = 0
            while i < len(data):
                if data[i] == IAC:
                    i += 3
                    continue
                cleaned.append(data[i])
                i += 1
            for ch in cleaned.decode("utf-8", "replace"):
                # ★ 先把 ANSI 转义序列整段吞掉，别让它落进命令缓冲。
                #   终端会对设备的查询自动回一段能力应答（`\x1b[?1;2c`、`0;276;0c`
                #   之类），这些字节会从窗格漏进设备输入流。真机行编辑器不会把它们
                #   当命令字符；模拟器若不吞，命令就变成 `[1;2cdisplay clock` →
                #   永远 "Unrecognized command"（与 mock_vrp.py 同源修复，
                #   2026-10-03 实测踩到）。
                if esc:
                    esc -= 1
                    if esc == 15 and ch in "[O(":
                        continue             # 引导符本身不算终结字节
                    if "@" <= ch <= "~":     # 终结字节 → 序列结束
                        esc = 0
                    continue
                if ch == "\x1b":
                    esc = 16                 # 上限 16 字符，畸形序列也别吞掉整条命令
                    continue
                if ch in "\r\n":
                    w("\n")
                    if state == "user":
                        w("\nPassword:")
                        state = "pass"
                        buf = ""
                        continue
                    if state == "pass":
                        state = "ready"
                        w("\n" + st.prompt)
                        buf = ""
                        continue
                    cmd = buf.strip()
                    buf = ""
                    if cmd:
                        out = handle(st, cmd)
                        if out:
                            w(out if out.endswith("\n") else out + "\n")
                    w(st.prompt)
                elif ch in "\x7f\x08":
                    if buf:
                        buf = buf[:-1]
                        w("\b \b")
                elif ch == "\x15":
                    # Ctrl-U = 清空当前行。netdev 在同屏下发前会先发一个 C-u
                    # 清掉残留的终端能力应答碎片；模拟器若不认它，那个字节会被
                    # 并在命令前面 → 永远 "Unrecognized command"（详见 mock_vrp.py 同名分支）
                    for _ in buf:
                        w("\b \b")
                    buf = ""
                elif ch < " ":
                    pass          # 其余控制字符：真机行编辑会忽略，别污染 buf
                elif ch == "?":
                    w("\n  version   clock   current-configuration   esn\n" + st.prompt + buf)
                else:
                    buf += ch
                    w(ch)
    except Exception as e:
        print(f"[mock-telnet] 会话异常: {type(e).__name__}: {e}", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(port: int):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"[mock-telnet] 监听 127.0.0.1:{port}  ({'noauth' if NOAUTH else 'admin/admin'})", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=serve_one, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    serve(PORT)
