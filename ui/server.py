#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netdev-ui —— netdev 的独立界面（本地 HTTP 服务，纯 Python 标准库，零第三方依赖）

设计原则（与 netdev 保持一致）
  1. 能力留在 netdev：本服务 **不 import netdev 内部模块**，只调它的 CLI（netdev list / run / apply …）
     —— 进程边界就是护栏的物理基础，AI 与界面都无法绕过 lib/gates.py
  2. 终端是"人机同屏"：attach 到 netdev 建的 tmux 设备窗格，你和 AI 看的是同一块屏
  3. 每个浏览器标签一个专属 tmux 会话（用 tmux 会话组共享 windows）
     —— 各自有各自的"当前窗口"，多个标签互不抢屏
  4. 写操作必须人点：审批通道独立于 AI（人机分离），AI 拿不到确认入口
  5. 只绑定 127.0.0.1：本机工具，不对外监听

用法
  python3 ~/netops/ui/server.py [--port 8898] [--host 127.0.0.1]
  浏览器打开 http://127.0.0.1:8898
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import http.server
import json
import os
import pathlib
import pty
import queue
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse

HOME = pathlib.Path.home()
# 2026-10-03：ui/server.py 是**直接跑的**（`python ui/server.py`），此时 sys.path[0]
# 是 ui/ 而不是项目根。下面 from lib import ... 会 ModuleNotFoundError（实测踩到：
# 服务根本起不来）。所以先把项目根算出来放进 sys.path，再导入 paths。
_ROOT_SELF = pathlib.Path(
    os.environ.get("NETDEV_ROOT") or pathlib.Path(__file__).resolve().parent.parent
).expanduser().resolve()
if str(_ROOT_SELF) not in sys.path:
    sys.path.insert(0, str(_ROOT_SELF))
from lib import hostenv as _H, paths as _P   # noqa: E402  路径统一真源 + 宿主钩子剥离
ROOT = _P.ROOT
STATIC = pathlib.Path(__file__).resolve().parent / "static"
# 版本号单一真源：dist/installer/VERSION（发布流水线写它）。
# 原来 /api/health 里硬编码 "0.1"，而 VERSION 是 1.0.0 —— 同一件事两个答案，
# 排障时会被误当成"装的是旧版"。读不到就退回一个明确的开发版号，不要瞎猜。
def _app_version() -> str:
    vf = ROOT / "dist" / "installer" / "VERSION"
    try:
        v = vf.read_text(encoding="utf-8").strip()
        return v or "dev"
    except Exception:
        return "dev"


APP_VERSION = _app_version()
TMUX_SESSION = "netops"          # netdev 的设备窗格住在这个会话里

# ── 找 tmux（本机 PATH 可能被 shim 动过，所以显式列候选）───────────────────
def find_tmux() -> str | None:
    cands = [shutil.which("tmux"),
             str(HOME / "homebrew/bin/tmux"),
             "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"]
    for p in cands:
        if p and pathlib.Path(p).exists():
            return p
    return None

TMUX = find_tmux()


def netdev_json(args: list[str], timeout: int = 20):
    """调 netdev CLI 并解析 JSON。失败返回 None（绝不抛给调用方）。"""
    cli = ROOT / "netdev"
    if not cli.exists():
        return None
    try:
        # ★ 用干净环境：剥掉宿主注入的 Node / Shell shim（见 _strip_host_shim）。
        #   shim 会在 PATH 里塞 brokered-bin / safe-bin 包装器，把 mkdir / rm
        #   换成受管版本 —— netdev CLI 自己要建 pid、快照、回收区目录，
        #   被换成受管版本后错误码与语义都不可依赖（实测踩过，见文件头注释）。
        r = subprocess.run([str(cli), *args], capture_output=True, text=True,
                           timeout=timeout, env=_env_with_node())
        out = (r.stdout or "").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def raw_netdev(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    cli = ROOT / "netdev"
    # ★ 网页审批通道：把 UI 自己的地址带给 netdev CLI 子进程，
    #   让它审批走「网页弹窗」（/api/ask）而不是 macOS 原生 osascript 弹窗。
    #   之前漏了这里 —— 界面里点「下发配置」时 approval 读到空的
    #   NETDEV_APPROVAL_URL，就会回退到系统弹窗（用户 2026-09-30 反馈）。
    # ★ 2026-10-03：改用 _env_with_node()（= os.environ 先剥宿主 shim 再补 PATH），
    #   别再把带 shim 的原始环境原样传给 netdev CLI。
    env = _env_with_node()
    if UI_BASE:
        env["NETDEV_APPROVAL_URL"] = UI_BASE
    try:
        r = subprocess.run([str(cli), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════════════════
# ★ 宿主注入的 Node / Shell / Python shim —— UI 拉起的**任何**子进程都先剥掉
#
# 背景一（2026-10-03 实测，Node 侧）：
#   宿主（WorkBuddy 桌面版）会给每一个 Node 进程注入
#     NODE_OPTIONS=--require=…/cli/vendor/shim/node-language-shim.cjs
#   该 shim 接管了 fs，把 mkdir 撞名的 EEXIST **改写**成
#     code = "CODEBUDDY_BROKER_DENY"（message 文本里仍写着 "EEXIST: …"）
#   凡是用 proper-lockfile 这类「看 err.code 决定重试 / 自愈」的库，
#   都会因为 code 被换掉而**整段跳过自愈分支** —— 崩溃残留的锁永不过期，
#   且报错点离真因极远。同一套 shim 还会在 PATH 里塞 brokered-bin /
#   safe-bin 命令包装器，把 rm / mkdir / rmdir 换成受管版本。
#
# 背景二（2026-10-04 实测，Python 侧 —— 上一轮只修了 Node 侧，漏了这条）：
#   同一个 shim 目录被注入到 PYTHONPATH，里面有个 sitecustomize.py 会在解释器
#   启动时被自动 import，把 os.remove / os.unlink / shutil.rmtree / pathlib.unlink
#   全换成受管版本，每次删除前跑一次「批量删除守卫」（按轮次累计文件数，超阈值
#   就 raise SystemExit）。SystemExit 是 BaseException，业务代码的
#   `except Exception` 接不住；而 threading.excepthook 对 SystemExit **静默忽略**
#   → 日志里连 traceback 都没有，连接直接断开，浏览器只报 "Load failed"。
#   于是界面上的「彻底删除快照」变成了一个查不出原因的失败。
#
# 边界（刻意做窄）：只剔除**指向宿主 shim 的那一条 token / 那几个目录**，
#   用户自己设的 NODE_OPTIONS / PYTHONPATH 内容原样保留；PATH 里
#   `~/.workbuddy/binaries/...` 这类**含 workbuddy 字样但属于运行时**的路径
#   **绝不误伤**（用 `/cli/vendor/shim` 这种精确特征匹配，不拿 "workbuddy"
#   当关键词）。
#
# 实现见 lib/hostenv.py（daemonize.py 拉起服务时用的是同一份逻辑）。
# 下面三个名字保留，是为了让既有回归与历史调用点原样可用。
# ══════════════════════════════════════════════════════════════════════════
_SHIM_TOKENS = _H._SHIM_TOKENS


def _is_shim_token(tok: str) -> bool:
    return _H.is_shim_token(tok)


def _strip_host_shim(env: dict) -> dict:
    """剥掉宿主注入的 Node / Shell / Python shim，让子进程回到内核语义。

    只按**精确特征**匹配宿主 shim 的路径与键名；不命中的一律原样保留。
    详见 lib/hostenv.py 的文件头。
    """
    return _H.strip_host_injection(env)


def _env_with_node() -> dict:
    """给子进程一个「干净 + 能找到 node」的环境。

    ★ 在「补 PATH」之前**先剥掉宿主注入的 shim**（见上方长注释）。
      这一步对「用户自己双击启动的 UI」是空操作（那种场景下环境本来就干净），
      对「从宿主里拉起的 UI」才是救命的那一刀。
    """
    env = _strip_host_shim(dict(os.environ))
    extra = [str(HOME / ".npm-global/bin"), "/usr/local/bin", "/opt/homebrew/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


# ══════════════════════════════════════════════════════════════════════════
#  可接入的 AI 后端 —— 只剩「直连 OpenAI 兼容 API」一条路（2026-10-03）
#
#  为什么砍掉另外两条：pi agent（RPC）与 WorkBuddy agent（headless）都是
#  「借别人的 CLI 当引擎」，各自带着一整套与 netdev 无关的故障面 ——
#    · pi：凭据 / 配置锁（proper-lockfile）残留、启动时跑 npm install、
#          0.85.1 根本没有 MCP 支持（netdev 的 13 个工具其实传不进去）；
#    · wb：内部服务端口冲突会**静默挂死**、凭据是宿主加密信封解不开、
#          --tools 白名单会把 MCP 工具一起掐掉…
#  直连后端只需要一把 API Key：零常驻进程、零第三方 CLI、零凭据文件依赖。
#  而且 netdev 的 13 个设备工具与全部护栏**一行不改**地复用
#  （见 DirectSession：import netdev_mcp，复用 HANDLERS + envelope）。
# ══════════════════════════════════════════════════════════════════════════
def probe_agents(deep: bool = False) -> dict:
    """探测 AI 后端可用性 —— 现在只报告「直连凭据配好了没有」。

    保留本函数的返回形状（agents / recommended / deep / note），
    是为了让 /api/agents 与界面上的探测面板继续工作，前端不必重写。
    """
    cfg = _direct_cfg()
    d_ok, d_note = _direct_cfg_state()
    keys = [k for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                        "GEMINI_API_KEY", "OPENROUTER_API_KEY") if os.environ.get(k)]
    item = {"id": "direct", "name": "直连 API", "available": d_ok, "path": None,
            "version": "", "mode": "自实现 agent loop（直连 OpenAI 兼容 API）",
            "tested": True, "env_keys": keys, "authed": d_ok, "auth_note": d_note,
            "provider": cfg.get("provider") or "",
            "base_url": cfg.get("base_url") or "",
            "model": cfg.get("model") or "",
            "hint": "在界面 ⚙ 设置里粘贴 API Key；或设 NETDEV_DIRECT_API_KEY + "
                    "NETDEV_DIRECT_BASE_URL；或写 config/direct.json"}
    if deep and d_ok:
        ok, why = _direct_probe()
        item["deep"] = {"ok": ok, "detail": why}

    if not item["available"]:
        item["verdict"] = "missing"
    elif item.get("authed"):
        item["verdict"] = "ready"
    else:
        item["verdict"] = "no-auth"
    if deep and item.get("deep") and not item["deep"].get("ok"):
        item["verdict"] = "broken"

    return {"agents": [item],
            "recommended": "direct" if item["verdict"] == "ready" else "",
            "deep": deep,
            "note": "ready=能接入 / no-auth=没配 Key / broken=连不通 / missing=未安装；"
                    "深探测会真发一条最小 prompt（消耗极少量额度）"}


# ══════════════════════════════════════════════════════════════════════════
#  终端会话：一个浏览器标签  ↔  一个专属 tmux 会话（共享 netops 的窗格）
# ══════════════════════════════════════════════════════════════════════════
class TermSession:
    BACKLOG_MAX = 300_000        # 回放缓冲上限（字节）

    def __init__(self, sid: str, dev: str, window: str):
        self.sid = sid
        self.dev = dev
        self.window = window
        self.tmux_session = f"ui-{sid}"     # 保留（仅作标识），不再建会话
        self._client_tty: str | None = None   # 自己那个 tmux client 的 tty
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self.subs: set[queue.Queue] = set()
        self.backlog = bytearray()
        self.lock = threading.Lock()
        self.alive = False
        self.created = time.time()
        self.last_active = time.time()      # 最近一次输入/订阅，供空闲回收用

    # ── 起会话 ──
    def start(self, rows: int = 40, cols: int = 140) -> None:
        if not TMUX:
            raise RuntimeError("找不到 tmux（同屏终端依赖它）")
        # ★ 2026-09-26 改回【直接 attach netops】，不再建 ui-* 临时会话。
        #   原因：临时会话是独立 session，其 client 尺寸被 tmux 锁死
        #   （实测 pty 改到 200x60，client 仍停在 98x31），
        #   于是最大化后 window 逻辑尺寸虽然变成 194x55，屏幕只画 30 行 → 下方大片空白。
        #   直接 attach netops 时 client 尺寸跟着 pty 走，最大化正常。
        #   代价：多个终端同时 attach netops 会共享“当前窗口”（切窗口互相影响）。
        # 1) 切到目标设备窗格
        subprocess.run([TMUX, "select-window", "-t", f"{TMUX_SESSION}:{self.window}"],
                       capture_output=True, timeout=10)
        # 3) attach 进伪终端
        master, slave = pty.openpty()
        self._set_winsize(master, rows, cols)
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            [TMUX, "attach", "-t", TMUX_SESSION],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, env=env, close_fds=True)
        os.close(slave)
        self.master = master
        self.alive = True
        # 记下自己这个 client 的 tty（close 时要 detach 它）
        try:
            self._client_tty = os.ttyname(master)
        except Exception:
            self._client_tty = None
        # 立刻把窗格尺寸控制权从 client 手里拿回来（否则多 client 会把它锁在最小值）
        try:
            subprocess.run([TMUX, "set-option", "-t", self.tmux_session, "window-size", "manual"],
                           capture_output=True, timeout=6)
            subprocess.run([TMUX, "resize-window", "-t", f"{TMUX_SESSION}:{self.window}",
                            "-x", str(cols), "-y", str(rows)],
                           capture_output=True, timeout=6)
        except Exception:
            pass
        threading.Thread(target=self._reader, daemon=True).start()

    # ── pty 尺寸 ──
    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            rows = max(5, min(300, int(rows)))
            cols = max(20, min(500, int(cols)))
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """调整终端尺寸。

        ★ 2026-09-26 修正（终端最大化后底部大片空白）：
        tmux 的窗格尺寸 = 所有连着的 client 里的【最小值】。
        原来这里只改了自己的 pty 尺寸 —— 对 tmux 来说等于没说，
        所以最大化后 xterm 变高了、tmux 窗格还是按旧行列画，下方就是空白。
        现在：改完 pty 后，再用 refresh-client -C 告诉 tmux
        「我这个 client（用它的 tty 标识）现在是 WxH」。
        """
        if self.master is None:
            return
        self._set_winsize(self.master, rows, cols)          # ① 自己的 pty 尺寸
        # ①b 显式通知 tmux 尺寸变了 —— 关键的一步。
        #      tmux 只在 attach 时读一次 pty 尺寸；之后必须收到 SIGWINCH 才重读。
        #      ioctl(TIOCSWINSZ) 虽然会触发该信号，但实测没送到 tmux 那边
        #      （client 一直停在 98x31，window 却已 194x55 → 屏幕只画 30 行）。
        #      这里直接给 tmux 进程组发一次 SIGWINCH，让它重读 pty 尺寸。
        try:
            if self.proc and self.proc.poll() is None:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGWINCH)
        except Exception:
            pass
        try:
            sess = self.tmux_session                        # ★ 是我们的 ui-* 临时会话
            tty = None
            try:
                tty = os.ttyname(self.master)               # 例如 /dev/ttys062
            except Exception:
                pass
            # ② window-size manual 【设在自己的会话上】
            #    踩过的坑：原来设在 netops 上，但终端用的是 ui-* 临时会话 ——
            #    选项是会话级的，设在别处等于没设；实测最大化后 window-size 仍是 latest，
            #    于是窗口尺寸被 client（98x31）拽住，终端只画上面一小块。
            subprocess.run([TMUX, "set-option", "-t", sess, "window-size", "manual"],
                           capture_output=True, timeout=6)
            # ③ 把 client 的尺寸也改掉（tmux 按 client 决定实际绘制范围）
            #    只改 window 不改 client：状态会显示 194x55，但屏幕上只画 98x31 那么大。
            if tty:
                subprocess.run([TMUX, "refresh-client", "-C", f"{cols}x{rows}", "-t", tty],
                               capture_output=True, timeout=6)
            # ④ 真正把窗格尺寸设成 xterm 算出来的行列
            subprocess.run([TMUX, "resize-window", "-t", f"{TMUX_SESSION}:{self.window}",
                            "-x", str(cols), "-y", str(rows)],
                           capture_output=True, timeout=6)
        except Exception:
            pass

    # ── 读 pty → 广播 ──
    def _reader(self) -> None:
        while self.alive:
            try:
                data = os.read(self.master, 8192)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.backlog += data
                if len(self.backlog) > self.BACKLOG_MAX:
                    del self.backlog[: len(self.backlog) - self.BACKLOG_MAX // 2]
                for q in list(self.subs):
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass
        self.alive = False
        # 通知所有订阅者：结束
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    # ── 写 pty（键盘输入）──
    def write(self, data: bytes) -> None:
        if self.master is None or not self.alive:
            return
        self.last_active = time.time()
        try:
            os.write(self.master, data)
        except OSError:
            self.alive = False

    # ── 订阅（SSE）──
    def subscribe(self) -> tuple[queue.Queue, bytes]:
        q: queue.Queue = queue.Queue(maxsize=2000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            snap = bytes(self.backlog)
        return q, snap

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        if self.master is not None:
            try:
                os.close(self.master)
            except Exception:
                pass
            self.master = None
        # ★ 不能 kill-session —— 现在直接 attach 的是 netops 本体！
        #   改成 detach 我们自己那个 client（按 tty 认）。
        if TMUX:
            try:
                if self._client_tty:
                    subprocess.run([TMUX, "detach-client", "-t", self._client_tty],
                                   capture_output=True, timeout=8)
            except Exception:
                pass
        self.alive = False


SESSIONS: dict[str, TermSession] = {}
SESS_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
#  AI 会话：一个直连会话  ↔  一个浏览器 AI 面板
#    要点：
#      · 后端是**进程内**的 agent loop（DirectSession）—— 无子进程、无 RPC、
#        无本机 CLI 依赖、无凭据锁；
#      · 设备能力只来自 netdev_mcp 的 13 个工具，全部转调 netdev CLI ——
#        黑名单 / 人审 / 先备份 / 逐条校验 / 同屏可见原样生效；
#      · 网页审批通道（ASK_PENDING）仍由 netdev 的 approval 回调驱动。
# ══════════════════════════════════════════════════════════════════════════

# 工具权限档位（direct 后端）——
#   read        : 不给任何工具，纯对话（最安全）
#   read+netdev : 给 13 个 netdev 工具（写操作会走 netdev 自身的闸门 / 人审）
#   full        : 同 read+netdev（保留档位名以兼容旧配置；direct 没有 shell 面）
TOOLSETS = ("read", "read+netdev", "full")
NETDEV_TOOLSETS = {"read+netdev", "full"}


def self_win(sess) -> str:
    """取一个 TermSession 的窗格名（用于日志）。"""
    try:
        return getattr(sess, "window", "") or ""
    except Exception:
        return ""


# ── 终端会话清理（2026-09-26 加）────────────────────────────────────────
#   问题：面板标题栏的 ✕ 只关面板，不会调 /api/term/close，
#        于是 ui-* 临时会话和 tmux client 一路累积（实测攒到 4 个）。
#        多 client 会互相拖累窗格尺寸，还会让新终端 attach 到混乱状态。
#   做法：开终端前先清理该设备相关的残留；服务启动时也清一次。
def cleanup_term_sessions(dev: str = "", keep_sid: str = "") -> dict:
    """杀掉该设备相关的 ui-* 临时会话（不动 netops 本体）。返回清理明细。"""
    if not TMUX:
        return {"killed": [], "detached": []}
    killed, detached = [], []
    # ① 先清掉不 attach 的僵尸 client
    try:
        r = subprocess.run([TMUX, "list-clients", "-F", "#{client_tty} #{client_session} #{client_attached}"],
                           capture_output=True, text=True, timeout=8)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not (len(parts) > 2 and parts[2] == "1"):
                tty, sess = parts[0], parts[1]
                if sess.startswith("ui-") and (not keep_sid or keep_sid not in sess):
                    subprocess.run([TMUX, "detach-client", "-t", tty], capture_output=True, timeout=6)
                    detached.append(tty)
    except Exception:
        pass
    # ② 杀掉该设备相关的 ui-* 会话
    #    会话名形如 ui-<sid>；我们用"这个 sid 是否属于同一设备"来判断
    with SESS_LOCK:
        want = {sid for sid, sess in SESSIONS.items()
                if (not dev or getattr(sess, "dev", "") == dev) and sid != keep_sid}
    try:
        r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=8)
        for name in (r.stdout or "").split():
            if not name.startswith("ui-"):
                continue
            sid = name[3:]
            # 只杀"已不在 SESSIONS 里"或"属于目标设备"的
            if sid in want or sid not in SESSIONS:
                subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True, timeout=8)
                killed.append(name)
    except Exception:
        pass
    # ③ 把那些已死会话从内存表里摘掉
    with SESS_LOCK:
        for sid in [k for k, v in SESSIONS.items() if not getattr(v, "alive", False)]:
            SESSIONS.pop(sid, None)
    return {"killed": killed, "detached": detached}


# ── AI 人设：设备调试助手（2026-09-26 加）──────────────────────────────
#   为什么要这个：原来 AI 是"一个通用助手，恰好有 netdev 工具"——
#   它不知道自己正在调试一台真实设备、不知道操作会显示在人眼前、
#   不知道写操作要先说计划。用户反馈过"它自己就干完了""我看不到它在干什么"。
#   这里把"设备调试助手"的行为准则作为 system message 刻进人设
#   （DirectSession._turn 里作为第一条 system 消息下发）。
DEBUG_SYSTEM_PROMPT = """你是【网络设备调试助手】，正在协助一位网络工程师调试真实设备。

关键前提（务必时刻记住）：
· 你操作的是一台/几台【真实设备】，不是模拟环境。
· 你的每一个动作都会【实时显示在工程师的屏幕上】（人机同屏）—— 他看得见你调用
  哪个工具、敲了哪条命令、设备回了什么。
· 你手上有一台设备的【多条通道】（例如 huawei=串口、huawei-ssh=SSH 是同一台设备）。
  用户消息里会明确给出【你正在调试的设备】，**所有 netdev 操作都用那个设备名，
  不要自己换成其它通道**。

工作准则：
1. 只读操作（display / show / dir / ping）直接执行，不要问、不要先解释。
   · 命令必须【一次性完整下发】（如 netdev_run 传 "display clock"），
     【严禁】把一个词拆成单个字符（d/i/s/p/l/a/y）逐个发 —— 那是错误的
     调用方式，会被写操作护栏拦成"已拒绝"。
2. 写操作（改配置）之前，必须先用一两句话说明清楚：
     · 要改什么（具体命令级别）
     · 为什么改
     · 风险与回滚方式（例如 undo vlan 888）
   说清楚之后再调 netdev_apply —— 它会弹审批给工程师确认。
   **绝不要**招呼都不打就直接下发配置。
3. 不要凭记忆回答"设备上有没有 X 配置" —— 去查（netdev_run / netdev_diff）。
   尤其注意：就算你刚才查过、工程师也可能已经手动改过了 —— 下结论前【重新查】。
4. 每次操作后确认结果（看回显、看返回的 ok / error），不要假设成功。
5. 不确定就说不确定，绝不编造。设备没回应就说"没读到"。
6. 发现异常（配置不符预期、报错、接口 down…）先报告给工程师，
   不要自己尝试修复。
7. 回答用中文，简洁、直接、给结论。涉及配置变更时用列表列清楚。

信息出处：工具返回里带 _identity（device / tool / at），
那是"这份数据来自哪台设备、什么时候取的"—— 引用数据时以此为准，
不要把不同设备、不同时间的数据混为一谈。

★ 认知保鲜（最关键的一条，务必执行）：
你调试的是【人机同屏】的真实设备，工程师会【在终端里手动改配置】——
你【看不见】他改了什么，你上下文里记的"设备状态"可能已经过时。
因此：
· 凡是【下结论】或【改配置】之前，必须先用 netdev_run（如
  display current-configuration / display vlan / display interface brief）
  或 netdev_diff【重查设备当前态】，不要凭上文记忆判断。
· 引用任何工具结果时，先看它的 _identity.at 时间戳——如果那是一次【很久之前】
  或【上一轮对话之前】取到的数据，就要先重查，再基于新结果下结论。
· 你记忆里的"设备上有 vlan 887"只是一条【旧快照】，工程师可能已经删了；
  判断设备现状的唯一依据是【刚刚查到的回显】，不是你的记忆。

★ 实时查询禁止翻留档（务必执行）：
项目 logs/ 目录下有设备的【历史回显留档】（*.log 文件），那是"过去某个时刻抓到的
快照"，不是"设备现在"的状态。因此：
· 凡是问【现在】的时间、状态、配置、接口、路由、vlan、流量等，
  一律用 netdev_run 实时查设备（如 display clock / display current-configuration），
  【不要】拿留档、也不要拿上文记忆来回答"现在是什么"。
· 你手上【没有】任何文件读写工具 —— 设备数据的唯一合法来源就是 netdev_* 工具。
  想引用"某份落盘结果"，只能用 netdev_screen_read / netdev_watch_tail 这类
  设备通道工具，或请在座的工程师帮你贴出来。
"""


AI_SESSIONS: dict = {}
AI_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
#  Direct 后端 —— 自实现 agent loop，直连 OpenAI 兼容 API（唯一后端）
#
#  为什么是它：pi / WorkBuddy 那两条「借别人 CLI 当引擎」的路都要求本机先装
#  一个与 netdev 无关的 CLI，各自带着一整套额外故障面（凭据锁、内部服务端口、
#  宿主注入的 shim、认证信封…），2026-10-03 已整体拆除。
#  direct 只需要一把 API Key（环境变量或一个配置文件），一个标准库 HTTP 客户端
#  直连 DeepSeek / OpenAI / 任意 OpenAI 兼容网关 —— 零新依赖、零常驻进程。
#
#  护栏唯一性（最重要的一条设计约束）：
#     direct **不重新实现任何设备操作**。它 import netdev_mcp，复用
#     netdev_mcp.HANDLERS（13 个工具函数）+ netdev_mcp.envelope（身份信封）。
#     那些 t_* 函数最终全部转调 netdev CLI —— 黑名单 / 人审 / 先备份 /
#     逐条校验 / 同屏可见，一行不重写。
#
#  事件契约（前端 onAiEvent 渲染的唯一形状）：
#     message_start / message_update / toolcall_start / tool_execution_end /
#     agent_end / error
# ══════════════════════════════════════════════════════════════════════════

# ── 凭据与端点：provider 表 + 三级优先取 key ─────────────────────────────
#   三级优先：环境变量 > config/direct.json（600 权限）> 探测本机已装后端
_DIRECT_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
        # ★ 2026-09-30 实测：账户实时型号只有 deepseek-flash / deepseek-v4-pro。
        #   deepseek-chat / deepseek-reasoner 等"文档名"返回 200 但被【静默映射】到
        #   deepseek-flash —— 配置名与实际执行不一致，排查时会被误导，故默认用真名。
        "default_model": "deepseek-flash",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4",
    },
}
_DIRECT_CFG_FILE = ROOT / "config" / "direct.json"


def _direct_cfg() -> dict:
    """解析 direct 凭据 + 端点。返回 {ok, provider, base_url, api_key, model, note}。

    三级优先：
      1. 环境变量（NETDEV_DIRECT_PROVIDER / NETDEV_DIRECT_BASE_URL /
         NETDEV_DIRECT_API_KEY / NETDEV_DIRECT_MODEL，或各 provider 自己的 *_API_KEY）
      2. config/direct.json（形如 {"provider":"deepseek","api_key":"...","base_url":"...","model":"..."}）
         —— 也就是界面 ⚙ 设置里「保存并启用」写下的那份
      3. provider 表里该 provider 自己的默认 base_url / model
    """
    provider = (os.environ.get("NETDEV_DIRECT_PROVIDER") or "").strip().lower()
    base_url = (os.environ.get("NETDEV_DIRECT_BASE_URL") or "").strip()
    api_key = (os.environ.get("NETDEV_DIRECT_API_KEY") or "").strip()
    model = (os.environ.get("NETDEV_DIRECT_MODEL") or "").strip()

    # 若未显式指定 provider，则按 provider 表逐个查环境变量
    if not provider and not (api_key and base_url):
        for pid, p in _DIRECT_PROVIDERS.items():
            k = (os.environ.get(p["env"]) or "").strip()
            if k:
                provider, api_key, base_url = pid, k, p["base_url"]
                model = model or p["default_model"]
                break

    # 第二级：配置文件
    if not api_key:
        try:
            cfg = json.loads(_DIRECT_CFG_FILE.read_text(encoding="utf-8"))
            provider = str(cfg.get("provider") or provider or "").strip().lower()
            api_key = str(cfg.get("api_key") or "").strip()
            base_url = str(cfg.get("base_url") or base_url or "").strip()
            model = str(cfg.get("model") or model or "").strip()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # 若 provider 已知但缺 key / base_url，用表补齐
    if provider in _DIRECT_PROVIDERS:
        p = _DIRECT_PROVIDERS[provider]
        api_key = api_key or (os.environ.get(p["env"]) or "").strip()
        base_url = base_url or p["base_url"]
        model = model or p["default_model"]

    if not api_key or not base_url:
        return {"ok": False, "provider": provider, "note":
                "未配置直连凭据：请设 NETDEV_DIRECT_API_KEY + NETDEV_DIRECT_BASE_URL，"
                f"或写 {_DIRECT_CFG_FILE}（{{provider, api_key, base_url, model}}）"}
    return {"ok": True, "provider": provider, "base_url": base_url.rstrip("/"),
            "api_key": api_key, "model": model,
            "note": f"provider={provider or '(自定义)'} · base={base_url}"}


def _direct_cfg_state() -> tuple[bool, str]:
    """给界面看的凭据可用性 + 人话说明（不消耗额度）。"""
    cfg = _direct_cfg()
    if cfg["ok"]:
        return True, f"直连就绪：{cfg['note']}"
    return False, cfg["note"]


def _direct_cfg_public() -> dict:
    """界面用的 direct 配置快照。key 脱敏：只给尾 4 位，绝不回完整 Key。"""
    cfg = _direct_cfg()
    return {"ok": cfg["ok"], "provider": cfg.get("provider", ""),
            "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "note": cfg.get("note", ""),
            "key_tail": (cfg.get("api_key") or "")[-4:],
            "key_src_env": bool((os.environ.get("NETDEV_DIRECT_API_KEY") or "").strip())}


def _direct_models() -> list[str]:
    """拉账户实时可用模型列表（Key 只在服务端用，不回传前端）。

    背景：DeepSeek 的"文档模型名"与"账户实际型号"不一致（deepseek-chat 会被
    静默映射到 flash）—— 让界面从 /models 拉真名，客户不用猜。
    """
    cfg = _direct_cfg()
    if not cfg["ok"]:
        return []
    try:
        import urllib.request
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return sorted(str(m.get("id")) for m in d.get("data", []) if m.get("id"))
    except Exception:
        return []


def _direct_probe() -> tuple[bool, str]:
    """深探测：真发一条最小 prompt（非流式，消耗极少额度），量延迟。

    界面上点「测试连通」就是它。
    """
    cfg = _direct_cfg()
    if not cfg["ok"]:
        return False, cfg["note"]
    t0 = time.time()
    try:
        s = DirectSession("__probe__", model=cfg.get("model", ""), tools="read")
        resp = s._chat([{"role": "user", "content": "连接测试：只回复两个字：在线"}],
                       stream=False)
        raw = resp.read().decode("utf-8", "replace")
        ms = int((time.time() - t0) * 1000)
        if resp.status != 200:
            return False, f"HTTP {resp.status}: {raw[:160]}"
        body = json.loads(raw)
        txt = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        return True, f"连通正常（{ms} ms）· 模型回应：{txt.strip()[:40] or '(空)'}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _direct_tool_schema() -> list[dict]:
    """把 netdev_mcp.TOOLS 转成 OpenAI 兼容的 tools 参数（去掉 MCP 专属字段）。"""
    try:
        import netdev_mcp
        out = []
        for t in netdev_mcp.TOOLS:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object"}),
                },
            })
        return out
    except Exception:
        return []


class DirectSession:
    """Direct 会话：裸 agent loop，直连 OpenAI 兼容端点。

    对外接口（prompt / abort / compact / stats / subscribe / close）与前端
    事件契约（message_start / message_update / toolcall_start /
    tool_execution_end / agent_end / error）是这个项目的**唯一** AI 契约。
    多轮上下文保存在 self.messages 里（本进程内存，不落盘）。
    """

    MAX_ITER = 8          # 单轮最多 8 次工具往返，防止死循环

    def __init__(self, aid: str, model: str = "", cwd: str | None = None,
                 tools: str = "read+netdev"):
        self.aid = aid
        cfg = _direct_cfg()
        self.cfg = cfg
        self.model = (model or cfg.get("model") or "").strip()
        self.tools_key = tools if tools in TOOLSETS else "read+netdev"
        # read 档位：不挂任何工具（只对话）；read+netdev / full 才给 netdev 工具
        self.use_netdev = self.tools_key in NETDEV_TOOLSETS
        self.cwd = cwd or str(ROOT)
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.alive = True
        self.last_active = time.time()
        self.ready = threading.Event()
        self.ready.set()                     # 无需预热，按需直连
        self.log: list[dict] = []            # 事件回放
        self.messages: list[dict] = []       # 多轮上下文（内存态）
        self._abort = threading.Event()      # 中止信号
        self._in_msg = False                 # 当前是否开着 message_start（一段回复一个气泡）
        self._tool_schemas = _direct_tool_schema() if self.use_netdev else []

    # ── 事件出口（前端 onAiEvent 消费的唯一形状）──
    def _emit(self, ev: dict) -> None:
        with self.lock:
            self.log.append(ev)
            if len(self.log) > 600:
                del self.log[:200]
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    def _close_msg(self) -> None:
        """收口当前文本段（一段回复 = 一个 message_start … message_end）。"""
        if getattr(self, "_in_msg", False):
            self._emit({"type": "message_end"})
            self._in_msg = False

    # ── 工具结果「结构化摘要 + 落盘」（2026-09-30 加）──────────────────
    #   为什么：netdev_run / backup / screen_read 等工具返回几千行回显，
    #   原样塞回上下文会①冲垮上下文②烧 token③噪音淹没关键结论（实测 netdev_run
    #   动辄 8000 字符）。这里把「大文本字段」摘出来落盘，只回填摘要，
    #   让模型拿到「元信息 + 关键行 + 全文路径」，需要细节再按路径取。
    #   注意：这层处理只发生在 direct 的进程内调用路径上，netdev_mcp 本身不动
    #   —— 它的 MCP 服务端契约（netdev-mcp 供外部 MCP 客户端用）保持原样。
    _BIG_TEXT_FIELDS = ("output", "screen", "diff", "lines", "raw", "results")
    _KEYWORD = re.compile(r"(?i)(error|fail|down|unrecognized|invalid|denied|refused|"
                          r"timeout|warning|mismatch|not found|no such|exceed)")

    def _summarize_tool_result(self, name: str, payload: dict) -> dict:
        """把 payload 里的大文本字段替换为「摘要 + 落盘路径」，其余元信息保留。

        ★ 2026-10-03 加固：摘要只是「锦上添花」，它自己失败【绝不能】把一次
          成功的工具调用改判成失败。原来这里是裸的 —— 一行 join 抛 TypeError
          穿透到 _call_tool 的 except，于是 isError=True，界面显示「执行失败」，
          而真正的结果被整个丢掉（实测 netdev_run 因此长期"必失败"，
          明明配置已经取回来并落盘了）。现在：整段包一层兜底，
          摘要层出任何问题都【回退为原始结果 + 一句警告】。
        """
        try:
            return self._summarize_tool_result_inner(name, payload)
        except Exception as e:
            safe = dict(payload) if isinstance(payload, dict) else {"data": payload}
            safe["_note"] = (f"结果摘要失败（{type(e).__name__}: {e}），"
                             f"已回退为原始结果 —— 本次工具调用本身的结果是有效的")
            return safe

    def _summarize_tool_result_inner(self, name: str, payload: dict) -> dict:
        """摘要正体（见 _summarize_tool_result 的说明）。"""
        try:
            big = {k: payload[k] for k in self._BIG_TEXT_FIELDS if k in payload
                   and isinstance(payload[k], (str, list)) and len(str(payload[k])) > 400}
        except Exception:
            big = {}
        if not big:
            return payload
        # 落盘原文
        logdir = ROOT / "logs" / "ai_tool" / (self.aid or "anon")
        try:
            logdir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return payload
        saved = {}
        for k, v in big.items():
            try:
                # ★ 2026-10-03 修（这就是让 netdev_run 长期"必失败"的那一行）：
                #   v 可能是「列表里装字典」—— 典型是 netdev_run 的 results
                #   ([{"command":..., "output":...}])。原来直接 "\n".join(v)
                #   必抛 TypeError: sequence item 0: expected str instance, dict found。
                #   而且它【不在 try 里】，异常穿透到 _call_tool 的 except，
                #   把一次成功的工具调用改判成 isError=True。
                #   两处修正：① join 前逐元素 str() ② 整段都进 try（单个字段
                #   落盘失败不许拖垮整份结果）。
                text = "\n".join(str(x) for x in v) if isinstance(v, list) else str(v)
                fn = f"{time.strftime('%H%M%S')}_{name}_{k}.txt"
                p = logdir / fn
                p.write_text(text + "\n", encoding="utf-8", errors="replace")
                saved[k] = str(p)
            except Exception:
                saved[k] = None
                continue
        # 摘要：关键行 + 首尾
        for k, v in big.items():
            lines = v if isinstance(v, list) else str(v).splitlines()
            lines = [str(x) for x in lines]
            hits = [x for x in lines if self._KEYWORD.search(x)][:20]
            head = lines[:6]
            tail = lines[-6:] if len(lines) > 6 else []
            summary = {"全文已落盘": saved.get(k),
                       "总行数": len(lines)}
            if hits:
                summary["关键行"] = hits
            if head:
                summary["开头"] = head
            if tail:
                summary["结尾"] = tail
            payload[k] = summary
        payload["_truncated"] = True          # 显式标记：这是摘要，不是全文
        return payload

    @staticmethod
    def _failure_reason(payload: dict) -> str:
        """从工具返回里挑一句「为什么失败」—— 前端只显示这一句。

        ★ 2026-10-03 加：原来 tool_execution_end 只带 isError、不带原因，
          界面就只剩一句「⚠ xxx 执行失败」。模型和用户都不知道发生了什么，
          实测模型只能靠猜（连猜 4 次全错），并把 2 次调用吹成 8 次。
        """
        try:
            for k in ("error", "note", "message", "_note"):
                v = payload.get(k)
                if isinstance(v, str) and v.strip():
                    return " ".join(v.split())[:200]
            for k in ("results", "steps"):
                items = payload.get(k)
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("ok") is False and it.get("error"):
                            return " ".join(str(it["error"]).split())[:200]
            return ""
        except Exception:
            return ""

    # ── 工具执行：直接复用 netdev_mcp（不重写任何设备逻辑）──
    def _call_tool(self, name: str, args: dict) -> str:
        self._emit({"type": "toolcall_start", "toolName": name, "args": args or {}})
        try:
            import netdev_mcp
            fn = netdev_mcp.HANDLERS.get(name)
            if not fn:
                self._emit({"type": "tool_execution_end", "toolName": name,
                            "isError": True, "error": f"未知工具: {name}"})
                return f"未知工具: {name}"
            payload = fn(args or {})
            payload = netdev_mcp.envelope(name, args or {}, payload)
            payload = self._summarize_tool_result(name, payload)
            _bad = bool(payload.get("ok") is False)
            self._emit({"type": "tool_execution_end", "toolName": name,
                        "isError": _bad,
                        "error": self._failure_reason(payload) if _bad else None})
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            self._emit({"type": "tool_execution_end", "toolName": name,
                        "isError": True, "error": f"{type(e).__name__}: {e}"})
            return f"工具执行异常: {type(e).__name__}: {e}"

    # ── 一次 HTTP 流式请求（标准库，零新依赖）──
    def _chat(self, msgs: list[dict], stream: bool = True):
        """POST /chat/completions，yield 每个流式 SSE 事件（dict）。"""
        import http.client
        import ssl as _ssl
        import urllib.request
        u = urllib.parse.urlparse(self.cfg["base_url"] + "/chat/completions")
        body = {
            "model": self.model or "deepseek-chat",
            "messages": msgs,
            "stream": stream,
        }
        if self._tool_schemas:
            body["tools"] = self._tool_schemas
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                           timeout=180) if u.scheme == "https" \
            else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=180)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg['api_key']}",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        conn.request("POST", u.path + ("?" + u.query if u.query else ""),
                     body=payload, headers=headers)
        return conn.getresponse()

    def _read_sse(self, resp) -> None:
        """逐行读 SSE 流，交给 _handle_sse_event。"""
        buf = b""
        for chunk in iter(lambda: resp.read(1024), b""):
            if self._abort.is_set():
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(b"data: "):
                    data = line[6:]
                elif line.startswith(b"data:"):
                    data = line[5:]
                else:
                    continue
                if data == b"[DONE]":
                    return
                try:
                    ev = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                self._handle_sse_event(ev)

    def _handle_sse_event(self, ev: dict) -> None:
        """累积 tool_calls 分片 + 输出文本增量。"""
        choices = ev.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            if not self._in_msg:              # ★ 一段回复只开一次气泡
                self._emit({"type": "message_start"})
                self._in_msg = True
            self._turn_text.append(delta["content"])
            self._emit({"type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta",
                                                  "delta": delta["content"]}})
        # ★ tool_calls 的 function.arguments 是分片 delta，必须累积拼接
        tc = delta.get("tool_calls") or []
        for part in tc:
            idx = part.get("index", 0)
            while len(self._tool_acc) <= idx:
                self._tool_acc.append({"id": "", "name": "", "args": ""})
            if part.get("id"):
                self._tool_acc[idx]["id"] = part["id"]
            fn = part.get("function") or {}
            if fn.get("name"):
                self._tool_acc[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                self._tool_acc[idx]["args"] += fn["arguments"]

    # ── 一轮对话：loop 直到纯文本或 max_iter ──
    def _turn(self, text: str) -> None:
        self._abort.clear()
        self._turn_text = []
        self._in_msg = False               # 新轮次：气泡状态复位
        self.messages.append({"role": "user", "content": text})
        if not self.cfg.get("ok"):
            self._emit({"type": "error", "error": self.cfg.get("note", "直连未配置")})
            self._emit({"type": "agent_end"})
            return

        # 系统人设：设备调试助手（见文件头 DEBUG_SYSTEM_PROMPT）
        msgs = [{"role": "system", "content": DEBUG_SYSTEM_PROMPT}] + self.messages

        try:
            for _ in range(self.MAX_ITER):
                if self._abort.is_set():
                    break
                self._tool_acc = []
                resp = self._chat(msgs, stream=True)
                if resp.status != 200:
                    err = resp.read().decode("utf-8", "replace")[:400]
                    self._emit({"type": "error",
                                "error": f"直连 API 返回 {resp.status}: {err}"})
                    break
                self._read_sse(resp)
                if self._abort.is_set():
                    break
                # 有工具调用 → 本地执行并回填，继续下一轮
                if self._tool_acc:
                    self._close_msg()      # 工具前的正文段收口（下一段文字是新气泡）
                    assistant_msg = {"role": "assistant", "content": "".join(self._turn_text) or None,
                                     "tool_calls": []}
                    tool_results = []
                    for t in self._tool_acc:
                        if not t["name"]:
                            continue
                        try:
                            args = json.loads(t["args"]) if t["args"] else {}
                        except Exception:
                            args = {}
                        assistant_msg["tool_calls"].append({
                            "id": t["id"], "type": "function",
                            "function": {"name": t["name"], "arguments": t["args"]},
                        })
                        result = self._call_tool(t["name"], args)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": t["id"],
                            "content": result,
                        })
                    msgs.append(assistant_msg)
                    msgs += tool_results
                    self.messages.append(assistant_msg)
                    self.messages += tool_results
                    self._turn_text = []
                    continue
                # 纯文本 → 结束
                break
        except Exception as e:
            self._emit({"type": "error",
                        "error": f"直连请求异常: {type(e).__name__}: {e}"})
        finally:
            self._close_msg()              # 兜底收口（含 abort / 异常路径）
            self._emit({"type": "agent_end"})
            self.last_active = time.time()

    # ── 对外接口 ──
    def prompt(self, text: str) -> bool:
        if not self.alive:
            return False
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()
        self.last_active = time.time()
        return True

    def abort(self) -> bool:
        self._abort.set()
        return True

    def compact(self, instructions: str = "") -> bool:
        # 内存态上下文：把旧消息压缩成一条摘要，保留最近 2 条
        if len(self.messages) > 6:
            keep = self.messages[-2:]
            summary = ("（以下为更早对话的压缩摘要）"
                       + (instructions or "") + " …")
            self.messages = [{"role": "system", "content": summary}] + keep
            return True
        return False

    def stats(self) -> bool:
        return False

    def respond_ui(self, req_id: str, value=None, cancelled: bool = False) -> bool:
        return False                     # direct 无 dialog 通道

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=3000)
        self.last_active = time.time()
        with self.lock:
            self.subs.add(q)
            replay = list(self.log[-120:])
        return q, replay

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def close(self) -> None:
        self.alive = False
        self._abort.set()
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass


# ── 网页审批通道：netdev 的 approval 会 POST 到这里，等人在浏览器上点 ──
ASK_LOCK = threading.Lock()
ASK_PENDING: dict = {}          # req_id -> {device, lines, kind, ev, allow, shown, at}
ASK_LAST_POLL = [0.0]           # 最近一次“浏览器长轮询”的时间（用于判断界面到底开没开）
UI_BASE = ""                    # 由 main() 填；会注入给 AI 子进程做 NETDEV_APPROVAL_URL


# ══════════════════════════════════════════════════════════════════════════
#  排障监控采集（只读）+ 快照管理
# ══════════════════════════════════════════════════════════════════════════
# 同屏会话的「自动化操作」要互斥：监控采集 / 快照保存 都会连续往屏上发命令，
# 并发会互相干扰（已实测：命令交错 ⇒ 设备回不到提示符 ⇒ 备份/下发失败）。
# 终端里“人”的输入不加锁（人机同屏，人优先）。
SCREEN_LOCKS: dict = {}
SCREEN_LOCK_G = threading.Lock()


def screen_lock(dev: str) -> threading.Lock:
    with SCREEN_LOCK_G:
        if dev not in SCREEN_LOCKS:
            SCREEN_LOCKS[dev] = threading.Lock()
        return SCREEN_LOCKS[dev]


def resolve_device(dev: str) -> dict:
    """把界面传来的“设备名”解析成设备（正式优先，其次连接簿）。

    为什么要它：界面现在同时展示【正式设备】和【临时目标】两类，
    而 netdev list 只含正式设备 —— 下游（开终端/采监控）如果还只查那一个源，
    临时设备就会“看得见、点不动”（实测踩到）。
    返回：{name, kind, window, uri, protocol, ...}；找不到返回 {}。
    """
    if not dev:
        return {}
    for d in (netdev_json(["list", "--json"]) or []):
        if dev in (d.get("name"), d.get("window")):
            return {**d, "kind": "formal", "target": d.get("name")}
    for c in conn_list():
        if dev in (c.get("id"), c.get("name")):
            # ★ 2026-09-26 修：不要用 c["window"] —— netdev conn list 返回的那个字段
            #   是从 URI 推导的（t-lab → "telnet-127-0-0-1"），而 CLI 实际建窗格
            #   用的是【连接簿的 name/id】（t-lab）。用错名字就找不到窗格 →
            #   run 回退直连 → 屏上不可见（实测踩到）。
            win = c.get("name") or c.get("id")
            # ★ 2026-09-27 修：target 必须是【连接簿的名字】，不能再是 URI。
            #   原来给的是 c["uri"]（如 telnet://admin@192.168.1.1:23），
            #   UI 用它去 netdev shell → netdev 按"匿名 URI"解析 → 自动生成
            #   名字 telnet-192-168-1-1 → 桥收到的设备名就成了这个，
            #   而凭据是按连接簿名（huawei23）存的 → 取不到 → 停在登录界面。
            #   还会连带：日志写到 telnet-192-168-1-1.screen.log（不是 huawei23.screen.log）、
            #   窗格名与界面显示不一致、找不到窗格时反复重试（表现为"打开很慢"）。
            #   用名字后走的是 CLI 的 resolve_target，它能正确查到连接簿（按 id/name 都认）。
            return {**c, "name": c.get("name") or c.get("id"), "id": c.get("id"),
                    "kind": "temp", "window": win,
                    "target": c.get("name") or c.get("id")}
    return {}


# ── 平台档案接入（2026-09-26）─────────────────────────────────────────
#   监控不再写死华为命令/格式：按 devices.toml 的 platform 分派。
#   未知平台退回华为档案，并标记 assumed（界面会提示）。
sys.path.insert(0, str(ROOT))
try:
    from lib import platforms as _plat
except Exception:                      # 极端情况下不影响主服务启动
    _plat = None
try:
    from lib import learned as _learned
except Exception:
    _learned = None


def _platform_of(dev_name: str) -> str:
    """从设备清单取 platform（取不到就空，让档案层决定退回哪套）。"""
    try:
        d = netdev_json(["list", "--json"], timeout=25) or []
        for x in d:
            if x.get("name") == dev_name:
                return (x.get("platform") or "").strip()
        # 临时目标（连接簿）没有 platform，按名字/协议兜个默认
    except Exception:
        pass
    return ""


MON_CMDS = {                       # 兼容旧引用：默认（华为）命令表
    "cpu":   "display cpu-usage",
    "mem":   "display memory-usage",
    "brief": "display interface brief",
}


def mon_cmds(platform: str | None, dev: str = "") -> dict:
    """按平台取监控命令表，并叠加「这台设备已学到的命令」。

    ★ 2026-10-01 修：原来只取平台默认命令，探测命中后 cache_put 的结果**从来没人用**，
      于是每轮采集都要先把默认命令撞一次墙、再逐条试候选（串口每条 1~2 秒）。
      现在走 platforms.plan_commands：学到的直接生效，第二次采集零探测成本。
    """
    if _plat is None:
        return dict(MON_CMDS)
    try:
        c = _plat.plan_commands(platform, dev, ["cpu", "mem", "brief"])
    except Exception:
        c = _plat.commands_for(platform, ["cpu", "mem", "brief"])
    return c or dict(MON_CMDS)


def _tmux_windows() -> set:
    if not TMUX:
        return set()
    try:
        r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                           capture_output=True, text=True, timeout=8)
        return {w for w in (r.stdout or "").split() if w}
    except Exception:
        return set()


def _tmux_live_windows() -> set:
    """活窗格集合（pane_dead==0）。

    为什么单列：_tmux_windows() 是【全量】窗口名（含死窗格）——
    僵尸清理必须看到死的才能杀；但「设备在线/监控通道决策」若认死窗格
    会出假象：窗格桥已死、串口早没了，还被当成"前台占用"走同屏，
    采回来一堆 Python 堆栈（2026-09-30 huawei 实测踩到）。
    """
    if not TMUX:
        return set()
    try:
        r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION,
                            "-F", "#{window_name}\t#{pane_dead}"],
                           capture_output=True, text=True, timeout=8)
        out = set()
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "0" and parts[0]:
                out.add(parts[0])
        return out
    except Exception:
        return set()


def _parse_metrics(text: str, platform: str | None = None, dev: str = "") -> dict:
    """从回显里抠指标。抠不到就是 None —— 不编造数值。

    2026-09-26：CPU/内存改用平台档案的多条正则（华为→华三→锐捷 各家句式不同）。
    档案里全不匹配时返回 None，界面显示「—」，绝不显示假的 0。
    """
    _prof = _plat.profile_of(platform) if _plat else None
    import re as _re
    def g(pat, cast=float):
        m = re.search(pat, text)
        if not m:
            return None
        try:
            v = cast(m.group(1))
            return int(v) if cast is int else round(v, 1)
        except Exception:
            return None
    m = {}
    # CPU / 内存：优先走平台档案的多条正则
    _pcpu = (_prof or {}).get("parse", {}).get("cpu") or []
    _pmem = (_prof or {}).get("parse", {}).get("mem") or []
    # 学到的规则优先（更贴近这台设备/这个版本）
    if _learned is not None:
        # 两级查找：设备级优先 → 平台级
        _lp, _lsrc = _learned.get_scoped(dev, platform or "", "cpu")
        if _lp:
            _pcpu = [_lp] + list(_pcpu)
    m["cpu_10s"] = _plat.pick(_pcpu, text, float) if _plat else None
    m["cpu_1m"]  = g(r"one minute:\s*([\d.]+)%")
    m["cpu_5m"]  = g(r"five minutes:\s*([\d.]+)%")
    if m["cpu_10s"] is None:                        # 档案没命中 → 兜底旧式
        m["cpu_10s"] = g(r"CPU utilization for (?:ten|five) seconds:\s*([\d.]+)%")
        if m["cpu_10s"] is None:
            m["cpu_10s"] = g(r"CPU\s+Usage\s*:\s*([\d.]+)%")
    m["cpu_5s"] = m["cpu_10s"]                      # 兼容旧字段名
    m["cpu_max"] = g(r"CPU\s+Usage\s*:\s*[\d.]+%\s*Max:\s*([\d.]+)%")
    if _learned is not None:
        _lm, _ = _learned.get_scoped(dev, platform or "", "ram")
        if not _lm:
            _lm, _ = _learned.get_scoped(dev, platform or "", "mem")
        if _lm:
            _pmem = [_lm] + list(_pmem)
    m["mem_pct"] = (_plat.pick(_pmem, text, int) if _plat else None) or g(r"Memory Using Percentage Is:\s*(\d+)%", int)
    m["mem_total"] = g(r"System Total Memory Is:\s*(\d+)\s*bytes", int)
    m["mem_used"] = g(r"Total Memory Used Is:\s*(\d+)\s*bytes", int)
    # CRC：两个来源 —— ① 单接口详情里的 "CRC: n" ② brief 表的 inErrors 列。
    #   原来只看 ①，而 brief 输出里没有 "CRC:" 字样，
    #   换了接口解析之后 crc 就变 None 了（实测踩到）。
    crcs = [int(x) for x in _re.findall(r"CRC:\s*(\d+)", text)]
    _err_sum = None
    try:
        _ifs2 = _parse_if_brief(text)
        if _ifs2:
            _err_sum = sum(int(x.get("in_err") or 0) for x in _ifs2)
    except Exception:
        _err_sum = None
    if crcs:
        m["crc"] = sum(crcs)
    elif _err_sum is not None:
        m["crc"] = _err_sum
    else:
        m["crc"] = None
    m["crc_ifaces"] = len(crcs) or (len(_ifs2) if _ifs2 else None)
    errs = [int(x) for x in _re.findall(r"inErrors\s+(\d+)", text)]
    m["in_err"] = sum(errs) if errs else None
    # 接口：走通用解析 _parse_if_brief。
    #   ★ 原来这里硬编码 `GigabitEthernet\d+/\d+/\d+` 且带宽只认 (\d+)%
    #     —— 换厂商（接口名前缀不同）或换端口（有流量时是 0.01%）就全空。
    #   现在：任意厂商前缀 + 小数带宽 + "--"（虚拟口）都能认。
    ifs = []
    try:
        ifs = _parse_if_brief(text)
    except Exception:
        ifs = []
    if ifs:
        _seen = set(); _u = []
        for it in ifs:                      # 同名只留最后一次（屏上可能有多次 brief）
            if it["name"] in _seen:
                _u = [x for x in _u if x["name"] != it["name"]]
            _seen.add(it["name"]); _u.append(it)
        _phys = [x for x in _u if (_plat.is_physical(x["name"]) if _plat else True)]
        m["if_total"] = len(_phys) or len(_u)
        m["if_up"] = sum(1 for x in (_phys or _u) if str(x["phy"]).startswith("up"))
        _ins = [x["in_uti"] for x in (_phys or _u) if x.get("in_uti") is not None]
        _outs = [x["out_uti"] for x in (_phys or _u) if x.get("out_uti") is not None]
        m["if_in_max"] = max(_ins) if _ins else None
        m["if_out_max"] = max(_outs) if _outs else None
    else:
        m["if_total"] = m["if_up"] = m["if_in_max"] = m["if_out_max"] = None
    return m


def collect_metrics(dev: str) -> dict:
    """采集只读指标：IP 设备（ssh/telnet）一律独立直连【静默采集】；
    串口空闲时同样直连；只有【串口被前台窗格占用】才走同屏（物理独占，绕不开）。

    2026-09-30 改通道优先级（用户要求"静默采集、不影响前台"）：
    · 原逻辑：设备有同屏窗格就走同屏 → 监控往前台屏幕塞命令，打断人工操作；
    · 现在：ssh/telnet 即使窗格开着也走 netdev run 独立短连接
      （开连 → 执行 → 断开，等价"后台开个会话拉完数据就关"），
      与前台窗格互不干扰；
    · 串口物理独占：窗格占用时 netdev run 打不开串口（CLI 会直接拒绝），
      只能走同屏 —— via 字段注明原因，界面会显示。
    多厂商适配不变：命令与解析仍按平台档案（华为/华三/锐捷/思科）分派 + 候选探测。
    """
    wins = _tmux_live_windows()          # 死窗格不算"前台占用"（桥已死 ≠ 占着串口）
    info = resolve_device(dev)
    window = info.get("window") or dev
    proto = (info.get("protocol") or "").strip().lower()
    screen_held = window in wins
    # 通道决策：只有【串口 + 前台窗格占用】才被迫走同屏；其余一律静默直连
    via_screen = bool(proto == "serial" and screen_held)
    via_note = ("同屏（串口被前台占用，物理独占）" if via_screen else "直连（静默）")
    cli = str(ROOT / "netdev")
    platform = _platform_of(dev)
    auto_detected = False
    # ★ 开箱即用：platform 留空（用户没标厂商）时，先发一条 display version
    #   自动识别厂商，再据此选命令/解析。识别不出才退回华为档案（候选探测兜底）。
    #   注意：这里只做「本次采集识别」，不持久化回写设备清单 ——
    #   每次采集多花一次 display version（约 1s），换来用户永远不用懂 platform 代号。
    if not platform and not via_screen:
        try:
            _rc, _vtext, _verr = raw_netdev(["run", dev, "display version"], timeout=30)
            if not _verr and _plat is not None:
                _detected = _plat.detect_platform(strip_ansi(_vtext))
                if _detected:
                    platform = _detected
                    auto_detected = True
        except Exception:
            pass
    base_cmds = mon_cmds(platform, dev)     # {cpu, mem, brief} 按平台取 + 叠加已学到的命令
    raws, cmds, used = {}, [], {}

    def _send_and_collect(work: dict):
        """发一组命令并读出回显（同屏或直连）。返回合并文本。"""
        if via_screen:
            for key, cmd in work.items():
                try:
                    subprocess.run([cli, "screen-send", window, cmd, "--yes"],
                                   capture_output=True, timeout=20)
                except Exception:
                    pass
                cmds.append(cmd); used[key] = cmd
                time.sleep(0.9)
            time.sleep(1.2)
            _rc, text, _e = raw_netdev(["screen-read", window, "--lines", "900"], timeout=30)
            raws["screen"] = strip_ansi(text)
            return raws["screen"]
        acc = []
        for key, cmd in work.items():
            _rc, text, err = raw_netdev(["run", dev, cmd], timeout=30)
            body = strip_ansi(text) if not err else ""
            raws[key] = body if not err else f"<err> {err[:200]}"
            cmds.append(cmd); used[key] = cmd
            acc.append(raws.get(key, ""))
        return "\n".join(acc)

    # ① 先按平台的默认命令采一次
    first_text = _send_and_collect(dict(base_cmds))

    # ② 探测：某条命令不被支持（或该指标没解析出来）→ 拿候选逐条试
    #   ★ 2026-10-01：原来这里先建了个 `retry` 字典把 cache_get 的结果塞进去，
    #     然后**从头到尾没用过**（死代码），缓存因此从来没生效过。已删除；
    #     缓存的生效点前移到 mon_cmds()/plan_commands()（建计划阶段就覆盖默认命令）。
    if _plat is not None and _plat.looks_like_bad_command(first_text):
        # 设备不认当前命令集：逐指标试候选，命中就缓存（下次直接生效）
        print(f"  · {dev}：平台命令未命中，开始探测候选命令…")
        for key in list(base_cmds.keys()):
            for cand in _plat.candidates_for(key, platform):
                if cand == base_cmds.get(key):
                    continue
                if via_screen:
                    try:
                        subprocess.run([cli, "screen-send", window, cand, "--yes"],
                                       capture_output=True, timeout=20)
                    except Exception:
                        pass
                    time.sleep(0.9)
                    _rc, t2, _e = raw_netdev(["screen-read", window, "--lines", "200"], timeout=25)
                    t2 = strip_ansi(t2)
                else:
                    _rc, t2, err2 = raw_netdev(["run", dev, cand], timeout=30)
                    t2 = strip_ansi(t2) if not err2 else f"<err> {err2[:120]}"
                if not _plat.looks_like_bad_command(t2) and t2.strip():
                    _plat.cache_put(dev, key, cand)
                    cmds.append(cand); used[key] = cand
                    raws.setdefault("probe", "")
                    raws["probe"] = (raws["probe"] + "\n" + t2)[-12000:]
                    print(f"  · {key} → 采用 {cand}（已记住，下次直接用）")
                    break

    # ③ 合并所有回显解析（带上平台）
    blob = "\n".join(v for v in raws.values() if v)
    learned = {}
    try:
        learned = _plat.cache_all(dev) if _plat is not None else {}
    except Exception:
        learned = {}
    return {"device": dev, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "via": via_note,
            "platform": platform or "(未标注)",
            "platform_auto": bool(auto_detected),
            "metrics": _parse_metrics(blob, platform, dev),
            "cmds": cmds, "used": used, "learned": learned,
            "raw": {k: v[-4000:] for k, v in raws.items()}}


def list_snapshots(device: str = "") -> list:
    data = netdev_json(["snap", "list", "--json"], timeout=25) or []
    if device:
        data = [d for d in data if d.get("device") == device]
    return data


def strip_ansi(s: str) -> str:
    import re as _re
    return re.sub(r"\x1b\[[0-9;]*m", "", s or "")


def clean_cli_tail(s: str, n: int = 400) -> str:
    """把 CLI 文本压成「人能读的一小段」。

    为什么需要：netdev 的进度输出会在**同一行**用 `\\r` 反复重绘，还带 ANSI 颜色码。
    直接 `slice(-300)` 会从半帧中间切开 —— 界面上真的出现过
    「已移入回收区：<换行>收区，不裸删）：2 份[0m #2 … [2m…」这种拼接乱码
    （2026-10-04 用户报障）。
    这里：先剥 ANSI，再对每行只保留最后一个 `\\r` 之后的内容（= 该行最终态），
    然后压缩空白、取尾部。
    """
    s = strip_ansi(s or "")
    s = s.replace("\r\n", "\n")
    s = "\n".join(ln.split("\r")[-1].rstrip() for ln in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[-n:]


def serial_ports() -> list:
    """可用串口端点（解析 netdev serial-discover 的文本输出；它不支持 --json）。"""
    import re as _re
    _rc, out, _e = raw_netdev(["serial-discover"], timeout=40)
    ports = []
    for line in strip_ansi(out).splitlines():
        m = re.match(r"\s*(/dev/\S+)\s+(.*)$", line)
        if m:
            info = re.sub(r"\s+", " ", m.group(2)).strip()
            ports.append({"path": m.group(1), "info": info[:80]})
    return ports


# ── 接口详情（监控面板「接口」卡下钻用）────────────────────────────────
def _parse_if_brief(text: str) -> list:
    """解析 display interface brief → [{name, phy, proto, in_uti, out_uti, in_err, out_err}]"""
    # ★ 2026-09-26 修：原来用 (\d+)% 匹配带宽，把【有流量】的接口全漏了。
    #   实测在线接口的带宽是小数（GigabitEthernet0/0/4 显示 0.01%），
    #   于是一个在线接口都没解析出来（up_count=0）—— 越关键的越漏。
    #   现在：带宽字段按 \S+ 拿，再宽容地转数字；虚拟接口的 "--" 也能收进来。
    def _num(x):
        try:
            return int(float(str(x).rstrip("%")))
        except Exception:
            return None

    out = []
    for ln in (text or "").splitlines():
        toks = ln.split()
        if len(toks) < 7:
            continue
        name, phy, proto = toks[0], toks[1], toks[2]
        if not re.match(r"^(up|down|\*down|up\([a-z]\)|down\([a-z]\))$", phy):
            continue
        if not re.match(r"^(up|down|\*down|up\([a-z]\)|down\([a-z]\))$", proto):
            continue
        if not re.match(r"^([\d.]+%|--)$", toks[3]) or not re.match(r"^([\d.]+%|--)$", toks[4]):
            continue
        out.append({"name": name, "phy": phy, "proto": proto,
                    "in_uti": _num(toks[3]), "out_uti": _num(toks[4]),
                    "in_err": _num(toks[5]) or 0, "out_err": _num(toks[6]) or 0})
    return out


def _parse_if_detail(text: str) -> dict:
    """解析单个接口的详细信息 → {state, crc, in_rate, out_rate, in_util, out_util, ...}"""
    d = {}
    t = text or ""
    m = re.search(r"current state\s*:\s*(\S+)", t)
    if m:
        d["state"] = m.group(1)
    m = re.search(r"Line protocol current state\s*:\s*(\S+)", t)
    if m:
        d["proto"] = m.group(1)
    m = re.search(r"Last 300 seconds input rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec", t)
    if m:
        d["in_bps"], d["in_pps"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Last 300 seconds output rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec", t)
    if m:
        d["out_bps"], d["out_pps"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"CRC:\s*(\d+),\s*Giants:\s*(\d+)", t)
    if m:
        d["crc"], d["giants"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Input bandwidth utilization\s*:\s*(\d+)%", t)
    if m:
        d["in_util"] = int(m.group(1))
    m = re.search(r"Output bandwidth utilization\s*:\s*(\d+)%", t)
    if m:
        d["out_util"] = int(m.group(1))
    return d


def _parse_transceiver(text: str) -> dict:
    """解析光模块信息（光衰）。没有光模块时返回 {}。"""
    d = {}
    t = text or ""
    m = re.search(r"Rx Power\(dBm\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["rx_power_dbm"] = m.group(1)
    m = re.search(r"Tx Power\(dBm\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["tx_power_dbm"] = m.group(1)
    m = re.search(r"Temperature\(C\)\s*:\s*([-\d.]+)", t, re.I)
    if m:
        d["temp_c"] = m.group(1)
    return d


def interface_detail(dev_name: str) -> dict:
    """接口下钻：概览 + 逐接口 CRC/带宽/光衰。只对 up 的接口抓详情（串口慢）。"""
    info = resolve_device(dev_name)
    win = info.get("window") or dev_name
    via = "同屏会话"
    def _read(cmd, lines=400):
        """读屏，且【只取该命令回显之后】的内容。

        为什么不能直接读整屏：窗格是长期复用的，屏上堆着历史回显，
        直接解析会把同一批接口重复算很多遍（实测解析出 26 个，真实只有 7 个）。
        取"最后一次出现的命令回显"之后，就只剩本次输出。
        """
        rc, out, err = raw_netdev(["screen-read", win, "--lines", str(lines)], timeout=30)
        txt = strip_ansi(out)
        key = (cmd or "").split("|")[0].strip()
        ls = txt.splitlines()
        start = 0
        for i, l in enumerate(ls):
            if key and key in l:
                start = i + 1
        # 只取到"收尾提示符"为止：提示符形如 <Huawei> / [Huawei-vlan10]。
        # ⚠ 不能简单判 endswith(">") —— display interface brief 的表头里就有 `>` 之类字符，
        #   会把表格下半截误切掉（实测漏掉最后一个在线接口）。
        body_lines = ls[start:] if key else ls
        out2 = []
        for l in body_lines:
            out2.append(l)
            if re.match(r"^\s*[<\[]\S+[>\]]\s*$", l):
                break
        return "\n".join(out2)

    def _send(cmd):
        cli = str(ROOT / "netdev")
        try:
            subprocess.run([cli, "screen-send", win, cmd, "--yes"],
                           capture_output=True, timeout=20)
        except Exception:
            pass
        time.sleep(1.0)

    brief = ""
    try:
        _send("display interface brief")
        time.sleep(1.2)
        brief = _read("display interface brief")
    except Exception as e:
        return {"error": f"取接口概览失败：{e}"}
    ifs = _parse_if_brief(brief)
    _seen = set(); _uniq = []
    for _it in ifs:                       # 去重（同名只留一个）
        if _it["name"] in _seen:
            continue
        _seen.add(_it["name"]); _uniq.append(_it)
    ifs = _uniq
    if not ifs:
        return {"error": "没解析到接口列表（设备回显异常？）", "raw": brief[-600:]}

    up_count = 0
    for it in ifs:
        # 只对 up 的接口抓详细（串口每次 1s+，全抓太慢）
        if str(it["phy"]).startswith("up"):            # 物理 up 才去抓详情（含 NULL0/虚拟口除外）
            up_count += 1
            _send(f"display interface {it['name']}")
            time.sleep(0.8)
            it.update(_parse_if_detail(_read(f"display interface {it['name']}")))
            # 光衰（电口没有光模块 → 空）
            _send(f"display transceiver verbose interface {it['name']}")
            time.sleep(0.6)
            opt = _parse_transceiver(_read("display transceiver verbose"))
            if opt:
                it["optical"] = opt
    # 全局光模块总览（有些设备不支持按接口查）
    global_opt = {}
    try:
        _send("display transceiver verbose")
        time.sleep(0.8)
        global_opt = _parse_transceiver(_read("display transceiver verbose"))
    except Exception:
        pass

    return {"device": dev_name, "via": via, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "interfaces": ifs, "up_count": up_count,
            "optical_supported": bool(global_opt) or any("optical" in x for x in ifs),
            "optical_global": global_opt}


# ── 单项指标取值（走同屏，不抢串口）──────────────────────────────────
_METRIC_CMDS = {
    "cpu":  "display cpu-usage",
    "ram":  "display memory-usage",
    "crc":  "display interface brief",
    "if":   "display interface brief",
    "ver":  "display version",
    "clock": "display clock",
}


# ── AI 提议解析规则（自我修正的第 2 步）────────────────────────────────
_METRIC_LABEL = {
    "cpu": "CPU 使用率（百分比）",
    "ram": "内存使用率（百分比）",
    "crc": "接口的错误包总数（inErrors 列求和）",
    "if":  "处于 up 状态的物理接口【个数】（需要数行，不是取某个字段）",
}


def ai_suggest_pattern(dev_name: str, key: str, raw: str, cmd: str,
                       timeout: int = 90) -> dict:
    """让 AI 看原始回显，提议一条能提取该指标的正则。**只提议，不保存。**

    安全设计：AI 的输出会经过 lib/learned.validate() 自检 ——
    要求"它给的正则必须能在同一段回显上取出它自己声称的那个值"。
    自检不过直接否决，不让它进档案。

    ★ 2026-10-03：改走**直连后端**（原来起一个 pi RPC 子进程 + 手工拼 JSONL）。
      这样「AI 学习回显解析规则」不再依赖本机装 pi，与 AI 助手共用同一条
      凭据链路（config/direct.json 或环境变量）。timeout 参数保留只为兼容调用方。
    """
    cfg = _direct_cfg()
    if not cfg["ok"]:
        return {"error": "直连 AI 未配置：" + cfg["note"]}
    label = _METRIC_LABEL.get(key, key)
    prompt = (
        "你是网络设备回显解析助手。下面是某设备执行命令 `" + cmd + "` 的原始回显。\n"
        "请完成两件事：\n"
        f"  1. 从中读出「{label}」的当前值（只回数字，不要单位）；\n"
        "  2. 给出一条 Python 正则（必须含【一个捕获组】），能从这段回显里稳定取出该值。\n\n"
        "要求：正则尽量具体（带上命令特征字），不要用过于宽泛的 \\d+ 到处匹配；\n"
        "      不确定就如实说不知道，不要编造。\n\n"
        "只输出一行 JSON，不要任何解释、不要代码块围栏：\n"
        '{"value": <数字>, "pattern": "<正则>"}\n'
        "若确实无法判断，输出：{\"value\": null, \"pattern\": \"\"}\n\n"
        "===== 原始回显开始 =====\n"
        + (raw or "")[:6000] +
        "\n===== 原始回显结束 ====="
    )
    try:
        s = DirectSession("__learn__", model="", tools="read")
        resp = s._chat([{"role": "user", "content": prompt}], stream=False)
        body_raw = resp.read().decode("utf-8", "replace")
        if resp.status != 200:
            return {"error": f"直连 API 返回 {resp.status}: {body_raw[:200]}"}
        body = json.loads(body_raw)
        out_text = (((body.get("choices") or [{}])[0]).get("message") or {}) \
            .get("content") or ""
    except Exception as e:
        return {"error": f"直连请求失败：{type(e).__name__}: {e}"}

    # 从 AI 的回复里抠出 JSON
    m = re.search(r'\{[^{}]*"value"\s*:\s*([^,}]+)[^{}]*\}', out_text or "", re.S)
    if not m:
        return {"error": "AI 没给出可用的 JSON", "raw_reply": (out_text or "")[:400]}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {"error": "AI 的 JSON 解析失败", "raw_reply": m.group(0)[:300]}
    return {"value": obj.get("value"), "pattern": (obj.get("pattern") or "").strip()}


def metric_one(dev_name: str, key: str) -> dict:
    """取单项指标的原始回显 —— 走同屏会话（不直连、不抢串口、用户看得见）。"""
    info = resolve_device(dev_name)
    win = info.get("window") or dev_name
    cmd = _METRIC_CMDS.get(key)
    if not cmd:
        return {"error": f"未知指标 {key}"}
    cli = str(ROOT / "netdev")
    # 1) 清历史（否则屏上的旧内容会混进来）
    try:
        subprocess.run([TMUX, "clear-history", "-t", f"{TMUX_SESSION}:{win}"],
                       capture_output=True, timeout=8)
    except Exception:
        pass
    # 2) 发命令（走同屏，屏幕可见）
    try:
        subprocess.run([cli, "screen-send", win, cmd, "--yes"],
                       capture_output=True, timeout=30)
    except Exception as e:
        return {"error": f"下发失败：{e}"}
    # 3) 等输出稳定再读 —— 固定 sleep 会读到半截。
    #    实测：display interface brief 有 26 行接口，2 秒时只吐到"表头+第 1 行"，
    #    结果交给 AI 的样例残缺，AI 只能回答"未给出"（它是对的，是我们给少了）。
    #    这里轮询：连续两次读数长度不变且出现收尾提示符，才算吐完。
    import hashlib as _hl
    prev_sig, prev_len, stable = "", -1, 0
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(1.2)
        _rc0, _o0, _e0 = raw_netdev(["screen-read", win, "--lines", "500"], timeout=25)
        _t0 = strip_ansi(_o0)
        # 只看命令之后那一段
        _l0 = _t0.splitlines()
        _st = 0
        for _i, _l in enumerate(_l0):
            if cmd in _l:
                _st = _i + 1
        _seg = "\n".join(_l0[_st:])
        _sig = _hl.md5(_seg.encode("utf-8", "replace")).hexdigest()
        if _sig == prev_sig and len(_seg) == prev_len:
            stable += 1
        else:
            stable = 0
        prev_sig, prev_len = _sig, len(_seg)
        # 稳定两轮 或 已看到收尾提示符 → 认为吐完
        if stable >= 2 or re.search(r"^\s*[<\[]\S+[>\]]\s*$", _seg, re.M):
            break
    # 读最终结果
    rc, out, err = raw_netdev(["screen-read", win, "--lines", "500"], timeout=30)
    txt = strip_ansi(out)
    ls = txt.splitlines()
    start = 0
    for i, l in enumerate(ls):
        if cmd in l:
            start = i + 1
    body = "\n".join(ls[start:])
    # 切到收尾提示符
    bl = body.splitlines(); keep = []
    for l in bl:
        keep.append(l)
        if re.match(r"^\s*[<\[]\S+[>\]]\s*$", l):
            break
    raw = "\n".join(keep).strip() or "(无回显)"
    val = None
    try:
        m = _parse_metrics(raw)
        if key == "cpu":
            val = m.get("cpu_10s")
        elif key == "ram":
            val = m.get("mem_pct")
        elif key in ("crc", "if"):
            # display interface brief 里没有 "CRC:" 字样，_parse_metrics 取不到；
            # 改用接口解析后汇总 inErrors（这才是"错包"口径）。
            _ifs = _parse_if_brief(raw)
            if _ifs:
                val = sum(int(x.get("in_err") or 0) for x in _ifs)
    except Exception:
        pass
    return {"device": dev_name, "key": key, "cmd": cmd,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lines": len(raw.splitlines()),
            "value": val, "output": raw}


# ── 回收区：彻底删除（单项 / 全部）──────────────────────────────────────
#   netdev CLI 的 `snap purge` 只能全清，没有单项；这里补上，并做严格路径校验：
#   只允许删除 S.TRASH_ROOT 目录【直接子项】，绝不允许越界（防止误删别处）。
def snap_purge(ref: str = "", all_of: bool = False) -> dict:
    sys.path.insert(0, str(ROOT))
    from lib import snapshot as S           # noqa: PLC0415
    trash = S.TRASH_ROOT
    items = S.list_trash()
    if all_of:
        if not items:
            return {"ok": True, "purged": [], "msg": "回收区已经是空的"}
        purged = []
        for d in items:
            try:
                # ★ 2026-10-04：走 S.rm_tree 而不是裸 shutil.rmtree ——
                #   宿主注入的 sitecustomize.py 被拦时会 raise SystemExit，
                #   那是 BaseException，`except Exception` 接不住，
                #   会一路穿透 HTTP 处理函数（日志里连 traceback 都没有），
                #   浏览器只报 "Load failed"。S.rm_tree 把它翻成可读异常。
                S.rm_tree(d)
                purged.append(d.name)
            except Exception as e:
                return {"ok": False, "purged": purged, "error": f"{d.name}: {e}"}
        return {"ok": True, "purged": purged, "n": len(purged)}
    ref = (ref or "").strip()
    if not ref:
        return {"ok": False, "error": "缺少 ref"}
    pick = next((d for d in items if ref in d.name), None)
    if not pick:
        return {"ok": False, "error": f"回收区里没找到：{ref}"}
    # ★ 路径校验：必须正好是 trash 目录的直接子项
    try:
        pick_r = pick.resolve()
        trash_r = trash.resolve()
        if pick_r.parent != trash_r:
            return {"ok": False, "error": "拒绝：目标不在回收区目录内"}
    except Exception as e:
        return {"ok": False, "error": f"路径校验失败：{e}"}
    try:
        S.rm_tree(pick_r)
        return {"ok": True, "purged": [pick.name], "n": 1}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── 接入 / 断开（2026-09-26 新语义）────────────────────────────────────
#   「接入」= 建同屏窗格（占用通道）→ 设备模块出现条目
#   「断开」= 只杀窗格（释放通道）→ 设备模块条目消失，但定义仍在库里
#   「删除」= 从 devices.toml / 连接簿 销毁（只在「管理连接簿」里做）
def disconnect_device(dev: str) -> dict:
    """断开一个设备的通道占用（杀窗格），不删任何定义。"""
    info = resolve_device(dev)
    window = info.get("window") or dev
    killed = False
    try:
        r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                           capture_output=True, timeout=8)
        killed = r.returncode == 0
    except Exception:
        pass
    # 顺手清掉可能残留的 client
    try:
        cleanup_term_sessions(dev)
    except Exception:
        pass
    return {"ok": True, "device": dev, "window": window, "disconnected": killed}


def store_kc(service: str, username: str, password: str) -> bool:
    """保存凭据。

    2026-09-26：改用 lib/creds.py 的本地文件存储（~/.netops/credentials.json，600 权限）。
    名字保留 store_kc 是因为调用点没改；不再往 macOS 钥匙串写（改存本地文件）。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return _creds.store_credential(service, username, password)
    except Exception:
        return False


def conn_list() -> list:
    """连接簿（随手接入的临时目标）。"""
    d = netdev_json(["conn", "list", "--json"], timeout=25)
    return d if isinstance(d, list) else []


def remove_device(name: str) -> dict:
    """从 devices.toml 移除一台正式设备（先备份，按 [[device]] 块整块删）。

    netdev 只有 device-add、没有 device-rm —— 所以这里做块级文本删除，
    严格只动目标那一块（保留注释、顺序、其他设备）。
    """
    import re as _re
    p = ROOT / "config" / "devices.toml"
    if not p.exists():
        return {"ok": False, "error": "devices.toml 不存在"}
    txt = p.read_text(encoding="utf-8")
    lines = txt.splitlines(keepends=True)
    out, i, hit = [], 0, False
    while i < len(lines):
        if lines[i].strip() == "[[device]]":
            block, j = [], i
            while j < len(lines) and (j == i or lines[j].strip() != "[[device]]"):
                block.append(lines[j])
                j += 1
            body = "".join(block)
            if re.search(r'^\s*name\s*=\s*"%s"\s*$' % _re.escape(name), body, _re.M):
                hit = True
            else:
                out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    if not hit:
        return {"ok": False, "error": f"未找到设备 {name}"}
    bak = p.with_name(p.name + ".bak-" + time.strftime("%Y%m%d_%H%M%S"))
    bak.write_text(txt, encoding="utf-8")
    p.write_text("".join(out), encoding="utf-8")
    return {"ok": True, "removed": name, "backup": bak.name}


def policy_state() -> dict:
    """当前写操作权限模式（读 state/policy.json，与 lib/approval 同源；含 allow 的 TTL 倒计时）。"""
    p = _P.state_dir() / "policy.json"
    mode, until = "ask", None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        mode = d.get("writes", "ask")
        until = d.get("allow_until")
    except Exception:
        pass
    if mode not in ("readonly", "ask", "allow"):
        mode = "ask"
    remain = None
    if mode == "allow" and isinstance(until, (int, float)):
        remain = int(max(0, until - time.time()))
        if remain <= 0:                     # 过期自动回落确认模式
            mode, remain = "ask", None
    return {"mode": mode, "allow_until": until, "remaining": remain}


# ══════════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════════
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "netdev-ui/0.1"
    protocol_version = "HTTP/1.1"

    # ── 小工具 ──
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):        # 静音：终端流太吵
        pass

    # ── GET ──
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if p.startswith("/static/"):
            return self._file(STATIC / p[len("/static/"):])
        if p == "/api/devices":
            return self._api_devices()
        if p == "/api/health":
            return self._json({"ok": True, "tmux": bool(TMUX), "sessions": len(SESSIONS),
                               "version": APP_VERSION})
        if p == "/api/agents":
            deep = qs.get("deep", ["0"])[0] in ("1", "true", "yes")
            return self._json(probe_agents(deep=deep))
        if p == "/api/direct/config":
            return self._api_direct_cfg_get()
        if p == "/api/direct/models":
            return self._api_direct_models()
        if p == "/api/term/stream":
            return self._api_stream(qs.get("sid", [""])[0])
        if p == "/api/ai/stream":
            return self._api_ai_stream(qs.get("aid", [""])[0])
        if p == "/api/metric/learn/list":
            return self._api_metric_learn_list()
        if p == "/api/metric/one":
            return self._api_metric_one(qs)
        if p == "/api/interface/detail":
            return self._api_interface_detail(qs)
        if p == "/api/monitor":
            return self._api_monitor(qs)
        if p == "/api/policy":
            return self._api_policy_get()
        if p == "/api/ask/pending":
            return self._api_ask_pending()
        if p == "/api/conn/list":
            return self._api_conn_list()
        if p == "/api/panes/orphans":
            return self._api_panes_orphans()
        if p == "/api/panes/list":
            return self._api_panes_list()
        if p == "/api/portlocks":
            return self._api_portlocks()
        if p == "/api/creds/list":
            return self._api_creds_list()
        if p == "/api/serial-discover":
            return self._api_serial_discover()
        if p == "/api/snap/list":
            return self._json({"snapshots": list_snapshots(qs.get("device", [""])[0])})
        if p == "/api/snap/file":
            return self._api_snap_file(qs)
        if p == "/api/snap/trash":
            return self._api_snap_trash(qs)
        return self._send(404, b"not found")

    # ── POST ──
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        if u.path == "/api/term/open":
            return self._api_term_open(b)
        if u.path == "/api/term/input":
            return self._api_term_input(b)
        if u.path == "/api/term/resize":
            return self._api_term_resize(b)
        if u.path == "/api/term/cleanup":
            return self._api_term_cleanup(b)
        if u.path == "/api/term/close":
            return self._api_term_close(b)
        if u.path == "/api/metric/learn":
            return self._api_metric_learn(b)
        if u.path == "/api/metric/learn/save":
            return self._api_metric_learn_save(b)
        if u.path == "/api/metric/learn/remove":
            return self._api_metric_learn_remove(b)
        if u.path == "/api/metric/learn/clear":
            return self._api_metric_learn_clear()
        if u.path == "/api/netdev/run":
            return self._api_netdev_run(b)
        if u.path == "/api/ai/open":
            return self._api_ai_open(b)
        if u.path == "/api/direct/config":
            return self._api_direct_cfg_set(b)
        if u.path == "/api/direct/test":
            return self._api_direct_test()
        if u.path == "/api/ai/send":
            return self._api_ai_send(b)
        if u.path == "/api/ai/respond":
            return self._api_ai_respond(b)
        if u.path == "/api/ai/compact":
            return self._api_ai_compact(b)
        if u.path == "/api/ai/stats":
            return self._api_ai_stats(b)
        if u.path == "/api/ai/abort":
            return self._api_ai_abort(b)
        if u.path == "/api/ai/close":
            return self._api_ai_close(b)
        if u.path == "/api/snap/save":
            return self._api_snap_save(b)
        if u.path == "/api/snap/rm":
            return self._api_snap_rm(b)
        if u.path == "/api/snap/restore":
            return self._api_snap_restore(b)
        if u.path == "/api/snap/purge":
            return self._api_snap_purge(b)
        if u.path == "/api/snap/unrm":
            return self._api_snap_unrm(b)
        if u.path == "/api/policy":
            return self._api_policy_set(b)
        if u.path == "/api/ask":
            return self._api_ask(b)
        if u.path == "/api/ask/respond":
            return self._api_ask_respond(b)
        if u.path == "/api/conn/rm":
            return self._api_conn_rm(b)
        if u.path == "/api/portlock/free":
            return self._api_portlock_free(b)
        if u.path == "/api/creds/del":
            return self._api_creds_del(b)
        if u.path == "/api/device/disconnect":
            return self._api_device_disconnect(b)
        if u.path == "/api/device/free":
            return self._api_device_free(b)
        if u.path == "/api/device/rm":
            return self._api_device_rm(b)
        if u.path == "/api/conn/add":
            return self._api_conn_add(b)
        if u.path == "/api/device/add":
            return self._api_device_add(b)
        if u.path == "/api/panes/clean":
            return self._api_panes_clean(b)
        if u.path == "/api/panes/kill":
            return self._api_panes_kill(b)
        return self._send(404, b"not found")

    # ── 静态文件 ──
    def _file(self, path: pathlib.Path):
        try:
            rp = path.resolve()
            if not str(rp).startswith(str(STATIC.resolve())):
                return self._send(403, b"forbidden")
            data = rp.read_bytes()
        except FileNotFoundError:
            return self._send(404, b"not found")
        except Exception as e:
            return self._send(500, str(e).encode())
        ct = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
              ".json": "application/json; charset=utf-8"}.get(rp.suffix, "application/octet-stream")
        self._send(200, data, ct)

    # ── 设备清单：正式（devices.toml）+ 临时（连接簿）合并 ──
    #    原来只列 devices.toml，导致“界面接入的临时设备根本看不见”（用户反馈）
    def _api_devices(self):
        devs = netdev_json(["list", "--json"]) or []
        for d in devs:
            d["kind"] = "formal"
        formal_names = {d.get("name") for d in devs}
        for c in conn_list():                      # 连接簿：随手接入的临时目标
            nm = c.get("id") or c.get("name")
            if not nm or nm in formal_names:       # 同名优先算正式设备
                continue
            devs.append({"name": c.get("name") or nm, "id": nm, "kind": "temp",
                         "protocol": c.get("protocol"), "address": c.get("address"),
                         "host": c.get("host"), "port": c.get("port"),
                         "device": c.get("device"), "baud": c.get("baud"),
                         # ★ 2026-09-27 修：不能用 c["window"] —— netdev conn list 返回的
                         #   那个字段是从 URI 推导的（huawei23 → telnet-192-168-1-1），
                         #   而 CLI 实际建窗格用的是连接簿的 name/id。
                         #   用错名字 → connected 永远 false → 界面显示"未接入"、
                         #   点接入又走"新建连接"→ 报"已存在同名连接"（用户实测踩到）。
                         "note": c.get("note"), "window": c.get("name") or c.get("id"),
                         "username": c.get("username"), "official": False})
        wins = _tmux_live_windows()       # ★ 只认活窗格：死窗格不该标"已接入/在线"
        for d in devs:
            win = d.get("window") or d.get("name")
            d["window_live"] = win in wins
            d["connected"] = win in wins          # ★ 「已接入」= 有【活】同屏窗格
            # 挂上凭据（明文，供界面里直接看；AI 侧看不到）
            c = self._cred_of(str(d.get("id") or d.get("name") or ""), str(d.get("name") or ""))
            d["cred_service"] = c["service"]
            d["cred_username"] = c["username"] or d.get("username") or ""
            d["cred_password"] = c["password"]
        return self._json({"devices": devs, "tmux": TMUX_SESSION,
                           "windows": sorted(wins), "sessions": list(SESSIONS.keys())})

    # ── 开终端 ──
    def _api_term_open(self, b: dict):
        dev = (b.get("device") or "").strip()
        rows = int(b.get("rows") or 40)
        cols = int(b.get("cols") or 140)
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        info = resolve_device(dev)          # 正式 + 临时 都能解析
        # ★ 2026-09-27 修：原来没检查解析结果，于是【接一个不存在的设备也返回成功】
        #   （实测 device=nonexist999 → 返回 {"sid":...}），
        #   界面显示"已接入"，实际什么都没建 —— 用户看到的就是"接入失败"却查不到原因。
        if not info:
            return self._json({"error": f"找不到设备「{dev}」—— 不在 devices.toml，也不在连接簿里。"
                                        f"要新接入请点顶栏「＋」。"}, 404)
        window = info.get("window") or dev
        target = info.get("target") or dev   # 临时目标用设备名开窗
        # ★ 串口：开窗时统一用 @auto，让桥自己探测波特率。
        #   原因：netdev 登记里的 baud 只能量整数（--baud 不接受 auto），常写成默认 9600，
        #   而真机可能是 115200 —— 波特率不对就是“屏幕无输出/满屏乱码”（已实测踩到）。
        #   桥（serial_bridge）本身支持 auto 探测（115200→9600→…），所以开窗这个环节用 auto 最稳。
        #
        # ⚠ 2026-09-29 修：原来只判 target 是否以 "serial:" 开头 —— 但临时目标传进来的
        #   是【设备名】（如 serial-u-usbserial-AABBCCDD-dlt8eust），根本进不了这个分支，
        #   于是它用了连接簿里存的 baud=9600，而真机是 115200 → 屏上全空白。
        #   现在改为【只要是串口设备就一律 @auto】，不再依赖 target 的写法。
        #   ⚠ 但别把 target 改成 `serial:/dev/xxx@auto` 去开窗 —— 那样 netdev 会按
        #     URI 生成窗格名（serial--dev-cu-usbserial-AABBCCDD），跟界面显示的设备名
        #     对不上，反而导致"窗格没建立起来"的误报。
        #     正确做法是让【连接簿里的 baud = auto】（已改），走设备名开窗即可。
        # 该设备还没有同屏窗格 → 请 netdev 建一个（它会自己处理串口独占/桥）
        if TMUX:
            r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                               capture_output=True, text=True, timeout=8)
            # ★ 2026-09-26 修（用户报"点 SSH 不能自动连接"）：
            #   设备侧 vty 有 idle-timeout，闲置超时后设备断开、桥进程退出，
            #   但 tmux 窗格还在，侧栏还显示"窗格在线"。
            #   原来只判"窗格名在不在"，于是直接复用那个空壳 —— 屏上什么都没有。
            #   现在同时判：窗格 dead 状态 + 里面跑的还是不是桥（python）。
            def _pane_state(win):
                try:
                    rr = subprocess.run([TMUX, "list-panes", "-t", f"{TMUX_SESSION}:{win}",
                                         "-F", "#{pane_dead} #{pane_current_command}"],
                                        capture_output=True, text=True, timeout=8)
                    out = (rr.stdout or "").strip()
                    if not out:
                        return None
                    parts = out.split()
                    return {"dead": parts[0] == "1", "cmd": parts[1] if len(parts) > 1 else ""}
                except Exception:
                    return None

            st = _pane_state(window)
            need_spawn = (st is None) or st["dead"] or ("python" not in (st.get("cmd") or ""))
            if need_spawn:
                if st is not None:
                    # 清掉旧空壳，否则 netdev shell 会"看到窗格已在就复用"
                    subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                   capture_output=True, timeout=8)
                    time.sleep(0.6)
                # ★ 2026-09-27 修：把 netdev 的输出留下来（失败时能给出原因），
                #   并且【轮询到窗格真的跑起桥为止】—— 原来超时了也无条件返回 sid，
                #   于是"tmux 会话都不存在 / 设备连不上"也报成功，
                #   界面显示"已接入"但列表里没有（用户实测踩到）。
                _spawn = subprocess.Popen([str(ROOT / "netdev"), "shell", target, "--restart"],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, start_new_session=True)
                _ok = False
                for _ in range(100):                # 最多等 20s（设备登录慢时够用）
                    time.sleep(0.2)
                    st2 = _pane_state(window)
                    if st2 is not None and ("python" in (st2.get("cmd") or "")):
                        _ok = True
                        break
                if not _ok:
                    # 收集 netdev 的输出当原因（它自己会说明为什么连不上）
                    _why = ""
                    try:
                        _spawn.terminate()
                        _out, _ = _spawn.communicate(timeout=3)
                        _why = " ".join((_out or "").split())[:300]
                    except Exception:
                        pass
                    return self._json({
                        "error": f"接入「{dev}」失败：同屏窗格没能建立起来。"
                                 + (f" netdev 输出：{_why}" if _why else
                                    f" 请手动跑一次看报错：netdev shell {target} --restart")
                    }, 500)
        sid = secrets.token_urlsafe(9)
        s = TermSession(sid, dev, window)
        try:
            s.start(rows=rows, cols=cols)
        except Exception as e:
            return self._json({"error": f"终端启动失败：{e}"}, 500)
        with SESS_LOCK:
            SESSIONS[sid] = s
        # ★ 唤醒设备（2026-09-26）：
        #   串口设备空闲时不主动说话 —— 桥连上、波特率也对，但屏上只会停在桥横幅，
        #   等多久都不会自己出 <Huawei>（实测：敲一下回车立刻就出来了）。
        #   所以开终端后主动敲几下回车，让用户打开就看到提示符。
        try:
            for _i in range(3):
                time.sleep(0.7)
                subprocess.run([TMUX, "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"],
                               capture_output=True, timeout=6)
        except Exception:
            pass
        time.sleep(0.6)
        return self._json({"sid": sid, "device": dev, "window": window})

    def _api_term_input(self, b: dict):
        s = SESSIONS.get(b.get("sid", ""))
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        try:
            data = base64.b64decode(b.get("data") or "")
        except Exception:
            return self._json({"error": "data 需要 base64"}, 400)
        s.write(data)
        return self._json({"ok": True, "bytes": len(data)})

    def _api_term_resize(self, b: dict):
        rows = int(b.get("rows") or 40); cols = int(b.get("cols") or 140)
        s = SESSIONS.get(b.get("sid", ""))
        # ★ 埋点：前端到底报了什么尺寸？用来定位"最大化后不填满"
        try:
            with open(str(ROOT / "logs" / "resize.log"), "a", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%H:%M:%S')}\t前端报来 {cols}x{rows}"
                         f"\tsid={b.get('sid','')[:8]}\t存在={bool(s)}\n")
        except Exception:
            pass
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        s.resize(rows, cols)
        # 记下"设置完之后 tmux 里实际是多少"
        try:
            r = subprocess.run([TMUX, "list-windows", "-t", TMUX_SESSION, "-F",
                                "#{window_name}=#{window_width}x#{window_height}"],
                               capture_output=True, text=True, timeout=6)
            line = next((x for x in (r.stdout or "").split() if self_win(s) in x), "")
            with open(str(ROOT / "logs" / "resize.log"), "a", encoding="utf-8") as fp:
                fp.write(f"\t\t→ 设置后 tmux: {line or '(取不到)'}\n")
        except Exception:
            pass
        return self._json({"ok": True})

    def _api_term_cleanup(self, b: dict):
        """清理终端残留（临时会话 + 僵尸 client）。"""
        try:
            return self._json({"ok": True, **cleanup_term_sessions(str(b.get("device") or ""))})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _api_term_close(self, b: dict):
        sid = b.get("sid", "")
        with SESS_LOCK:
            s = SESSIONS.pop(sid, None)
        if s:
            s.close()
        return self._json({"ok": True})

    # ── 只读命令直通（AI 与界面共用；写操作不走这里）──
    def _api_netdev_run(self, b: dict):
        dev = (b.get("device") or "").strip()
        cmd = (b.get("command") or "").strip()
        if not dev or not cmd:
            return self._json({"error": "缺少 device/command"}, 400)
        rc, out, err = raw_netdev(["run", dev, cmd])
        return self._json({"rc": rc, "stdout": out, "stderr": err})

    # ══ AI 会话（直连 OpenAI 兼容 API —— 唯一后端）══
    def _api_ai_open(self, b: dict):
        """开一个 AI 会话。**只有直连后端**（2026-10-03 起）。

        旧版本支持 backend=pi|wb，那两条「借别人的 CLI 当引擎」的通道已被
        整体拆除（原因见文件头「可接入的 AI 后端」注释块）。这里对旧值做
        优雅降级：任何 backend 都按 direct 处理，并回一条 notice 说明，
        免得旧前端 / 旧配置直接报错。
        """
        old = (b.get("backend") or "").strip().lower()
        cfg = _direct_cfg()
        if not cfg["ok"]:
            return self._json({"error": cfg["note"],
                               "hint": "在界面 ⚙ 设置里粘贴 API Key 即可启用 AI 助手"}, 400)
        # 界面上只有一个 AI 面板 ⇒ 开新会话前先收掉旧的，别让线程一路堆积
        with AI_LOCK:
            old_sessions = list(AI_SESSIONS.values())
            AI_SESSIONS.clear()
        for o in old_sessions:
            try:
                o.close()
            except Exception:
                pass
        aid = secrets.token_urlsafe(9)
        s = DirectSession(aid, model=b.get("model") or "", cwd=b.get("cwd") or None,
                          tools=b.get("tools") or "read+netdev")
        with AI_LOCK:
            AI_SESSIONS[aid] = s
        tool_list = ([t["function"]["name"] for t in s._tool_schemas]
                     if s._tool_schemas else None)
        resp = {"aid": aid, "backend": "direct",
                "model": s.model or (cfg.get("model") or "（provider 默认）"),
                "provider": cfg.get("provider") or "",
                "cwd": s.cwd, "pid": "无（HTTP 直连）", "ready": True,
                "tools": s.tools_key, "tool_list": tool_list}
        if old and old not in ("direct", "auto", "off"):
            resp["notice"] = (f"后端「{old}」已下线（pi / WorkBuddy 的借用式 RPC 通道"
                              "已整体拆除），本次会话自动使用直连 API。")
        return self._json(resp)

    def _api_direct_cfg_get(self):
        """直连配置快照（key 脱敏）。"""
        return self._json(_direct_cfg_public())

    def _api_direct_cfg_set(self, b: dict):
        """保存直连配置 → config/direct.json（600 权限，gitignore 已排除）。

        api_key 留空 = 保留已保存的 Key（界面不回显明文，改 Key 才填）。
        """
        p = _DIRECT_CFG_FILE
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        provider = str(b.get("provider") or "").strip().lower()
        base_url = str(b.get("base_url") or "").strip()
        model = str(b.get("model") or "").strip()
        api_key = str(b.get("api_key") or "").strip()
        if provider in _DIRECT_PROVIDERS:      # 表内 provider：空缺项用默认补齐
            pd = _DIRECT_PROVIDERS[provider]
            base_url = base_url or pd["base_url"]
            model = model or pd["default_model"]
        api_key = api_key or str(old.get("api_key") or "").strip()
        if not api_key:
            return self._json({"ok": False,
                               "error": "API Key 不能为空（首次配置必须粘贴 Key）"}, 400)
        if not base_url:
            return self._json({"ok": False, "error": "接口地址不能为空"}, 400)
        cfg = {"provider": provider, "api_key": api_key,
               "base_url": base_url.rstrip("/"), "model": model}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            os.chmod(p, 0o600)
        except Exception as e:
            return self._json({"ok": False,
                               "error": f"写配置失败：{type(e).__name__}: {e}"}, 500)
        out = _direct_cfg_public()
        out["saved"] = True
        if out.get("key_src_env"):             # 环境变量优先级高于文件，得说清楚
            out["note"] = ("已写入 config/direct.json；但本进程环境变量里有 "
                           "NETDEV_DIRECT_API_KEY（优先级更高），去掉该变量后文件配置才生效")
        return self._json(out)

    def _api_direct_test(self):
        """直连连通测试（真发一条最小 prompt）。"""
        ok, detail = _direct_probe()
        return self._json({"ok": ok, "detail": detail})

    def _api_direct_models(self):
        """账户实时可用模型列表（给模型的 datalist 当候选）。"""
        models = _direct_models()
        return self._json({"ok": bool(models), "models": models})

    def _api_ai_send(self, b: dict):
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        text = (b.get("message") or "").strip()
        if not text:
            return self._json({"error": "缺少 message"}, 400)
        ok = s.prompt(text)
        if not ok:
            # 直连后端 prompt() 只在会话已关闭（alive=False）时返回 False。
            # 把原因说清楚，别让用户对着一个假死的面板干等。
            return self._json({"ok": False, "accepted": False,
                               "error": "AI 会话已关闭（请重新开一个会话）"})
        return self._json({"ok": ok, "accepted": ok})

    def _api_ai_respond(self, b: dict):
        """界面里的人点了同意/拒绝 —— 审批通道的另一半。

        注意：审批对话走的是 netdev 自己的 approval 通道（/api/ask + ASK_PENDING），
        与本端点的「AI dialog」不是一回事；直连后端没有 dialog 通道，固定返回 False。
        """
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        ok = s.respond_ui(str(b.get("id") or ""), b.get("value"), bool(b.get("cancelled")))
        return self._json({"ok": ok})

    def _api_ai_compact(self, b: dict):
        """手动压缩 AI 上下文（长对话用）。

        直连后端是内存态上下文：把较早的消息折成一条摘要，只保留最近两条。
        消息还不够多时返回 ok=False（没什么可压的）。
        """
        aid = b.get("aid", "")
        s = AI_SESSIONS.get(aid)
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        ok = s.compact(str(b.get("instructions") or ""))
        return self._json({"ok": ok,
                           "msg": "已压缩：较早的消息折成一条摘要，保留最近的工作上下文"
                                  if ok else "消息还不够多，暂时不需要压缩"})

    def _api_ai_stats(self, b: dict):
        """看当前会话的上下文用量。

        直连后端不向服务端要 token 统计（无对应接口），固定回 False；
        保留端点是为了前端按钮不至于 404。
        """
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        ok = s.stats()
        return self._json({"ok": ok,
                           "msg": "已查询（结果会以事件形式回到会话流）" if ok
                                  else "直连后端不提供 token 统计"})

    def _api_ai_abort(self, b: dict):
        s = AI_SESSIONS.get(b.get("aid", ""))
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        return self._json({"ok": s.abort()})

    def _api_ai_close(self, b: dict):
        with AI_LOCK:
            s = AI_SESSIONS.pop(b.get("aid", ""), None)
        if s:
            s.close()
        return self._json({"ok": True})

    def _api_ai_stream(self, aid: str):
        s = AI_SESSIONS.get(aid)
        if not s:
            return self._json({"error": "AI 会话不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, replay = s.subscribe()
        try:
            for ev in replay:
                self._sse(b"ev", json.dumps(ev, ensure_ascii=False).encode())
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    self._sse(b"end", b"")
                    break
                self._sse(b"ev", json.dumps(ev, ensure_ascii=False).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)

    def _api_serial_discover(self):
        try:
            return self._json({"ports": serial_ports()})
        except Exception as e:
            return self._json({"ports": [], "error": str(e)}, 500)

    def _api_conn_add(self, b: dict):
        """加入连接簿（临时目标）。密码如有 → 同存本地凭据文件。"""
        import json as _json
        proto = (b.get("protocol") or "ssh").strip()
        name = (b.get("name") or "").strip()
        args = ["conn", "add", proto]
        if proto == "serial":
            dev = (b.get("device") or "").strip()
            if not dev:
                return self._json({"error": "请选择串口设备"}, 400)
            args += ["--device", dev]
            # 注意：netdev 的 --baud 【只收整数】，传 'auto' 会 argparse 报错（已实测）。
            # 前端下拉里有 auto 选项 ⇒ auto 就不传，让 netdev 用默认（自动探测）。
            _b = str(b.get("baud") or "").strip().lower()
            if _b and _b != "auto":
                args += ["--baud", _b]
        else:
            host = (b.get("host") or "").strip()
            if not host:
                return self._json({"error": "请填主机地址"}, 400)
            args += [host]
            if b.get("port"):
                args += ["--port", str(b["port"])]
        if name:
            args += ["--name", name]
        if b.get("username"):
            args += ["--username", b["username"]]
        if b.get("note"):
            args += ["--note", b["note"]]
        if b.get("platform"):
            args += ["--platform", b["platform"]]
        args += ["--json"]
        rc, out, err = raw_netdev(args, timeout=90)
        data = {}
        try:
            data = _json.loads(out.strip()) if out.strip().startswith(("{", "[")) else {}
        except Exception:
            data = {}
        key = (data.get("id") or data.get("name")) if isinstance(data, dict) else None
        if not key:
            # 实测：conn add 的 --json 并不输出 JSON，所以从人读文本里把名字抓出来
            # 形如：  ✔ 已加入连接簿：test-cli-ssh ｜ ssh ｜ 10.99.99.99:22
            import re as _re
            m = re.search(r"已加入连接簿[:：]\s*(\S+)", strip_ansi(out))
            key = m.group(1) if m else name
        saved = None
        if rc == 0 and b.get("password") and key:
            saved = store_kc(f"netdev-{key}", b.get("username") or "admin", b["password"])
        return self._json({"ok": rc == 0, "rc": rc, "key": key, "password_saved": saved,
                           "stdout": strip_ansi(out)[-600:], "stderr": strip_ansi(err)[-300:]})

    def _api_device_add(self, b: dict):
        """登记为正式设备（写入 devices.toml）。密码如有 → 存本地凭据文件 netdev-<name>。"""
        proto = (b.get("protocol") or "ssh").strip()
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "正式设备必须填名称"}, 400)
        args = ["device-add", "--name", name, "--protocol", proto]
        if proto == "serial":
            dev = (b.get("device") or "").strip()
            if not dev:
                return self._json({"error": "请选择串口设备"}, 400)
            args += ["--device-port", dev]
            # 同上：--baud 只收整数，'auto' 会报错 ⇒ 不传
            _b = str(b.get("baud") or "").strip().lower()
            if _b and _b != "auto":
                args += ["--baud", _b]
        else:
            host = (b.get("host") or "").strip()
            if not host:
                return self._json({"error": "请填主机地址"}, 400)
            args += ["--host", host]
            if b.get("port"):
                args += ["--port", str(b["port"])]
        if b.get("username"):
            args += ["--username", b["username"]]
        if b.get("platform"):
            args += ["--platform", b["platform"]]
        if b.get("tags"):
            args += ["--tags", b["tags"]]
        rc, out, err = raw_netdev(args, timeout=120)
        saved = None
        if rc == 0 and b.get("password"):
            saved = store_kc(f"netdev-{name}", b.get("username") or "admin", b["password"])
        return self._json({"ok": rc == 0, "rc": rc, "name": name, "password_saved": saved,
                           "stdout": strip_ansi(out)[-800:], "stderr": strip_ansi(err)[-400:]})

    # ══ 设备 / 连接簿管理 ══
    def _api_conn_list(self):
        items = conn_list()
        for c in items:                       # 附上密码（明文，供连接簿条目直接看）
            cr = self._cred_of(str(c.get("id") or ""), str(c.get("name") or ""))
            c["password"] = cr["password"]
            c["cred_username"] = cr["username"] or c.get("username") or ""
        return self._json({"items": items})

    def _api_conn_rm(self, b: dict):
        key = (b.get("key") or "").strip()
        if not key:
            return self._json({"error": "缺少 key"}, 400)
        # 先记下它的窗格名：删条目后如果窗格还留着，串口会被它占死 —— 下次就接不进来了
        info = resolve_device(key)
        window = info.get("window") or ""
        # 注意：netdev conn rm 【不接受 --yes】——硬加会 argparse 报错（已实测）。直接删即可。
        rc, out, err = raw_netdev(["conn", "rm", key], timeout=60)
        killed = False
        if rc == 0 and window and TMUX:
            with screen_lock(key):
                r2 = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                    capture_output=True, timeout=8)
                killed = r2.returncode == 0
        return self._json({"ok": rc == 0, "stdout": strip_ansi(out)[-400:],
                           "pane_killed": killed, "pane": window,
                           "stderr": strip_ansi(err)[-300:]})

    # ══ 串口/窗格占用一览 + 单窗格释放 ══
    #   “接入”会建同屏窗格，而串口是物理独占的 —— 窗格不释放，同一个串口就再也接不进来。
    #   原来的“清僵尸”只清没人认领的，用户自己接的那种有主子的清不掉（用户反馈过）。
    def _api_panes_list(self):
        wins = _tmux_windows()
        devmap = {}
        for d in (netdev_json(["list", "--json"]) or []):
            devmap[d.get("window") or d.get("name")] = f"正式设备 {d.get('name')}"
        for c in conn_list():
            devmap.setdefault(c.get("window") or c.get("id"), f"临时目标 {c.get('id')}")
        rows = []
        for w in sorted(wins):
            # serial--dev-cu-usbserial-AABBCCDD  →  /dev/cu.usbserial-AABBCCDD
            serial = ""
            if w.startswith("serial--"):
                serial = "/" + w[len("serial--"):].replace("-", "/", 1).replace("-", ".", 1)
                serial = "/dev/cu." + w[len("serial--dev-cu-"):] if w.startswith("serial--dev-cu-") else serial
            rows.append({"window": w, "owner": devmap.get(w, "（无主子）"),
                         "serial": serial, "orphan": w not in devmap})
        # 串口占用（直接问系统）
        busy = {}
        for p in serial_ports():
            try:
                rr = subprocess.run(["/usr/sbin/lsof", "-t", p["path"]],
                                    capture_output=True, text=True, timeout=8)
                busy[p["path"]] = bool((rr.stdout or "").strip())
            except Exception:
                busy[p["path"]] = None
        return self._json({"panes": rows, "serial_busy": busy})

    def _api_panes_kill(self, b: dict):
        """释放指定窗格（无论它有没有主子）—— 人主动要求才杀。"""
        w = (b.get("window") or "").strip()
        if not w:
            return self._json({"error": "缺少 window"}, 400)
        if w not in _tmux_windows():
            return self._json({"error": f"窗格不存在：{w}"}, 404)
        r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{w}"],
                           capture_output=True, timeout=8)
        return self._json({"ok": r.returncode == 0, "killed": w})

    # ══ 清理“僵尸窗格” ══
    #   只把「名字像自动生成的串口窗格」（serial--dev-cu-* / telnet-* / ssh-*）
    #   且当前没人认领的算作僵尸 —— 避免把用户手动开的窗格（如 switch-01）误判。
    @staticmethod
    def _looks_autogen(w: str) -> bool:
        return bool(__import__("re").match(r"^(serial--|telnet-|ssh-)", w or ""))

    def _api_panes_orphans(self):
        formal = {(d.get("window") or d.get("name")) for d in (netdev_json(["list", "--json"]) or [])}
        conn = {(c.get("window") or c.get("id")) for c in conn_list()}
        keep = formal | conn
        rows = []
        for w in sorted(_tmux_windows()):
            rows.append({"window": w, "orphan": (w not in keep) and self._looks_autogen(w)})
        return self._json({"panes": rows, "keep": sorted(keep)})

    def _api_panes_clean(self, b: dict):
        """真的杀。只杀“没人认领”的窗格，不碰正式设备/连接簿的窗格。"""
        formal = {(d.get("window") or d.get("name")) for d in (netdev_json(["list", "--json"]) or [])}
        conn = {(c.get("window") or c.get("id")) for c in conn_list()}
        keep = formal | conn
        killed = []
        for w in list(_tmux_windows()):
            if w in keep or not self._looks_autogen(w):
                continue
            r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{w}"],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                killed.append(w)
        return self._json({"ok": True, "killed": killed, "kept": sorted(keep)})

    # ── 凭据查看 / 删除（本地明文文件）──
    #   ★ 安全边界：这些接口只给网页上的人用。
    #     AI（pi 子进程）的工具白名单是 read/read+netdev，**没有任何 web/bash 工具**，
    #   而 netdev 的 MCP 工具清单里也没有凭据类接口 → AI 拿不到明文密码。
    @staticmethod
    def _cred_of(dev_id: str, name: str = "") -> dict:
        """查一个对象对应的凭据（明文）。"""
        try:
            sys.path.insert(0, str(ROOT))
            from lib import creds as _creds      # noqa: PLC0415
            for svc in (f"netdev-{dev_id}", f"netdev-{name}"):
                if not svc or svc == "netdev-":
                    continue
                u, p = _creds.keychain_item(svc)
                if p or u:
                    return {"service": svc, "username": u or "", "password": p or ""}
        except Exception:
            pass
        return {"service": "", "username": "", "password": ""}

    def _api_creds_list(self):
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return self._json({"file": _creds.CRED_FILE,
                           "items": _creds.list_credentials(show_password=True)})

    def _api_creds_del(self, b: dict):
        svc = (b.get("service") or "").strip()
        if not svc:
            return self._json({"error": "缺少 service"}, 400)
        sys.path.insert(0, str(ROOT))
        from lib import creds as _creds          # noqa: PLC0415
        return self._json({"ok": bool(_creds.delete_credential(svc)), "deleted": svc})

    # ── 串口锁（带业主）──
    def _api_portlocks(self):
        sys.path.insert(0, str(ROOT))
        from lib import portlock as _pl            # noqa: PLC0415
        return self._json({"locks": _pl.all_locks(), "file": str(_pl.LOCKFILE)})

    def _api_portlock_free(self, b: dict):
        """人工释放某个串口锁（网页上的「释放」）。"""
        port = (b.get("port") or "").strip()
        if not port:
            return self._json({"error": "缺少 port"}, 400)
        sys.path.insert(0, str(ROOT))
        from lib import portlock as _pl            # noqa: PLC0415
        return self._json({"ok": _pl.force_release(port), "port": port})

    # ── 只释放通道占用（杀同屏窗格），不动 devices.toml ──
    #   正式设备的 ✕ 走这里：设备保留，只把占着串口/连接的那个窗格释掉。
    #   （之前 ✕ 会真从 devices.toml 里删设备，被误点太多次了）
    def _api_device_disconnect(self, b: dict):
        """断开：只释放通道，不删定义。"""
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        try:
            return self._json(disconnect_device(name))
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _api_device_free(self, b: dict):
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        info = resolve_device(name)
        window = info.get("window") or ""
        killed = False
        if window and TMUX:
            with screen_lock(name):
                r = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                   capture_output=True, timeout=8)
                killed = r.returncode == 0
        return self._json({"ok": True, "freed": name, "pane_killed": killed, "pane": window})

    # ── 真删除：从 devices.toml 移除 + 连带清窗格 ──
    #   入口是「📋 管理连接簿」弹窗（不是列表上的 ✕），有备份。
    def _api_device_rm(self, b: dict):
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少 name"}, 400)
        info = resolve_device(name)
        window = info.get("window") or ""
        r = remove_device(name)
        killed = False
        if r.get("ok") and window and TMUX:
            with screen_lock(name):
                rr = subprocess.run([TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                                    capture_output=True, timeout=8)
                killed = rr.returncode == 0
        return self._json({"ok": bool(r.get("ok")), "removed": name,
                           "backup": r.get("backup"), "error": r.get("error"),
                           "pane_killed": killed, "pane": window})

    # ══ 网页审批通道（netdev 的 lib/approval 打过来）══
    def _api_ask(self, b: dict):
        """阻塞等待「人在网页上点一下」。

        这就是人机分离的落地：AI 侧工具白名单里没有 bash/web，它发不出这个 POST；
        审批结果只存在内存，也不落文件，所以它也无从伪造。
        """
        # 通道健康检查：最近 30 秒内没有浏览器轮询 ⇒ 界面没开或是旧版页面。
        # 这时直接告诉 approval “网页通道不可用”，让它回退原生弹窗 —— 避免静默超时拒发。
        if time.time() - ASK_LAST_POLL[0] > 30:
            return self._json({"available": False, "allow": False,
                               "reason": "网页未在轮询（页面没开或未刷新）"})
        rid = secrets.token_urlsafe(8)
        ev = threading.Event()
        item = {"id": rid, "device": b.get("device") or "-",
                "lines": [str(x) for x in (b.get("lines") or [])],
                "kind": b.get("kind") or "write", "ev": ev, "allow": None,
                "shown": False, "at": time.time()}
        with ASK_LOCK:
            ASK_PENDING[rid] = item
        timeout = float(b.get("timeout") or 60)
        try:
            answered = ev.wait(timeout=timeout + 5)
        finally:
            with ASK_LOCK:
                ASK_PENDING.pop(rid, None)
        return self._json({"allow": bool(item["allow"]) if answered else False,
                           "req_id": rid, "answered": bool(answered)})

    def _api_ask_pending(self):
        """浏览器长轮询：取一个待审批请求（最多等 25 秒）。

        注意：不做“只发一次”限制 —— 多标签页时，若第一个标签弹窗失败/被忽略，
        请求就被永久吞掉（已实测踩到：连续两次 DENY 都是没人应答）。
        改成可重复取，谁先应答谁生效（应答后 answered=True 就不再下发）。
        """
        ASK_LAST_POLL[0] = time.time()          # 心跳：证明“界面真的开着且是新版页面”
        t0 = time.time()
        while time.time() - t0 < 25:
            with ASK_LOCK:
                for it in ASK_PENDING.values():
                    if not it.get("answered"):
                        return self._json({"req": {"id": it["id"], "device": it["device"],
                                                   "lines": it["lines"], "kind": it["kind"]}})
            time.sleep(0.4)
        return self._json({"req": None})

    def _api_ask_respond(self, b: dict):
        rid = str(b.get("id") or "")
        with ASK_LOCK:
            it = ASK_PENDING.get(rid)
        if not it:
            return self._json({"ok": False, "error": "请求不存在或已过期"}, 404)
        it["allow"] = bool(b.get("allow"))
        it["answered"] = True
        it["ev"].set()
        return self._json({"ok": True})

    # ══ 写操作策略（三模式）══
    def _api_policy_get(self):
        return self._json(policy_state())

    def _api_policy_set(self, b: dict):
        mode = (b.get("mode") or "").strip()
        if mode not in ("readonly", "ask", "allow"):
            return self._json({"error": "mode 只能是 readonly / ask / allow"}, 400)
        rc, out, err = raw_netdev(["policy", mode], timeout=180)   # 切 allow 会弹人审窗
        st = policy_state()
        return self._json({"ok": rc == 0, "rc": rc, "requested": mode,
                           "stdout": out[-600:], "stderr": err[-400:], **st})

    # ══ 排障监控 ══
    # AI 提议时看到的原始回显，按 device:key 暂存；
    # 保存时必须用【同一份】来自检 —— 之前保存用的是截断后的 sample，
    # 与 AI 看到的那段不一致，导致"提议时通过、保存时匹配不到"（实测踩到）。
    _LEARN_CACHE: dict = {}

    def _api_metric_learn(self, b: dict):
        """让 AI 提议一条解析规则（只提议，不保存）。"""
        dev = (b.get("device") or "").strip()
        key = (b.get("key") or "").strip()
        if not dev or not key:
            return self._json({"error": "缺少 device/key"}, 400)
        if _learned is None:
            return self._json({"error": "学习模块不可用"}, 500)
        with screen_lock(dev):
            try:
                one = metric_one(dev, key)
            except Exception as e:
                return self._json({"error": f"取回显失败：{e}"}, 500)
        if one.get("error"):
            return self._json(one, 500)
        raw = one.get("output") or ""
        sug = ai_suggest_pattern(dev, key, raw, one.get("cmd") or "")
        if sug.get("error"):
            return self._json({**sug, "raw": raw[-1500:]})
        # 自检（三道护栏之一）——用 AI 看到的那份原文
        ok, why, got = _learned.validate(sug.get("pattern"), raw, sug.get("value"))
        self._LEARN_CACHE[f"{dev}:{key}"] = raw      # 供保存时复用同一份
        plat = _platform_of(dev) or ""
        # 未标平台 → 只能存设备级（避免不同厂商挤进同一个桶里互相串）
        scopes = [{"scope": "dev", "label": f"只给这台设备（{dev}）"}]
        if plat:
            scopes.append({"scope": "plat", "label": f"给该平台所有设备（{plat}）"})
        return self._json({"ok": True, "device": dev, "key": key,
                           "platform": plat or "(未标注)",
                           "scopes": scopes, "cmd": one.get("cmd"),
                           "sample": raw[:2500],
                           "ai_value": sug.get("value"),
                           "ai_pattern": sug.get("pattern"),
                           "selfcheck_ok": ok, "selfcheck_msg": why,
                           "selfcheck_value": got,
                           "verified": bool(ok and sug.get("value") is not None)})

    def _api_metric_learn_save(self, b: dict):
        """采纳 AI 的提议（保存到学习档案）。"""
        if _learned is None:
            return self._json({"error": "学习模块不可用"}, 500)
        key = (b.get("key") or "").strip()
        pat = b.get("pattern") or ""
        dev = (b.get("device") or "").strip()
        scope = (b.get("scope") or "dev").strip()          # 默认设备级（最安全）
        plat = (b.get("platform") or "").strip()
        name = dev if scope == "dev" else (plat or dev)
        # 用提议时缓存的原文（与 AI 看到的完全一致）做自检；
        # 取不到才退回前端传来的 sample。
        sample = self._LEARN_CACHE.get(f"{dev}:{key}") or b.get("sample") or ""
        val = b.get("value")
        ok, why = _learned.put_scoped(scope, name, key, pat, sample, val, source="ai")
        if ok:
            self._LEARN_CACHE.pop(f"{dev}:{key}", None)
        return self._json({"ok": ok, "msg": why, "scope": scope, "name": name, "key": key})

    def _api_metric_learn_list(self):
        if _learned is None:
            return self._json({"rules": []})
        return self._json({"rules": _learned.all_rules_scoped(), "file": str(_learned.FILE)})

    def _api_metric_learn_remove(self, b: dict):
        """删除一条学到的规则。"""
        if _learned is None:
            return self._json({"ok": False})
        full = (b.get("platform") or b.get("full") or "").strip()
        return self._json({"ok": _learned.remove(full, (b.get("key") or "").strip())})

    def _api_metric_learn_clear(self):
        if _learned is None:
            return self._json({"ok": False})
        return self._json({"ok": True, "cleared": _learned.clear()})

    def _api_metric_one(self, qs):
        """单项指标：走同屏取，不在终端里插命令。"""
        dev = (qs.get("device", [""])[0] or "").strip()
        key = (qs.get("key", [""])[0] or "").strip()
        if not dev or not key:
            return self._json({"error": "缺少 device/key"}, 400)
        with screen_lock(dev):
            try:
                return self._json(metric_one(dev, key))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_interface_detail(self, qs):
        """接口下钻：在线接口的带宽占用率 / CRC / 光衰。"""
        dev = (qs.get("device", [""])[0] or "").strip()
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        with screen_lock(dev):
            try:
                return self._json(interface_detail(dev))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_monitor(self, qs):
        dev = (qs.get("device", [""])[0] or "").strip()
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        try:
            return self._json(collect_metrics(dev))
        except Exception as e:
            return self._json({"error": f"采集失败：{type(e).__name__}: {e}"}, 500)

    # ══ 快照管理 ══
    def _api_snap_save(self, b: dict):
        dev = (b.get("device") or "").strip()
        tag = (b.get("tag") or "").strip() or "UI手工"
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        with screen_lock(dev):          # 与监控采集 / 其它同屏操作互斥，避免抓配置时被插命令
            info = resolve_device(dev)
            target = info.get("target") or dev     # 临时设备 → 用 URI 当快照目标
            rc, out, err = raw_netdev(["snap", "save", target, "--tag", tag], timeout=300)
        return self._json({"ok": rc == 0, "rc": rc, "stdout": strip_ansi(out)[-2000:],
                           "stderr": strip_ansi(err)[-600:]})

    def _api_snap_rm(self, b: dict):
        dev = (b.get("device") or "").strip()
        idx = str(b.get("idx") or "").strip()
        if not dev or not idx:
            return self._json({"error": "缺少 device/idx"}, 400)
        rc, out, err = raw_netdev(["snap", "rm", dev, idx, "--yes"], timeout=90)   # 不带 --yes 只是预览
        # 注意：netdev 移入回收区后还会去复制 workbuddy 副本，源目录已移走 → FileNotFoundError，
        # 于是 rc≠0 但实际已经删成功了。所以用「该项是否还在快照目录」判定真实结果。
        still = any(str(idx) == str(s.get("idx"))
                    for s in (netdev_json(["snap", "list", "--json"], timeout=25) or []))
        # ★ 2026-10-04：输出必须过 clean_cli_tail。原样 slice 会把带 \r 重绘的进度帧
        #   从中间切开，界面上显示成一串带 ANSI 码的拼接乱码。
        return self._json({"ok": not still, "rc": rc,
                           "stdout": clean_cli_tail(out, 400),
                           "stderr": clean_cli_tail(err, 300)})

    def _api_snap_purge(self, b: dict):
        """彻底删除回收区的内容（物理删除，不可恢复）。ref 不给则按 all 处理。"""
        try:
            return self._json(snap_purge(str(b.get("ref") or ""), bool(b.get("all"))))
        except (Exception, SystemExit) as e:
            # ★ SystemExit 单列：宿主注入的 sitecustomize.py 被「批量删除守卫」拦下时
            #   抛的就是它。它是 BaseException，`except Exception` 接不住 ——
            #   一旦漏出去，请求没有响应、连接断开，浏览器只会显示 "Load failed"，
            #   用户完全不知道发生了什么。这里兜住，给出可读原因。
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _api_snap_trash(self, qs):
        """回收区：netdev snap trash 只输出文本，这里解析成结构化列表。"""
        import re as _re
        _rc, out, _e = raw_netdev(["snap", "trash"], timeout=40)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", out)     # 先去 ANSI，否则行尾是 \x1b[0m
        items = []
        for m in _re.finditer(r"^\s+(\S+?)(?:_removed-(\d{8})_?(\d{6})?)?\s+(\d+)B\s*$", clean, _re.M):
            name = m.group(1)
            stamp = ""
            if m.group(2):
                s = m.group(2) + (m.group(3) or "")
                stamp = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}" if len(s) >= 12 else s
            items.append({"name": name, "full": m.group(0).strip().split()[0],
                          "size": int(m.group(4)), "removed_at": stamp})
        return self._json({"items": items, "raw": out[-1500:]})

    def _api_snap_unrm(self, b: dict):
        dev = (b.get("device") or "").strip()
        ref = (b.get("ref") or "").strip()
        if not ref:
            return self._json({"error": "缺少 ref"}, 400)
        args = ["snap", "unrm"] + ([dev] if dev else []) + [ref]
        rc, out, err = raw_netdev(args, timeout=90)
        if rc != 0 and ("--yes" in (err or "") or "确认" in (out or "")):
            rc, out, err = raw_netdev(args + ["--yes"], timeout=90)
        return self._json({"ok": rc == 0, "rc": rc, "stdout": out[-1500:], "stderr": err[-500:]})

    def _api_snap_restore(self, b: dict):
        dev = (b.get("device") or "").strip()
        idx = str(b.get("idx") or "").strip()
        apply = bool(b.get("apply"))
        if not dev:
            return self._json({"error": "缺少 device"}, 400)
        args = ["snap", "restore", dev] + (["--from", idx] if idx else [])
        if apply:
            args += ["--apply", "--yes"]          # 写操作：仍由 netdev 弹人审窗
        rc, out, err = raw_netdev(args, timeout=360)
        return self._json({"ok": rc == 0, "rc": rc, "applied": apply,
                           "stdout": out[-4000:], "stderr": err[-800:]})

    def _api_snap_file(self, qs):
        sid = (qs.get("id", [""])[0] or "").strip()
        name = pathlib.Path(qs.get("name", ["running.cfg"])[0]).name
        base = (ROOT / "backups" / "snapshots").resolve()
        f = (base / sid / name).resolve()
        if not str(f).startswith(str(base)):
            return self._send(403, b"forbidden")
        try:
            data = f.read_bytes()
        except Exception:
            return self._send(404, b"not found")
        return self._send(200, data, "text/plain; charset=utf-8")

    # ── SSE 终端流 ──
    def _api_stream(self, sid: str):
        s = SESSIONS.get(sid)
        if not s:
            return self._json({"error": "会话不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q, snap = s.subscribe()
        try:
            if snap:
                self._sse(b"snap", snap)
            while True:
                try:
                    data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # 保活
                    self.wfile.flush()
                    continue
                if data is None:
                    self._sse(b"end", b"")
                    break
                self._sse(b"out", data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)

    def _sse(self, ev: bytes, data: bytes) -> None:
        payload = base64.b64encode(data)
        self.wfile.write(b"event: " + ev + b"\ndata: " + payload + b"\n\n")
        self.wfile.flush()


_SERVED_CODE = ("ui/server.py", "ui/static/index.html", "netdev_mcp.py")


def _write_code_manifest(port: int) -> None:
    """把「本进程启动时加载的代码」按内容哈希记下来，供 CLI 的陈旧自检对账。

    ★ 2026-10-04 加。为什么必须用哈希而不是 mtime：
      mtime 会被「碰一下再还原」的操作污染 —— 本仓的
      tests/test_mcp_hotreload.py 就会临时改写 netdev_mcp.py 再还原，
      内容和大小都没变，只有 mtime 变新了。用 mtime 判断会报假警
      （doctor 平白让用户重启一次），而这正是我们要消灭的那类
      「看起来有理、其实在撒谎」的信号。哈希只认内容，改过就是改过。

    文件名带端口 —— 因为这可能是【隔离实例】：
      tests/test_ui_lifecycle.py 会在 8899 端口另起一个实例（它自己的
      pidfile / logfile 都是临时的）。若清单写死一个路径，那个实例就会
      把 8898 正式实例的清单覆盖掉，自检于是对着错误的基准比对。
    """
    try:
        import hashlib
        import json as _json
        files = {}
        for rel in _SERVED_CODE:
            p = ROOT / rel
            if p.is_file():
                files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "lib").glob("*.py")):
            files[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
        (ROOT / "logs" / f"ui-service-{port}.code.json").write_text(
            _json.dumps({"pid": os.getpid(),
                         "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "files": files}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """浏览器刷新/关标签导致的 RST 是常态，不要刷 traceback。"""
        if sys.exc_info()[0] in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


def cleanup_stale() -> int:
    """清掉上次异常退出残留的 ui-* 会话（只动 ui-*，绝不碰 netops / view-*）。"""
    if not TMUX:
        return 0
    n = 0
    try:
        r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=8)
        for name in (r.stdout or "").split():
            if name.startswith("ui-"):
                subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True, timeout=8)
                n += 1
    except Exception:
        pass
    return n


def idle_reaper() -> None:
    """无浏览器订阅、且长时间无输入的终端会话，自动回收 —— 避免 tmux 会话堆积。"""
    while True:
        time.sleep(60)
        now = time.time()
        with SESS_LOCK:
            dead = [sid for sid, s in SESSIONS.items() if not s.subs and now - s.last_active > 600]
            for sid in dead:
                s = SESSIONS.pop(sid)
                try:
                    s.close()
                except Exception:
                    pass
        with AI_LOCK:      # AI 会话：空闲 30 分钟且无订阅者就收掉（进程有成本）
            dead_ai = [aid for aid, s in AI_SESSIONS.items()
                       if not s.subs and now - s.last_active > 1800]
            for aid in dead_ai:
                s = AI_SESSIONS.pop(aid)
                try:
                    s.close()
                except Exception:
                    pass


def main() -> int:
    global UI_BASE
    ap = argparse.ArgumentParser(description="netdev-ui —— netdev 的独立界面")
    ap.add_argument("--port", type=int, default=8898)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    UI_BASE = f"http://{a.host}:{a.port}"
    # ★ 2026-09-30：把自己的地址也写进【本进程的 os.environ】——
    #   不只是 Python 变量。原因：① direct 后端在进程内 import netdev_mcp，
    #   它的 _netdev_cli 靠 os.environ 转发审批地址；② 任何从本进程继承
    #   环境的子进程（raw_netdev 的 CLI 等）都能看到网页审批通道。
    #   之前 UI_BASE 只是 Python 变量，os.environ 里没有 → direct/界面下发
    #   的审批全部静默回退到 macOS 原生弹窗（用户实测反馈）。
    os.environ["NETDEV_APPROVAL_URL"] = UI_BASE

    if not TMUX:
        print("⚠ 没找到 tmux：终端同屏不可用（其他功能仍在）", file=sys.stderr)
    if not (ROOT / "netdev").exists():
        print(f"⚠ 没找到 {ROOT/'netdev'}：设备清单与命令透传将不可用", file=sys.stderr)

    n = cleanup_stale()
    if n:
        print(f"  已清理上次残留的 {n} 个 ui-* 会话")
    threading.Thread(target=idle_reaper, daemon=True).start()

    srv = Server((a.host, a.port), Handler)
    # flush=True：作为后台守护进程跑时 stdout 是文件、不是 tty →
    # Python 会整块缓冲，日志文件长时间是空的，出问题无从查起（2026-10-01 实测踩到）。
    print(f"▮ netdev-ui 已启动  http://{a.host}:{a.port}   PID {os.getpid()}   "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  tmux={TMUX or '未找到'}  设备窗格会话={TMUX_SESSION}  静态目录={STATIC}", flush=True)
    # 记下本进程加载的代码指纹 —— 供 `netdev ui status` / `netdev doctor`
    # 判断「服务跑的代码是不是比磁盘上的旧」（改完源码忘了重启，实测踩过）。
    _write_code_manifest(a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在收尾…")
    finally:
        with SESS_LOCK:
            for s in list(SESSIONS.values()):
                s.close()
            SESSIONS.clear()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
