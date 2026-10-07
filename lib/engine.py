"""引擎适配层：netmiko（SSH/Telnet） + pyserial（串口）。

对外只暴露一个统一接口，上层（CLI / MCP）不关心底下是哪个库。
串口：netmiko 4 与 scrapli 均不支持，故用 pyserial 自实现"读到提示符"。
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import re
import time
import tomllib
from dataclasses import dataclass, field

# 路径统一由 lib/paths.py 解析（2026-10-03 新增）。
# 原来这里写死 `home() / "netops"`，装到别处就读不到设备清单 —— 全新克隆必挂。
from . import host
from . import paths as _paths

ROOT = _paths.ROOT
DEVICES_TOML = _paths.cfg("devices.toml")

# 兼容参数：华为 AR111-S 只支持 SHA1 密钥交换（AR110 V200R010C10SPC700 实测）
LEGACY_KEX = "diffie-hellman-group14-sha1"

ERR_PAT = re.compile(r"(Error:|Unrecognized command|Incomplete command|"
                     r"Wrong parameter|Too many parameters|Ambiguous command|"
                     r"invalid|Permission denied)", re.I)


# --------------------------------------------------------------------------- 设备清单
def load_devices(path: pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path or DEVICES_TOML)
    if not p.exists():
        return {}
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    return {d["name"]: d for d in data.get("device", [])}


def get_device(name: str, path=None) -> dict:
    devs = load_devices(path)
    if name not in devs:
        raise KeyError(f"设备清单里没有 '{name}'；可用：{', '.join(devs) or '(空)'}")
    return devs[name]


def platform_for(dev: dict) -> str:
    """决定 netmiko 的 device_type。

    ★ 2026-09-26 修（Telnet 通道被当成 SSH 连）：
      原来是 platform 优先 —— 但 platform 表示"什么设备"（命令集），
      而 device_type 表示"怎么连"（驱动）。两者混了会出这种错：
        连接簿里写 platform="huawei_vrp"（型号）+ protocol="telnet"（连法）
        → 原逻辑返回 "huawei_vrp" → netmiko 用 SSH 去连 23 端口
        → 报 "Error reading SSH protocol banner ... 0xff"（0xff 是 telnet 字节）
      现在：**protocol 优先定驱动**，platform 只提供厂商部分。
    """
    proto = (dev.get("protocol") or "ssh").strip().lower()
    plat = (dev.get("platform") or "").strip()
    # 去掉 platform 里可能已带的 _telnet 后缀，避免拼成 xxx_telnet_telnet
    base = plat[:-len("_telnet")] if plat.endswith("_telnet") else plat
    if proto == "telnet":
        # netmiko 的 telnet 驱动名不是"<platform>_telnet"那么直白
        # （实测：huawei_vrp_telnet 不存在，正确的是 huawei_telnet）
        # 这里按厂商映射；不认识的就退回 huawei_telnet（本机主要是华为）
        _tel = {
            "huawei_vrp": "huawei_telnet", "huawei": "huawei_telnet",
            "h3c_comware": "hp_comware_telnet", "h3c": "hp_comware_telnet",
            "ruijie_os": "ruijie_os_telnet", "ruijie": "ruijie_os_telnet",
            "cisco_ios": "cisco_ios_telnet", "cisco": "cisco_ios_telnet",
            "zte_zxros": "zte_zxros_telnet",
            "maipu_s": "huawei_telnet", "maipu": "huawei_telnet", "mypower": "huawei_telnet",
        }
        return _tel.get(base, "huawei_telnet")
    # 迈普 MyPower S 系列 CLI 与华为 VRP 同源：netmiko 没有 maipu 驱动，
    # 用 huawei_vrp 驱动连接最稳（命令/提示符/分页行为一致）。
    if base in ("maipu_s", "maipu", "mypower"):
        return "huawei_vrp"
    return base or "huawei_vrp"


# --------------------------------------------------------------------------- 结果
@dataclass
class Result:
    cmd: str
    ok: bool
    text: str = ""
    elapsed: float = 0.0
    error: str | None = None

    def __str__(self):
        return self.text


@dataclass
class Plan:
    lines: list[str] = field(default_factory=list)
    rollback: list[str] = field(default_factory=list)
    save: bool = True


# --------------------------------------------------------------------------- 串口提示符
PROMPT_RE = re.compile(r"[<\[](?P<p>[\w\-~]+(?:[\w\-]*)?)[>\]]\s*$")

# 交互式确认问句（save / 覆盖等），与 NetmikoSession.save 保持一致
ASK_RE = re.compile(r"\(y/n\)|\[y/n\]|\[Y/N\]|continue\?|overwrite|Are you sure", re.I)


class SerialSession:
    """pyserial 直连串口；按提示符判命令结束（不依赖任何 shell 标记）。

    已处理：console 登录认证（Username/Password）、分页 ---- More ---- 自动翻页。
    不为了关分页去改 console 视图的配置（分页靠自动翻页）。

    配置写入**只**发生在显式调用 push() / save() 时：push() 会自己进 system-view
    并在结束时 return 回用户视图；其余方法（run/hint/probe_login）一律只读。
    """

    def __init__(self, dev: dict, disable_paging: bool = False, soft_paging_off: bool = False):
        # soft_paging_off：连上后发一次 `screen-length 0 temporary`（会话级关分页）。
        # 为什么要它：分页靠自动翻页抓大配置会漏页（实测 239 行只抓到 94~117 行）。
        import serial  # pyserial

        self.dev = dev
        self.name = dev["name"]
        port = dev.get("port") or ""
        if port == "auto" or not port:
            port = discover_serial_port()
        if not port:
            raise RuntimeError("未找到可用串口设备（Windows 看设备管理器里的 COM 口；"
                               "macOS 看 /dev/cu.usbserial-*）")
        self.port = port
        # 波特率：清单写 auto/空 时先解析成实际值（缓存优先，没缓存现场探一次）。
        # 2026-10-06 修：原来直接 int(dev.get("baud", 9600)) —— 写 "auto" 会 int() 抛错，
        # 缺省 9600 又会让真机 115200 静默按 9600 起（表现为「串口无任何回显」）。
        _baud = dev.get("baud")
        if str(_baud).strip().lower() in ("", "auto", "none"):
            _baud = serial_baud_cache_get(port)
            if not _baud:
                try:
                    _baud, _ = probe_serial_baud(port)
                except Exception:
                    _baud = None
            if not _baud:
                _baud = 115200
            self.dev = dict(dev)
            self.dev["baud"] = int(_baud)
        self.ser = serial.Serial(port, int(_baud),
                                 bytesize=8, parity="N", stopbits=1,
                                 timeout=0.2, write_timeout=3)
        # settle：收完数据后静默多久算"结束"。串口设备吐字有停顿，
        # 0.8s 偏紧（实测华为 AR111-S 抓配置被截断），提到 1.5s 更稳；
        # 短命令命中提示符会立即返回，不受此值影响。
        self.settle = float(dev.get("settle", 1.5))
        self.disable_paging = disable_paging
        self.logged_in = False
        self.console_auth = bool(dev.get("console_auth", False))
        # 关键护栏：macOS 允许多进程同时打开同一串口，但读到的字节会被瓜分 → 两边都残缺
        _others = other_port_holders(self.port)
        self.port_warning = (f"⚠ 串口 {self.port} 已被其它进程占用（pid {'、'.join(_others)}）——"
                             f"两边会互相抢字节，回显可能残缺/乱码；请只保留一个（要么 screen，要么 netdev）"
                             if _others else "")

    # ── 底层读写
    PAGER_RE = re.compile(r"-+\s*More\s*-+", re.I)
    USER_RE = re.compile(r"(Login authentication|Username\s*:|login\s*:)\s*$", re.I | re.M)
    PASS_RE = re.compile(r"Password\s*:\s*$", re.I | re.M)
    FAIL_RE = re.compile(r"(Authentication fail|Logged Fail|authentication failed)", re.I)
    LOGOUT_RE = re.compile(r"Configuration console exit|retry to log on", re.I)

    def _chunk(self):
        return self.ser.read(4096)

    # 长输出命令：设备吐字中途会停顿（实测华为 AR111-S 抓 current-configuration
    # 中途静默 >0.8s，默认 settle 会误判"读完了"，只拿到 115/170 行、丢了 return）。
    # 对这类命令放宽静默阈值；短命令仍走紧凑值，保证交互跟手。
    _LONG_OUT_RE = re.compile(
        r"current-configuration|saved-configuration|"
        r"display\s+interface\b|display\s+vlan\s+all|display\s+arp\s+all|"
        r"display\s+mac-address|display\s+diagnostic|dir\b|display\s+esn|"
        r"display\s+device\b|display\s+startup",
        re.I)
    LONG_SETTLE = 2.5

    def _settle_for(self, cmd: str) -> float:
        """按命令选静默阈值：长配置类输出放宽，短命令用默认。"""
        if self._LONG_OUT_RE.search(cmd or ""):
            return max(self.settle, self.LONG_SETTLE)
        return self.settle

    def _read_until_prompt(self, timeout=20.0, max_pages=300, settle=None):
        """读到提示符；遇到 ---- More ---- 自动发空格翻页。

        宽容策略（9600 波特下设备常见“先停一下再吐数据”）：
          * 还没收到任何数据时，至少等 first_wait 秒；
          * 已经开始收到数据后，静默超过 settle 秒才认为结束。

        settle 可由调用方覆盖（见 _settle_for）：长配置输出中途会停顿 >1s，
        用默认值会被误判“读完了”（2026-10-01 实测：华为 AR111-S 抓
        current-configuration 只拿到 115/170 行，缺了收尾的 return）。
        """
        _settle = self.settle if settle is None else settle
        buf = ""
        t0 = time.time()
        last = t0
        got = False
        first_wait = min(timeout, float(self.dev.get("first_wait", 4.0)))
        pages = 0
        while time.time() < t0 + timeout:
            chunk = self._chunk()
            if chunk:
                buf += chunk.decode("utf-8", "replace")
                buf = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", buf)
                got = True
                last = time.time()
                if self.PAGER_RE.search(buf) and pages < max_pages:
                    self.ser.write(b" ")
                    pages += 1
                    time.sleep(0.15)
                    continue
                tail = buf.splitlines()[-1] if buf.splitlines() else ""
                if PROMPT_RE.search(tail):
                    time.sleep(0.08)
                    more = self.ser.read(8192)      # 收尾：抓紧跟的残留
                    if more:
                        buf += more.decode("utf-8", "replace")
                    break
                continue
            idle = time.time() - last
            if (got and idle > _settle) or (not got and idle > first_wait):
                break
            time.sleep(0.1)
        self.pages = pages
        return buf

    def probe_login(self, timeout: float = 8.0):
        """先问设备要什么，不猜。

        返回：'user_pass'（要用户名+密码）| 'pass_only'（只要密码）| 'none'（无需认证）| 'unknown'
        """
        self.ser.reset_input_buffer()
        self.ser.write(b"\r\n")
        time.sleep(0.4)
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._chunk()
            if chunk:
                buf += chunk.decode("utf-8", "replace")
                buf = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", buf)
                tail = buf.splitlines()[-1] if buf.splitlines() else ""
                if self.USER_RE.search(tail):
                    self.ser.write(b"\x03")           # 不回答，先退出登录流程
                    time.sleep(0.2)
                    return "user_pass"
                if self.PASS_RE.search(tail):
                    self.ser.write(b"\x03")
                    time.sleep(0.2)
                    return "pass_only"
                if PROMPT_RE.search(tail):
                    return "none"
            else:
                time.sleep(0.15)
        return "unknown"

    def login(self, username: str = "", password: str = "", retries: int = 3):
        """处理 console 登录（Username/Password）。

        返回 (ok, 说明)。未开启 console 认证的设备直接拿到提示符也算 ok。
        """
        out = ""
        for attempt in range(1, retries + 1):
            self.ser.reset_input_buffer()
            self.ser.write(b"\r\n")
            time.sleep(0.4)
            out = ""
            deadline = time.time() + 10
            last_data = time.time()
            sent_user = sent_pass = False
            while time.time() < deadline:
                chunk = self._chunk()
                if chunk:
                    out += chunk.decode("utf-8", "replace")
                    out = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)
                    last_data = time.time()
                if self.FAIL_RE.search(out) or self.LOGOUT_RE.search(out):
                    break
                tail = out.splitlines()[-1] if out.splitlines() else ""
                if PROMPT_RE.search(tail):
                    self.logged_in = True
                    return True, ("已进入设备" if attempt == 1 else f"已进入设备（第 {attempt} 次尝试）")
                if self.PASS_RE.search(tail) and not sent_pass:
                    self.ser.write((password + "\r").encode())
                    sent_pass = True
                    last_data = time.time()
                    time.sleep(0.4)
                    continue
                if self.USER_RE.search(tail) and not sent_user:
                    self.ser.write((username + "\r").encode())
                    sent_user = True
                    last_data = time.time()
                    time.sleep(0.3)
                    continue
                if (sent_pass or sent_user) and time.time() - last_data > 4:
                    break          # 凭据已发但迟迟没提示符 → 本次视为失败
                time.sleep(0.15)
            if self.FAIL_RE.search(out):
                time.sleep(1.0)    # 设备会限速（Please retry after N seconds）
                continue
        return False, ("登录失败：账号或密码不对"
                       if self.FAIL_RE.search(out) else "登录失败：未拿到设备提示符")

    def warmup(self):
        """握手：先唤醒设备并等到提示符，再（可选）会话级关分页。不改任何配置。

        顺序很关键（2026-09-26 修正）：
          原来把 `screen-length 0 temporary` 放在唤醒之前，而且紧接着又
          `reset_input_buffer()`，等于把设备对关分页的应答也清掉了；
          设备刚被打开时往往还没"醒"，第一轮命令容易落空 ——
          表现为直连首抓只有 3~13 行，靠重试才拿到完整配置。
        """
        # ① 先唤醒：回车 → 等提示符（串口设备空闲时不主动说话）
        self.ser.reset_input_buffer()
        self.ser.write(b"\r\n")
        time.sleep(0.4)
        try:
            self._read_until_prompt(timeout=6)
        except Exception:
            pass
        # ② 再关分页（temporary = 只影响当前会话，不写设备配置）
        if getattr(self, 'soft_paging_off', False):
            try:
                self._send('screen-length 0 temporary')
                time.sleep(0.35)
                self._read_until_prompt(timeout=8)
            except Exception:
                pass
        # ③ 收尾再稳一下，确保下一条命令从干净状态开始
        self.ser.reset_input_buffer()
        return ""

    def run(self, cmd: str, timeout: float = 15.0) -> Result:
        t0 = time.time()
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode())
        # 长输出命令（配置/接口全表等）用更大的静默阈值，避免中途停顿被截断
        out = self._read_until_prompt(timeout=timeout, settle=self._settle_for(cmd))
        txt = _clean(out, cmd)
        bad = ERR_PAT.search(txt)
        return Result(cmd, not bool(bad), txt, round(time.time() - t0, 2),
                      bad.group(0) if bad else None)

    def hint(self, prefix: str = "", timeout: float = 8.0) -> Result:
        """串口上向设备要补全提示。"""
        t0 = time.time()
        self.ser.reset_input_buffer()
        self.ser.write((prefix + "?").encode())
        out = self._read_until_prompt(timeout=timeout)
        self.ser.write(b"\x03")
        out += self._read_until_prompt(timeout=4)
        return Result(prefix + "?", True, _clean(out, ""), round(time.time() - t0, 2))

    def prompt(self, timeout: float = 6.0) -> str:
        """发一个回车，取回当前提示符（如 '<Huawei>' / '[Huawei]'）。

        console 会话是长驻的：上一个会话可能把设备留在配置视图，
        所以下发前必须先问清“现在在哪一层”，不能盲发 system-view。
        """
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")
        out = self._read_until_prompt(timeout=timeout)
        for ln in reversed(out.replace("\r", "").split("\n")):
            m = PROMPT_RE.search(ln.strip())
            if m:
                return m.group(0).strip()
        return ""

    def push(self, lines, read_timeout: int = 25):
        """配置下发：进 system-view → 按序逐条下发 → return 回用户视图。

        与 NetmikoSession.push 同一契约（返回 Result，用 ERR_PAT 判错）。
        串口没有 netmiko 的 send_config_set，故手工走视图；任一条报错即停手，
        按 Ctrl+C 清掉未完成命令行并退回用户视图，避免把后续命令下到错误上下文里。
        """
        t0 = time.time()
        lines = [str(c) for c in lines]
        out = ""
        bad = None
        prompt = self.prompt()
        out += f"[下发前提示符 {prompt or '未知'}]\n"
        seq = ([] if prompt.startswith("[") else ["system-view"]) + lines
        for c in seq:
            r = self.run(c, timeout=read_timeout)
            out += r.text + "\n"
            if not r.ok:
                bad = r.error
                self.ser.write(b"\x03")                 # Ctrl+C：清掉半截命令行
                self._read_until_prompt(timeout=4)
                rr = self.run("return", timeout=10)      # 退回用户视图
                out += rr.text + "\n[已中止下发，退回用户视图]\n"
                break
        if bad is None:
            rr = self.run("return", timeout=10)
            out += rr.text + "\n"
        txt = out.strip()
        return Result(" | ".join(lines), bad is None, txt, round(time.time() - t0, 2), bad)

    def save(self) -> Result:
        """save vrpcfg.zip（华为 y/n 交互），末尾用 dir 复核落盘文件。

        只对“最新一次回显”判问句，避免把历史的 (y/n) 反复当成新问句重发 y。
        """
        t0 = time.time()
        out = ""
        try:
            if self.prompt().startswith("["):           # save 要在用户视图
                self.run("return", timeout=10)
            last = self.run("save vrpcfg.zip", timeout=15).text
            out += last + "\n"
            for _ in range(5):
                if ASK_RE.search(last or ""):
                    last = self.run("y", timeout=25).text
                    out += last + "\n"
                else:
                    break
            verify = self.run("dir flash:/vrpcfg.zip", timeout=20)
            out += verify.text
            ok = ("vrpcfg" in verify.text) and not ERR_PAT.search(out)
            return Result("save vrpcfg.zip", ok, out.strip(), round(time.time() - t0, 2),
                          None if ok else "save 校验未通过")
        except Exception as e:
            return Result("save vrpcfg.zip", False, out.strip(), round(time.time() - t0, 2),
                          f"{type(e).__name__}: {e}")

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class NetmikoSession:
    """netmiko 承载 SSH / Telnet（华为 VRP / H3C / 锐捷 / 思科 等）。"""

    def __init__(self, dev: dict, password: str, session_log: str | None = None,
                 disable_paging: bool = True):
        from netmiko import ConnectHandler

        self.dev = dev
        self.name = dev["name"]
        params = dict(
            device_type=platform_for(dev),
            host=dev["host"],
            username=dev.get("username", "admin"),
            password=password,
            port=int(dev.get("port", 22 if dev.get("protocol", "ssh") == "ssh" else 23)),
            conn_timeout=int(dev.get("conn_timeout", 10)),
            auth_timeout=int(dev.get("auth_timeout", 15)),
            banner_timeout=int(dev.get("banner_timeout", 15)),
            timeout=int(dev.get("read_timeout", 20)),
            fast_cli=bool(dev.get("fast_cli", False)),
        )
        if dev.get("secret"):
            params["secret"] = dev["secret"]
        if session_log:
            params["session_log"] = session_log
        self.conn = ConnectHandler(**params)
        self.disable_paging = disable_paging

    def warmup(self):
        if not self.disable_paging:
            return ""
        out = ""
        try:
            out += self.conn.send_command("screen-length 0 temporary",
                                          read_timeout=10, cmd_verify=False)
        except Exception:
            pass
        return out

    def run(self, cmd: str, timeout: float | None = None) -> Result:
        t0 = time.time()
        try:
            out = self.conn.send_command(cmd, read_timeout=timeout or self.dev.get("read_timeout", 20),
                                         cmd_verify=False)
            bad = ERR_PAT.search(out or "")
            return Result(cmd, not bool(bad), out or "", round(time.time() - t0, 2),
                          bad.group(0) if bad else None)
        except Exception as e:
            return Result(cmd, False, "", round(time.time() - t0, 2), f"{type(e).__name__}: {e}")

    def push(self, lines, read_timeout: int = 25):
        """配置下发：一次性进入配置视图按序下发，返回逐条回显文本。"""
        t0 = time.time()
        try:
            out = self.conn.send_config_set(lines, read_timeout=read_timeout, cmd_verify=False)
            return Result(" | ".join(lines), not bool(ERR_PAT.search(out or "")), out or "",
                          round(time.time() - t0, 2),
                          (ERR_PAT.search(out or "").group(0) if ERR_PAT.search(out or "") else None))
        except Exception as e:
            return Result(" | ".join(lines), False, "", round(time.time() - t0, 2),
                          f"{type(e).__name__}: {e}")

    def hint(self, prefix: str = "") -> Result:
        """向设备要补全提示：发 '<前缀>?' 取回设备自身的帮助（不依赖本地命令表）。"""
        t0 = time.time()
        out = ""
        try:
            self.conn.write_channel(prefix + "?")
            time.sleep(0.7)
            out += self.conn.read_channel_timing(last_read=1.0, read_timeout=8)
            self.conn.write_channel("\x03")          # Ctrl+C 清掉未完成的命令行
            time.sleep(0.25)
            out += self.conn.read_channel_timing(last_read=0.8, read_timeout=6)
            return Result(prefix + "?", True, out, round(time.time() - t0, 2))
        except Exception as e:
            return Result(prefix + "?", False, out, round(time.time() - t0, 2),
                          f"{type(e).__name__}: {e}")

    def save(self) -> Result:
        """save（华为走 y/n 交互，用 timing 模式；只判断最新一次回显，避免重复送 y）。"""
        t0 = time.time()
        out = ""
        try:
            resp = self.conn.send_command_timing("save vrpcfg.zip", read_timeout=20)
            out += resp
            for _ in range(4):
                if re.search(r"\(y/n\)|\[y/n\]|continue\?|overwrite|\[Y/N\]|Are you sure",
                             resp, re.I):
                    resp = self.conn.send_command_timing("y", read_timeout=20)
                    out += resp
                else:
                    break
            verify = self.conn.send_command("dir flash:/vrpcfg.zip", read_timeout=15, cmd_verify=False)
            ok = ("vrpcfg" in verify) and ("Error" not in out)
            return Result("save vrpcfg.zip", ok, out + "\n" + verify,
                          round(time.time() - t0, 2), None if ok else "save 校验未通过")
        except Exception as e:
            return Result("save vrpcfg.zip", False, out, round(time.time() - t0, 2),
                          f"{type(e).__name__}: {e}")

    def close(self):
        try:
            self.conn.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- 工厂
def open_serial(dev: dict):
    """只打开串口，不做登录（由调用方决定拿什么凭据）。

    2026-09-26：改用 soft_paging_off（连上发一次 `screen-length 0 temporary`）。
    原因：靠"自动翻页"读 ---- More ---- 抓大配置会漏页 ——
    实测 `netdev run` 抓 display current-configuration 三次里有一次只拿到半截
    （真实 239 行，有时只回 117/94 行且无收尾 return）。
    temporary 只作用于当前会话，不写设备配置、不需审批。
    """
    return SerialSession(dev, disable_paging=True, soft_paging_off=True)


# ─────────────────────────────────────────────────────────────────────────
# 串口独占护栏（2026-09-26 事故后加固）
#
# 事故：AI 作业时调 MCP 的 netdev_serial_run / netdev_diff / netdev_ping，
#       这三个工具直接 engine.connect() 打开串口，**绕过**了 CLI 侧
#       netdev_cli._serial_open() 里的 _tmux_holds 检查。
#       结果：桥（同屏会话）正读着同一个串口 → 两边抢字节 →
#       SerialException: "device reports readiness to read but returned no data
#       (device disconnected or multiple access on port?)" → **桥崩，串口"莫名断开"**。
#
# 修法：把防线放在 engine.connect()——这里是所有串口连接的必经之路，
#       无论 CLI 还是 MCP 都拦得住，不再依赖调用方自觉。
# ─────────────────────────────────────────────────────────────────────────
def _tmux_holds_serial(dev_name: str) -> bool:
    """该设备是否正在 netops 的同屏会话里（串口被桥占着）。"""
    import os as _os
    import shutil as _sh
    import subprocess as _sp
    t = _sh.which("tmux") or str(pathlib.Path(_os.path.expanduser("~")) / "homebrew/bin/tmux")
    if not _os.path.exists(t) and not _sh.which("tmux"):
        return False
    try:
        r = _sp.run([t, "list-windows", "-t", "netops", "-F", "#{window_name}"],
                    capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and dev_name in r.stdout.split()
    except Exception:
        return False


def guard_serial_exclusive(dev: dict) -> None:
    """串口被占用时拒绝直连（否则两边抢字节 → 桥崩）。两重判断：

       ① 带业主的锁（lib/portlock）：谁持有 / 何时 / 干什么 —— 出事后可追责
       ② 同屏会话名字比对（兜底）：桥若还没来得及上锁也能拦住
    """
    import os as _os
    if _os.environ.get("NETDEV_ALLOW_SERIAL_SHARE") == "1":
        return
    name = dev.get("name", "")
    port = dev.get("port") or dev.get("device_port") or ""

    # ① 锁
    if port:
        try:
            from lib import portlock as _pl
            cur = _pl.status(port)
            if cur and cur.get("holder") not in (None, "", name):
                raise RuntimeError(
                    f"✘ 串口 {port} 已被占用：{cur.get('holder')}"
                    f"（用途 {cur.get('purpose') or '未注明'}，"
                    f"已持有 {int(cur.get('age',0)//60)} 分 {int(cur.get('age',0)%60)} 秒，"
                    f"pid {cur.get('pid')}）\n"
                    f"   串口是物理独占资源 —— 两方同时读会互相抢字节，导致桥崩溃。\n"
                    f"   正确做法：① netdev screen-send {name} \"display ...\"（走持有方那条链）\n"
                    f"             ② 或先在网页「管理串口占用」里释放它"
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    if name and _tmux_holds_serial(name):
        _how = ("先在网页界面断开该同屏会话（netdev ui → 会话管理）"
                if host.IS_WIN else "先退出桥（tmux attach -t netops，按 Ctrl+]）")
        raise RuntimeError(
            f"✘ {name} 正在人机同屏会话中（netops:{name}），串口被桥占着。\n"
            f"   直连会与桥抢字节，导致桥崩溃 —— 这就是「串口莫名断开」的根因。\n"
            f"   正确做法：\n"
            f"     ① 走同屏会话（推荐）：netdev screen-send {name} \"display ...\"\n"
            f"     ② 读屏：netdev screen-read {name}\n"
            f"     ③ 确实要直连：{_how}"
        )


def connect(dev: dict, password: str | None = None, session_log: str | None = None,
            disable_paging: bool | None = None, engine: str = "netmiko",
            username: str | None = None):
    """建立会话。

    串口：先 probe_login 问设备要什么 → 需要就从钥匙串取凭据登录 → 再握手；
    SSH/Telnet：netmiko。
    """
    proto = dev.get("protocol", "ssh")
    dpl = dev.get("disable_paging", proto != "serial") if disable_paging is None else disable_paging
    # ── 入口兜底（2026-09-26 加固）──
    #   SSH/Telnet 不涉及物理独占，但同样存在"多入口绕过检查"的结构性风险
    #   （例如 MCP 的工具直接调 connect，忘了带 session_log → 出事无据可查）。
    #   所以：凡没指定 session_log 的，这里自动补一个；并统一记一条连接审计。
    _auto = False
    if not session_log:
        try:
            import time as _t
            _d = ROOT / "logs"
            _d.mkdir(parents=True, exist_ok=True)
            session_log = str(_d / f"{dev.get('name','dev')}_{proto}_{_t.strftime('%Y%m%d_%H%M%S')}.log")
            _auto = True
        except Exception:
            session_log = None
    try:
        import time as _t2
        _ld = ROOT / "logs"
        _ld.mkdir(parents=True, exist_ok=True)
        with open(_ld / "connections.log", "a", encoding="utf-8") as _fp:
            _fp.write(f"{_t2.strftime('%Y-%m-%d %H:%M:%S')}\t{dev.get('name','?')}\t"
                      f"{proto}\tpid={os.getpid()}\t"
                      f"log={'auto' if _auto else (session_log or 'none')}\n")
    except Exception:
        pass
    if proto == "serial":
        guard_serial_exclusive(dev)                    # ★ 串口独占：别和同屏桥抢
        # ★ 2026-09-26 实测：串口靠"自动翻页"抓大配置会漏页
        #   （直连抓 display current-configuration 连抓三次得到 117/94/117 行，真实 239 行）。
        #   改用 VRP 的会话级关分页：screen-length 0 temporary —— temporary 只作用于当前会话，
        #   不写设备配置、不需要审批，会话结束即失效。
        s = SerialSession(dev, disable_paging=True, soft_paging_off=True)
        kind = s.probe_login()
        s.auth_kind = kind
        if kind in ("user_pass", "pass_only"):
            if not password and not dev.get("allow_no_credential"):
                s.close()
                raise RuntimeError(
                    f"{dev['name']} 的 console 要求认证（{kind}），但没有可用凭据。"
                    f"先跑一次：netdev login {dev['name']}")
            ok, msg = s.login(username or dev.get("username", ""), password or "")
            if not ok:
                s.close()
                raise RuntimeError(f"串口登录失败：{msg}（用 `netdev login {dev['name']}` 重试）")
            s.login_msg = msg
        else:
            s.login_msg = "无需认证" if kind == "none" else f"未识别认证方式({kind})"
        s.warmup()
        return s
    s = NetmikoSession(dev, password or "", session_log=session_log, disable_paging=dpl)
    s.login_msg = "已登录"
    s.warmup()
    return s


def discover_serial_port():
    """自动发现一个可用串口，返回设备名；没有则 None。

    跨平台：Windows 走 pyserial 枚举 COM 口，POSIX 走 /dev/cu.* 通配。
    （2026-10-06 补 Windows 分支：原来只 glob /dev/cu.*，Windows 上恒为 None，
      于是 devices.toml 里写 `port = "auto"` 的串口设备——含
      `netdev device-add serial --auto` 生成的——一接入就报
      「未找到可用串口设备」，而机器上明明插着 COM3。）
    """
    if host.IS_WIN:
        ports = host.serial_ports()
        if not ports:
            return None
        # 优先挑 USB 转串口（设备 Console 基本都走 USB 适配器）；
        # 主板自带的 COM1 往往是调试口，排后面。
        _usb = ("usb", "serial", "ch340", "cp210", "ftdi", "prolific", "silicon")
        def _rank(it):
            dev_name, desc = it
            d = (desc or "").lower()
            return (0 if any(k in d for k in _usb) else 1, dev_name)
        return sorted(ports, key=_rank)[0][0]
    import glob
    pats = ["/dev/cu.usbserial*", "/dev/cu.usbmodem*", "/dev/cu.SLAB_USBtoUART*",
            "/dev/cu.wchusbserial*", "/dev/tty.usbserial*"]
    found = []
    for p in pats:
        found += sorted(glob.glob(p))
    return found[0] if found else None


def _clean(raw: str, cmd: str) -> str:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = [ln for ln in lines if ln.strip() != cmd.strip()]
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- 过滤
FILTER_RE = re.compile(
    r"^(?P<base>.+?)\s*\|\s*(?P<kind>include|exclude|begin)\s+(?P<pat>.+)$", re.I)


def split_filter(cmd: str):
    """把 `display xxx | include A|B` 拆成 (基础命令, 过滤方式, 关键词)。

    串口（console）不接受 `|` 过滤（实测报 Too many parameters），
    所以拆开后先跑基础命令，再在本地过滤。
    """
    m = FILTER_RE.match((cmd or "").strip())
    if not m:
        return None, None, None
    return m.group("base").strip(), m.group("kind").lower(), m.group("pat").strip()


def apply_filter(text: str, kind: str, pat: str) -> str:
    pats = [p.strip() for p in pat.split("|") if p.strip()]
    lines = (text or "").splitlines()
    if kind == "include":
        out = [ln for ln in lines if any(p in ln for p in pats)]
    elif kind == "exclude":
        out = [ln for ln in lines if not any(p in ln for p in pats)]
    else:  # begin
        idx = next((i for i, ln in enumerate(lines) if any(p in ln for p in pats)), None)
        out = lines[idx:] if idx is not None else []
    return "\n".join(out).strip()


def run_smart(s, cmd: str, **kw) -> Result:
    """统一入口：串口上遇到 `|` 过滤就本地代劳（回显更可信，也避开 console 限制）。"""
    base, kind, pat = split_filter(cmd)
    if base and isinstance(s, SerialSession):
        r = s.run(base, **kw)
        r.cmd = cmd
        if r.ok:
            filtered = apply_filter(r.text, kind, pat)
            r.text = (filtered or "(本地过滤后无匹配行)") + f"\n[本地过滤：{kind} {pat}]"
        return r
    if base and hasattr(s, "conn") and kw.pop("local_filter", False):
        r = s.run(base)
        r.cmd = cmd
        if r.ok:
            r.text = apply_filter(r.text, kind, pat) or "(本地过滤后无匹配行)"
        return r
    return s.run(cmd, **kw)


# --------------------------------------------------------------------------- diff
def diff_configs(a_text: str, b_text: str, label_a="A", label_b="B") -> str:
    import difflib
    return "\n".join(difflib.unified_diff(
        a_text.splitlines(), b_text.splitlines(),
        fromfile=label_a, tofile=label_b, lineterm="", n=2))


def other_port_holders(port: str, exclude_pid: int | None = None):
    """列出正在占用该串口的**其它**进程（macOS 允许多进程打开同一串口，但会互相抢字节）。"""
    import os as _os
    try:
        import subprocess as _sp
        out = _sp.run(["/usr/sbin/lsof", "-t", port], capture_output=True, text=True, timeout=5).stdout.split()
        me = str(exclude_pid or _os.getpid())
        return [p for p in out if p != me]
    except Exception:
        return []


def serial_baud_cache_get(port):
    """读波特率缓存（上次探测到的值）。"""
    import json as _json
    try:
        return _json.loads((_paths.state_dir()/"serial_baud.json").read_text()).get(port)
    except Exception:
        return None


def serial_baud_cache_put(port, baud):
    import json as _json
    f = _paths.state_dir()/"serial_baud.json"
    try:
        data = _json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        data = {}
    data[port] = int(baud)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    except Exception:
        pass


def serial_display(dev: dict):
    """串口设备给人看的 (端口, 波特率)：清单里写 auto 时尽量解析成实际值。

    为什么需要（2026-10-06 修）
        `port = "auto"` / `baud = "auto"` 是设计允许的（USB 转串口换口后不必改清单），
        但 `netdev list` 与网页端原来把清单原值直接显示成「串口 auto @9600」——
        真机上设备明明在 COM3 @115200，用户看到会以为没认到设备。
        这里解析成实际端口/波特率；解析不到再退回 auto（不编造）。
    """
    port = str(dev.get("port") or "").strip()
    if port.lower() in ("", "auto"):
        port = discover_serial_port() or "auto"
    baud = dev.get("baud")
    if str(baud).strip().lower() in ("", "auto", "none"):
        baud = serial_baud_cache_get(port) or "auto"
    return port, baud


def probe_serial_baud_wake(port, candidates=(115200, 9600, 38400, 57600, 19200), timeout=0.9):
    """猜 console 波特率 —— 唤醒字节轮换版（2026-09-30 加）。

    为什么比 probe_serial_baud 强：设备停在 Username: / Password: 登录提示符时，
    对【单个 CR/LF 不重画提示符】（华为实测：敲几十个回车全沉默）。
    此时只发 \r\n 会误判「这一档无回应」，把好设备当坏设备。
    修法：每个波特率发【不同】的唤醒字节（\r / \n / \r\n / 空格），并累计
    多轮探测的结果，而不是一次不响就判死。
    """
    import serial
    holders = other_port_holders(port)
    if holders:
        cached = serial_baud_cache_get(port)
        return (cached or 0), f"端口被占用（pid {','.join(holders)}）"
    wakes = (b"\r", b"\n", b"\r\n", b" ")
    best = (None, "", -999)
    for b in candidates:
        acc_txt = ""
        try:
            with serial.Serial(port, b, timeout=0.05, bytesize=8, parity="N",
                               stopbits=1, rtscts=False, dsrdtr=False) as sp:
                for w in wakes:                      # ★ 唤醒字节轮换，不反复发同一个 \r
                    try:
                        sp.reset_input_buffer()
                    except Exception:
                        pass
                    sp.write(w)
                    t0 = time.time()
                    buf = b""
                    while time.time() - t0 < timeout:
                        chunk = sp.read(512)
                        if chunk:
                            buf += chunk
                    acc_txt += buf.decode("utf-8", "ignore")
        except Exception:
            continue
        good = sum(1 for ch in acc_txt if 32 <= ord(ch) < 127)
        bad = sum(1 for ch in acc_txt if ord(ch) < 9 or 13 < ord(ch) < 32 or ord(ch) == 127)
        score = good - 2 * bad
        if good >= 3 and score > 0:
            serial_baud_cache_put(port, b)
            return b, acc_txt.strip()[:60]
        if score > best[2]:
            best = (b, acc_txt.strip()[:60], score)
    return (best[0] or 9600), best[1]


def probe_serial_baud(port, candidates=(115200, 9600, 38400, 57600, 19200), timeout=0.9):
    """猜 console 波特率：逐个试，发一个回车看哪档能收到**可读**文本。

    用途：设备速率被改过 / 恢复出厂回落到 9600 时，接入不会失联。
    返回 (波特率, 证据片段)；全都不通时返回 (9600, "")。
    """
    # ★ 2026-09-30：统一改走唤醒字节轮换版 —— 停在登录提示符的设备对单个
    #   \r\n 不重画提示符，旧逻辑会误判「无回应」（本机 huawei 115200 实测踩到）。
    _b, _ev = probe_serial_baud_wake(port, candidates, timeout)
    if _ev and not _ev.startswith("端口被占用"):
        return _b, _ev
    import serial
    # ★ 安全闸：端口已被别人（含我们自己的桥）占用时**绝不打开** ——
    #   macOS 允许多进程打开同一串口，但会互相抢字节、让对方 read 抛
    #   "device reports readiness to read but returned no data" 而崩掉。
    holders = other_port_holders(port)
    if holders:
        cached = serial_baud_cache_get(port)
        return (cached or 0), f"端口被占用（pid {','.join(holders)}）"
    best = (None, "", -999)
    for b in candidates:
        try:
            with serial.Serial(port, b, timeout=0.05, bytesize=8, parity="N",
                               stopbits=1, rtscts=False, dsrdtr=False) as sp:
                try:
                    sp.reset_input_buffer()
                except Exception:
                    pass
                sp.write(b"\r\n")
                t0 = time.time()
                buf = b""
                while time.time() - t0 < timeout:
                    chunk = sp.read(512)
                    if chunk:
                        buf += chunk
                txt = buf.decode("utf-8", "ignore")
                good = sum(1 for ch in txt if 32 <= ord(ch) < 127)
                bad = sum(1 for ch in txt if ord(ch) < 9 or 13 < ord(ch) < 32 or ord(ch) == 127)
                score = good - 2 * bad
                if good >= 3 and score > 0:
                    serial_baud_cache_put(port, b)
                    return b, txt.strip()[:60]
                if score > best[2]:
                    best = (b, txt.strip()[:60], score)
        except Exception:
            continue
    return (best[0] or 9600), best[1]
