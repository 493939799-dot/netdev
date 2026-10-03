#!/usr/bin/env python3
"""本机 VRP 模拟器（paramiko SSH 服务器）—— 仅供 netdev 离线自检，不接触真实设备。

模拟内容：登录认证、提示符/视图切换、display 系列、acl 规则增删、save 的 y/n 交互、
dir flash:/vrpcfg.zip。用于在无真机时验证 run / 闸门 / apply / save / backup 全链路。

用法: python3 mock_vrp.py [port]
"""
import socket
import sys
import threading
import time

import paramiko

HOST_KEY = paramiko.RSAKey.generate(2048)
USER, PASSWORD = "admin", "admin"

VERSION_TEXT = """Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (AR111-S V200R010C10SPC700)
Copyright (C) 2012-2024 Huawei Technologies Co., Ltd.
HUAWEI AR111-S Router uptime is 0 day, 3 hours, 12 minutes
"""


class State:
    def __init__(self):
        self.view = []                    # 视图栈
        self.acl = {"2000": []}           # acl -> [rule 字符串]
        self.saved = False
        self.flash_size = 1826
        self.sysname = "MOCK-HW"

    @property
    def prompt(self):
        if not self.view:
            return f"<{self.sysname}>"
        if self.view[0] == "acl":
            return f"[{self.sysname}-acl-basic-{self.view[1]}]"
        return f"[{self.sysname}]"

    def config(self):
        out = [f"#", f"sysname {self.sysname}", "#"]
        for acl, rules in self.acl.items():
            out += [f"acl number {acl}"]
            out += [f" {r}" for r in rules]
            out += ["#"]
        out += ["interface Vlanif1", " ip address 192.168.1.1 255.255.255.0", "#",
                "user-interface vty 0 4", " authentication-mode aaa",
                " protocol inbound ssh", " user privilege level 15", "#",
                "local-user admin password irreversible-cipher ********",
                "local-user admin privilege level 15", "local-user admin service-type ssh",
                "#", "return"]
        return "\n".join(out) + "\n"


SHARED = State()   # 全设备共享状态：save 后 flash 配置在其他会话也生效（贴近真机）


class Handler(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == USER else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *a, **k):
        return True

    def check_channel_shell_request(self, channel):
        return True


def shell_loop(chan):
    st = SHARED
    buf = ""

    def w(s):
        chan.sendall(s.replace("\n", "\r\n").encode())

    time.sleep(0.2)
    w("\r\n  Huawei Versatile Routing Platform\r\n")
    w(st.prompt)

    while True:
        try:
            data = chan.recv(1024)
        except Exception:
            break
        if not data:
            break
        for ch in data.decode("utf-8", "replace"):
            if ch in "\r\n":
                w("\r\n")
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
            elif ch == "\x03":
                buf = ""
                w("^C\r\n" + st.prompt)
            else:
                buf += ch
                w(ch)          # 设备回显
    try:
        chan.close()
    except Exception:
        pass


def handle(st: State, cmd: str) -> str:
    c = cmd.strip()
    low = c.lower()
    v = st.view[0] if st.view else ""

    if low == "display version":
        return VERSION_TEXT
    if low == "display clock":
        return "\n2026-09-16 14:04:14\nWednesday\nTime Zone(BJ) : UTC+08:00\n"
    if low == "display esn":
        return "\nESN of slot 0:\n    21500102842SH8602524\n"
    if low.startswith("display device"):
        return "\nSlot  Type    Online    Power    Register    Alarm\n  0   AR111-S Present   Present  Registered  Normal\n"
    if low.startswith("display interface brief"):
        return "\nInterface     PHY   Protocol  InUti OutUti  inErrors outErrors\nGE0/0/0       up    up           0%     0%         0         0\nGE0/0/1       down  down         0%     0%         0         0\n"
    if low.startswith("display ip interface brief"):
        return "\nInterface          IP Address        Physical   Protocol\nVlanif1            192.168.1.1/24    up         up\n"
    if low.startswith("display vlan"):
        return "\nVID   Status  Property  MAC-LRN  Statistics  Description\n1     enable  default   enable   enable      -\n110   enable  default   enable   enable      WaiWang\n"
    if low.startswith("display current-configuration"):
        if "|" in c:
            kw = c.split("|", 1)[1].strip().split("|")
            return "\n".join(l for l in st.config().splitlines() if any(k.strip() in l for k in kw)) + "\n"
        return "\n" + st.config()
    if low.startswith("display saved-configuration"):
        if not st.saved:
            return "\nWarning: The current configuration is not saved to the device.\n"
        if "|" in c:
            kw = c.split("|", 1)[1].strip().split("|")
            return "\n".join(l for l in st.config().splitlines() if any(k.strip() in l for k in kw)) + "\n"
        return "\n" + st.config()
    if low.startswith("display acl"):
        num = c.split()[-1] if len(c.split()) > 2 else "2000"
        rules = st.acl.get(num, [])
        head = f"\nBasic ACL {num}, {len(rules)} rules\nAcl's step is 5\n"
        body = "".join(f" rule {r}\n" for r in rules) or " (no rules)\n"
        return head + body
    if low in ("display users",):
        return "\n  User-Intf  Delay   Type   Network Address   AuthenStatus\n  0  CON 0   00:00:00  Serial\n"
    if low in ("system-view", "sys"):
        st.view = ["system"]
        return ""
    if v == "system" and low.startswith("acl ") and len(c.split()) == 2:
        st.acl.setdefault(c.split()[1], [])
        st.view = ["acl", c.split()[1]]
        return ""
    if v == "system" and low.startswith("sysname "):
        st.sysname = c.split(None, 1)[1]
        return ""
    if v == "acl" and low.startswith("rule "):
        st.acl.setdefault(st.view[1], []).append(c.split(None, 1)[1])
        return ""
    if v == "acl" and low.startswith("undo rule "):
        parts = c.split()
        key = parts[-1] if len(parts) > 2 else ""
        st.acl[st.view[1]] = [r for r in st.acl.get(st.view[1], []) if not r.startswith(key + " ")]
        return ""
    if low == "return":
        st.view = []            # 真实 VRP：return 一路回到用户视图
        return ""
    if low in ("quit", "exit"):
        if st.view:
            st.view.pop()
        return ""
    if low.startswith("save"):
        return "__SAVE__"
    if low.startswith("dir flash:/vrpcfg.zip") or low == "dir":
        return (f"\nDirectory of flash:/\n\n    Idx  Attr     Size(Byte)  Date        Time       FileName\n"
                f"      0  -rw-        {st.flash_size}  Sep 16 2026 14:10      vrpcfg.zip\n\n"
                f"1,826,010 KB total (1,000,000 KB free)\n")
    if low.startswith("ping"):
        return ("\n  PING 8.8.8.8: 56 data bytes, press CTRL_C to break\n"
                "    Reply from 8.8.8.8: bytes=56 Sequence=1 ttl=113 time=38 ms\n"
                "  --- 8.8.8.8 ping statistics ---\n    5 packet(s) transmitted, 5 packet(s) received, 0.00% packet loss\n")
    if low.startswith("screen-length 0 temporary") or low.startswith("undo terminal monitor"):
        return ""
    return f"\n        ^\nError: Unrecognized command found at '^' position.\n"


def handle_save_interactive(chan, st: State):
    """save 的 y/n 交互（真实 VRP 行为）。"""
    chan.sendall("The current configuration will be written to the device.\r\n"
                 "Are you sure to continue? (y/n)[n]:")
    got = _read_line(chan)
    if got.strip().lower() != "y":
        chan.sendall("\r\nInfo: The configuration is not saved.\r\n")
        return
    chan.sendall("\r\nNow saving the current configuration to the slot 0.\r\n"
                 "Save the configuration successfully.\r\n"
                 "Configuration file had been saved successfully\r\n")
    st.saved = True
    st.flash_size = 1866


def _read_line(chan):
    buf = ""
    while True:
        d = chan.recv(64)
        if not d:
            break
        for ch in d.decode("utf-8", "replace"):
            if ch in "\r\n":
                chan.sendall("\r\n")
                return buf
            if ch in "\x7f\x08":
                buf = buf[:-1]
            else:
                buf += ch
                chan.sendall(ch)


def _help_for(buf: str) -> str:
    """模拟 VRP 的 '?' 补全提示（仅覆盖自检用得到的部分）。"""
    b = buf.strip()
    one = {
        "": "  display      Display information\n  system-view  Enter system view\n"
            "  save         Save configuration\n  dir          List files\n"
            "  ping         Ping test\n  quit         Quit\n",
        "display": "  version                  Display version information\n"
                   "  clock                    Display clock\n"
                   "  current-configuration    Display current configuration\n"
                   "  saved-configuration      Display saved configuration\n"
                   "  interface                Display interface information\n"
                   "  ip                       Display IP information\n"
                   "  vlan                     Display VLAN information\n"
                   "  acl                      Display ACL information\n"
                   "  nat                      Display NAT information\n"
                   "  esn                      Display ESN\n"
                   "  device                   Display device information\n"
                   "  users                    Display terminal users\n",
        "display ip": "  interface      Display IP interface information\n"
                      "  routing-table  Display routing table\n  pool           Display IP pool\n",
        "display nat": "  outbound   Display NAT outbound information\n"
                       "  session    Display NAT session information\n",
    }
    return one.get(b, f"  <不完整的命令：{b}，后续参数请补充>\n")


def serve(port: int):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    print(f"[mock] VRP 模拟器监听 ssh://127.0.0.1:{port}  (admin/admin)", flush=True)

    def one(conn):
        try:
            t = paramiko.Transport(conn)
            t.add_server_key(HOST_KEY)
            t.start_server(server=Handler())
            chan = t.accept(20)
            if chan is None:
                return
            # 简单 shell：支持 save 的交互式 y/n
            st = SHARED
            chan.sendall("\r\n  Huawei Versatile Routing Platform\r\n".encode())
            buf = ""
            chan.sendall(st.prompt.encode())
            while True:
                d = chan.recv(1024)
                if not d:
                    break
                for ch in d.decode("utf-8", "replace"):
                    if ch in "\r\n":
                        chan.sendall(b"\r\n")
                        cmd = buf.strip(); buf = ""
                        if cmd.lower().startswith("save"):
                            handle_save_interactive(chan, st)
                        elif cmd:
                            out = handle(st, cmd)
                            if out == "__SAVE__":
                                handle_save_interactive(chan, st)
                            elif out:
                                chan.sendall((out if out.endswith("\n") else out + "\n").replace("\n", "\r\n").encode())
                        chan.sendall(st.prompt.encode())
                    elif ch in "\x7f\x08":
                        if buf:
                            buf = buf[:-1]; chan.sendall(b"\b \b")
                    elif ch == "\x03":
                        buf = ""; chan.sendall(b"^C\r\n" + st.prompt.encode())
                    elif ch == "?":
                        chan.sendall(("\r\n" + _help_for(buf)).replace("\n", "\r\n").encode())
                        chan.sendall(("\r\n" + st.prompt + buf).encode())
                    else:
                        buf += ch
                        chan.sendall(ch.encode())
            chan.close()
        except Exception as e:
            print(f"[mock] 会话异常: {type(e).__name__}: {e}", flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    while True:
        conn, _ = sock.accept()
        threading.Thread(target=one, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 20022)
