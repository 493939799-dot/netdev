#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：AI 工具链一致性 + 采集命令学习缓存 + 宿主 shim 剥离。

为什么是这几组：它们是「看不见但一坏就全坏」的地方，每一条都对应一次真事故。

  1. 采集命令学习缓存 —— 曾经 cache_put 是单向的（学了不用），每轮采集重撞墙。
  2. 平台自动识别   —— 识别错 = 命令表全错，AI 拿到的"现场"就是假的。
  3. 宿主 shim 剥离 —— 宿主给每个 Node 进程注入 NODE_OPTIONS=…node-language-shim.cjs，
     它把 mkdir 撞名的 EEXIST 改写成 code=CODEBUDDY_BROKER_DENY。
     凡是 proper-lockfile 那类「看 err.code 决定自愈」的库都会被整段跳过自愈分支。
     （本项目 2026-10-03 的真根因，症状极度误导。）
  4. 全仓 .py 可编译 —— 2026-10-03 查出 netdev_mcp.py 有个 IndentationError，
     整个文件语法不成立：MCP 服务端秒崩 → AI 手里"一个设备工具都没有"。
     这种错必须由回归兜住，不能靠人肉发现。
  5. netdev_mcp 服务端契约 —— 工具表 ↔ 处理器必须一一对应，且真能完成 MCP 握手。
  6. AI 工具链一致性 —— ui/server.py 给模型的工具 schema 必须与 netdev_mcp.TOOLS
     完全对齐；且 pi / WorkBuddy 两个 RPC 后端**确实已经拆掉了**。

用法：
    python3 tests/test_ai_toolchain_and_cache.py
全程离线：不接真机、不发网络请求、不消耗任何 AI 额度。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


import platforms as P  # noqa: E402


_UI_SERVER = None
_UI_SERVER_TRIED = False


def load_ui_server():
    """把 ui/server.py 当模块加载（失败返回 None，并把原因记成一条 NG）。

    会缓存结果 —— 多个测试组都要用它，重复 import 既没意义，
    又会让"可导入"这条 check 被重复计数、把用例总数搅乱。
    """
    global _UI_SERVER, _UI_SERVER_TRIED
    if _UI_SERVER_TRIED:
        return _UI_SERVER
    _UI_SERVER_TRIED = True
    ui = ROOT / "ui"
    if str(ui) not in sys.path:
        sys.path.insert(0, str(ui))
    try:
        import server as S          # noqa: E402
        check("ui/server.py 可导入（语法 / 依赖无误）", True)
        _UI_SERVER = S
    except Exception as e:
        check("ui/server.py 可导入（语法 / 依赖无误）", False, f"{type(e).__name__}: {e}")
        _UI_SERVER = None
    return _UI_SERVER


# ======================================================================
# 一、采集命令学习缓存：必须「学一次，一直用」
# ======================================================================
def test_cmd_cache():
    print("\n[1] 采集命令学习缓存")
    tmp = tempfile.mkdtemp(prefix="netdev-cache-")
    os.environ["NETDEV_ROOT"] = tmp
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False

    base = P.plan_commands("ruijie_os", "sw1")
    check("plan_commands 给出平台默认命令", bool(base.get("cpu")), str(base))
    check("锐捷默认 cpu 命令来自档案", base.get("cpu") == "show cpu", repr(base.get("cpu")))

    # 模拟「探测命中并记住」——某台老锐捷只认 display cpu-usage
    P.cache_put("sw1", "cpu", "display cpu-usage")
    after = P.plan_commands("ruijie_os", "sw1")
    check("学到的命令在建计划阶段即生效（本次修复点）",
          after.get("cpu") == "display cpu-usage", repr(after.get("cpu")))

    other = P.plan_commands("ruijie_os", "sw2")
    check("学习结果按设备隔离，不污染别的设备",
          other.get("cpu") == "show cpu", repr(other.get("cpu")))

    f = pathlib.Path(tmp) / "config" / "cmd-cache.json"
    check("缓存已落盘", f.is_file(), str(f))
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False
    again = P.plan_commands("ruijie_os", "sw1")
    check("重启后（清内存重载）仍生效", again.get("cpu") == "display cpu-usage",
          repr(again.get("cpu")))

    f.write_text("{ this is not json", encoding="utf-8")
    P._CMD_CACHE.clear()
    P._CACHE_LOADED = False
    try:
        bad = P.plan_commands("huawei_vrp", "h1")
        check("缓存文件损坏时静默降级（不抛异常）",
              bad.get("cpu") == "display cpu-usage", repr(bad))
    except Exception as e:
        check("缓存文件损坏时静默降级（不抛异常）", False, f"{type(e).__name__}: {e}")

    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("NETDEV_ROOT", None)


# ======================================================================
# 二、平台自动识别：五家 banner
# ======================================================================
def test_detect():
    print("\n[2] 平台自动识别（display version 回显）")
    cases = [
        ("Huawei Versatile Routing Platform Software\nVRP (R) software, Version 5.170", "huawei_vrp"),
        ("H3C Comware Software, Version 7.1.070", "h3c_comware"),
        ("Ruijie Networks RGOS Version 11.0", "ruijie_os"),
        ("Cisco IOS Software, C2960 Software", "cisco_ios"),
        ("Maipu MyPower OS Version 2.0", "maipu_s"),
        ("\x00\x01 garbage not a real banner", None),
    ]
    for text, want in cases:
        got = P.detect_platform(text)
        check(f"识别 {want or '不认识'} <= {text.splitlines()[0][:38]!r}", got == want, f"得到 {got!r}")


# ======================================================================
# 三、宿主注入的 Node / Shell shim：必须剥掉
# ======================================================================
def test_host_shim_strip():
    """2026-10-03 真根因回归（原本表现为「AI 助手莫名其妙不好用」）。

    宿主会给每个 Node 进程注入 NODE_OPTIONS=--require=…/node-language-shim.cjs，
    该 shim 把 mkdir 撞名的 EEXIST 改写成 code=CODEBUDDY_BROKER_DENY
    （message 文本仍是 "EEXIST: …"）。用 proper-lockfile 的库只在
    `err.code === 'EEXIST'` 时才走陈旧锁清理分支 → 分支被跳过 →
    **崩溃残留的锁永远不过期**。

    实测对照（同一个 proper-lockfile，只改锁目录 mtime）：
      带 shim   ：现在 / -3s / -10s / -29s / -35s / 未来 → 全部 BROKER_DENY
      剥掉 shim ：-29s → ELOCKED；-35s → 清掉旧锁并成功获取
    """
    print("\n[3] 宿主 Node / Shell shim 剥离")
    S = load_ui_server()
    if S is None:
        return

    SHIM = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/vendor/shim"

    # 1) NODE_OPTIONS 只有 shim → 整键删掉
    env = {"NODE_OPTIONS": f'--require="{SHIM}/node-language-shim.cjs"'}
    S._strip_host_shim(env)
    check("NODE_OPTIONS 全是宿主 shim 时整键删除", "NODE_OPTIONS" not in env, str(env))

    # 2) NODE_OPTIONS 混有用户自己的参数 → 只剔 shim，保留其他
    env = {"NODE_OPTIONS": f'--max-old-space-size=4096 --require="{SHIM}/node-language-shim.cjs"'}
    S._strip_host_shim(env)
    check("NODE_OPTIONS 混用时只剔 shim 那一段",
          env.get("NODE_OPTIONS") == "--max-old-space-size=4096", str(env))

    # 3) PATH 里的 shim 目录要被剔掉，**但 …/.workbuddy/binaries 这类运行时路径绝不能误伤**
    #    样本用中性的家目录名，避免把作者本机路径写进开源仓库；
    #    关键是路径里**含 host 字样**但不含 shim 特征 —— 守的是"别拿关键词乱杀"
    runtime = "/home/u/.workbuddy/binaries/node/versions/22.22.2/bin"
    env = {"PATH": f"{SHIM}/brokered-bin:{runtime}:/usr/bin:/bin"}
    S._strip_host_shim(env)
    got = (env.get("PATH") or "").split(":")
    check("PATH 里剥掉 shim 目录", f"{SHIM}/brokered-bin" not in got, str(got))
    check("PATH 里**保留** workbuddy 运行时目录（不能拿 workbuddy 当关键词乱杀）",
          runtime in got, str(got))
    check("PATH 里保留系统目录", "/usr/bin" in got and "/bin" in got, str(got))

    # 4) BASH_ENV 指向 shim → 删；指向用户自己的脚本 → 留
    env = {"BASH_ENV": f"{SHIM}/shell-runtime-bash-env.sh"}
    S._strip_host_shim(env)
    check("BASH_ENV 指向宿主 shim 时删除", "BASH_ENV" not in env, str(env))
    env = {"BASH_ENV": "/home/u/my-own.sh"}
    S._strip_host_shim(env)
    check("BASH_ENV 是用户自己的脚本则保留", env.get("BASH_ENV") == "/home/u/my-own.sh", str(env))

    # 5) 干净环境是**空操作**（用户自己双击启动 UI 时走的正是这条路）
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/u"}
    before = dict(env)
    S._strip_host_shim(env)
    check("干净环境零改动（幂等 / 无副作用）", env == before, f"{before} → {env}")

    # 6) _env_with_node 端到端：剥 shim + 补 node 路径，两件事都做到
    old = dict(os.environ)
    try:
        os.environ["NODE_OPTIONS"] = f'--require="{SHIM}/node-language-shim.cjs"'
        os.environ["PATH"] = f"{SHIM}/safe-bin:/usr/bin:/bin"
        e = S._env_with_node()
        check("_env_with_node 剥掉了 NODE_OPTIONS", "NODE_OPTIONS" not in e, str(e.get("NODE_OPTIONS")))
        check("_env_with_node 剔掉了 safe-bin", f"{SHIM}/safe-bin" not in (e.get("PATH") or ""),
              str(e.get("PATH")))
        check("_env_with_node 补上了 ~/.npm-global/bin",
              str(pathlib.Path.home() / ".npm-global/bin") in (e.get("PATH") or ""), str(e.get("PATH")))
    finally:
        os.environ.clear()
        os.environ.update(old)

    # ── 7) ★ 2026-10-04：Python 侧（sitecustomize.py）──────────────────────
    #    上一轮只修了 Node 侧，漏了这条，于是"快照删不掉"。
    #    宿主把 shim 目录塞进 PYTHONPATH，里面的 sitecustomize.py 会在解释器启动时
    #    被自动 import，接管 shutil.rmtree；被「批量删除守卫」拦下时 raise SystemExit。
    #    ★ PYTHONPATH 写的是**目录本身（无尾斜杠）**，所以匹配特征也必须是
    #      `/cli/vendor/shim` —— 原来带尾斜杠的写法对它**匹配不到**（这就是漏网的原因）。
    env = {"PYTHONPATH": SHIM}
    S._strip_host_shim(env)
    check("PYTHONPATH = 宿主 shim 目录（无尾斜杠）时整键删除", "PYTHONPATH" not in env, str(env))

    env = {"PYTHONPATH": f"{SHIM}:/home/u/mylib"}
    S._strip_host_shim(env)
    check("PYTHONPATH 混用时只剔 shim 那一段（保留用户自己的）",
          env.get("PYTHONPATH") == "/home/u/mylib", str(env))

    env = {"PYTHONPATH": "/home/u/mylib:/opt/pkgs"}
    before = dict(env)
    S._strip_host_shim(env)
    check("PYTHONPATH 与宿主无关时零改动", env == before, f"{before} → {env}")

    # 宿主为 shim 专门注入的键：连路径一起清掉。
    # 残留"半套配置"会让 shim 走到"helper 不可用"分支 —— 那条分支同样是 SystemExit。
    env = {"GENIE_TRASH_DIR": "/Applications/WorkBuddy.app/Contents/Resources/vendor/genie-trash",
           "CODEBUDDY_SAFE_DELETE_ENABLED": "1",
           "CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD": "50",
           "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS": "/tmp/x/broker.sock",
           "CODEBUDDY_BROKERED_BIN_DIR": f"{SHIM}/brokered-bin",
           "CODEBUDDY_CONFIG_DIR": "/Users/mac/.workbuddy",   # 与 shim 无关 → 必须留
           "HOME": "/Users/mac"}
    S._strip_host_shim(env)
    for dead in ("GENIE_TRASH_DIR", "CODEBUDDY_SAFE_DELETE_ENABLED",
                 "CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD",
                 "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS", "CODEBUDDY_BROKERED_BIN_DIR"):
        check(f"剥掉宿主 shim 专用变量 {dead}", dead not in env, str(env))
    check("**不能**误伤无关的 CODEBUDDY_*（如配置目录）",
          env.get("CODEBUDDY_CONFIG_DIR") == "/Users/mac/.workbuddy", str(env))
    check("不能误伤 HOME", env.get("HOME") == "/Users/mac", str(env))


# ======================================================================
# 三之二、批量删除守卫把删除变成 SystemExit：必须翻译成人话，且根因要堵住
# ======================================================================
def test_bulk_delete_guard_translation():
    """2026-10-04 真事故回归：界面里点「彻底删除快照」连续 4 次只报 Load failed。

    真因链（每一环都已实证）：
      宿主把 shim 目录注入 PYTHONPATH → shim/sitecustomize.py 被解释器自动 import →
      接管 shutil.rmtree → 删除前跑「批量删除守卫」（按轮次累计待删文件数，本机阈值 50）→
      超阈值时 raise SystemExit(1) → **SystemExit 是 BaseException**，
      业务的 `except Exception` 接不住 → 穿透 HTTP 处理函数；而
      `threading.excepthook` 对 SystemExit **静默忽略** → 日志里连 traceback 都没有 →
      连接断开 → 浏览器只报一句 "Load failed"。

    现场证据：`logs/ui-service.log` 里
      [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":53,"threshold":50,...}
    连出 4 条。
    """
    print("\n[3b] 宿主批量删除守卫：SystemExit 必须被翻译成人话")
    S = load_ui_server()
    from lib import snapshot as SN       # noqa: E402  （lib/ 内部互相用相对导入，只能整包引）

    orig = shutil.rmtree
    try:
        def _boom(*a, **k):
            raise SystemExit(1)          # 与 sitecustomize.py 的真实行为一致
        shutil.rmtree = _boom
        try:
            SN.rm_tree(ROOT / "backups" / "snapshots_trash" / "探针")
            check("rm_tree 遇 SystemExit 必须抛错（不许静默通过）", False, "没有抛错")
        except PermissionError as e:
            msg = str(e)
            check("rm_tree 把 SystemExit 翻成 PermissionError（Exception 能接住）", True)
            check("错误里点明是宿主守卫干的", ("守卫" in msg and "宿主" in msg), msg[:160])
            check("错误里点明 SystemExit 这一层", "SystemExit" in msg, msg[:160])
            check("错误里给了补救动作 netdev ui restart", "netdev ui restart" in msg, msg[:160])
        except BaseException as e:       # noqa: BLE001
            check("rm_tree 把 SystemExit 翻成 PermissionError", False,
                  f"抛的是 {type(e).__name__}: {e}")
    finally:
        shutil.rmtree = orig

    # 加固不许影响正常路径
    d = tempfile.mkdtemp(prefix="netdev-rmtree-")
    (pathlib.Path(d) / "x").write_text("x", encoding="utf-8")
    SN.rm_tree(d)
    check("rm_tree 正常路径照常删除", not pathlib.Path(d).exists())

    srv_src = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    cli_src = (ROOT / "netdev_cli.py").read_text(encoding="utf-8")

    # 调用点必须真的用上它，否则加固只是摆设
    check("server.snap_purge 走 S.rm_tree", "S.rm_tree(" in srv_src)
    check("server 不再裸调 _sh.rmtree", "_sh.rmtree" not in srv_src)
    check("netdev CLI 的 purge 走 S.rm_tree", "S.rm_tree(" in cli_src)
    check("netdev CLI 不再裸调 _sh.rmtree", "_sh.rmtree" not in cli_src)
    check("HTTP 层显式兜住 SystemExit", "except (Exception, SystemExit)" in srv_src)

    # ★ 根因修复：守护进程必须在 os.execv **之前**净化环境。
    #   只净化"子进程的 env"是不够的 —— 钩子是在本进程启动阶段 import 进来的，撤不掉。
    dz = (ROOT / "ui" / "daemonize.py").read_text(encoding="utf-8")
    check("daemonize 引入了宿主钩子剥离", "strip_host_injection" in dz)
    check("daemonize 在 os.execv 之前净化环境",
          dz.index("strip_host_injection(os.environ)") < dz.index("os.execv("),
          "顺序反了就等于没修")

    # 界面：删除成功的提示不再倒 CLI 原始输出（\r 重绘 + ANSI 会拼成乱码）
    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    seg = html[html.index("if(a==='del')"):html.index("if(a==='restore')")]
    check("快照「删除」分支不再倒 CLI 原始 stdout/stderr",
          ("r.stdout" not in seg) and ("r.stderr" not in seg), seg[-200:])
    check("_api_snap_rm 的输出过了 clean_cli_tail", "clean_cli_tail(out" in srv_src)
    if S is not None:
        check("CLI 原始输出已去 ANSI 与 \\r 重绘",
              "\r" not in S.clean_cli_tail("\x1b[2K进度1\r\x1b[32m✔ 好了\x1b[0m", 200))


# ======================================================================
# 四、全仓 Python 必须可编译（2026-10-03 netdev_mcp.py IndentationError 的守门）
# ======================================================================
def _source_py_files() -> list[pathlib.Path]:
    """真正会进仓库的那些 .py —— 用 git 自己的规则量，不靠读 .gitignore 猜。

    拿不到 git（比如源码压缩包）时退回"排除已知产物目录"的走法。
    """
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "ls-files", "*.py"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=20)
        files = [ROOT / ln for ln in (r.stdout or "").splitlines()
                 if ln.strip() and (ROOT / ln).is_file()]
        if r.returncode == 0 and files:
            return sorted(files)
    except Exception:
        pass
    skip = {".venv", "__pycache__", ".git", "dist", "build", "node_modules"}
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in skip or part.startswith(".") for part in p.parts):
            continue
        out.append(p)
    return out


def test_py_files_compile():
    """语法的守门测试。

    为什么必须有：netdev_mcp.py 曾经被多缩进 2 格，**整个文件语法都不成立**
    （Python 直接 IndentationError 退出）。后果极隐蔽 —— MCP 服务端秒崩，
    客户端只是"静默丢弃这个服务端"，模型手里一个设备工具都没有，
    表现却是"AI 在瞎编工具调用"。这种错只能靠编译检查兜住。
    """
    print("\n[4] 全仓 Python 可编译")
    files = _source_py_files()
    bad = []
    for p in files:
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            bad.append(f"{p.relative_to(ROOT)}:{e.lineno} {e.__class__.__name__}: {e.msg}")
    check(f"仓内 {len(files)} 个 .py 全部可编译", not bad, "; ".join(bad[:5]))
    check("扫到了源文件（不是空扫）", len(files) >= 20, f"只有 {len(files)} 个")


# ======================================================================
# 五、netdev_mcp 服务端契约：工具表 ↔ 处理器一一对应，且真能握手
# ======================================================================
def _mcp_python() -> str:
    """优先用项目自带 venv 的解释器（与 netdev-mcp 包装脚本一致）。"""
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _mcp_roundtrip(proc, obj: dict, timeout: float = 20.0) -> dict | None:
    import time
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("id") == obj.get("id"):
            return ev
    return None


def test_mcp_contract():
    print("\n[5] netdev_mcp 服务端契约")
    mcp_py = ROOT / "netdev_mcp.py"
    try:
        import netdev_mcp  # noqa: E402
    except Exception as e:
        check("netdev_mcp.py 可导入", False, f"{type(e).__name__}: {e}")
        return
    check("netdev_mcp.py 可导入", True)

    names = [t.get("name") for t in netdev_mcp.TOOLS]
    check("TOOLS 名字无重复", len(names) == len(set(names)), str(names))
    check("TOOLS 每项都带 name/description/inputSchema",
          all(t.get("name") and t.get("description") and isinstance(t.get("inputSchema"), dict)
              for t in netdev_mcp.TOOLS), str(names))
    missing = [n for n in names if n not in netdev_mcp.HANDLERS]
    extra = [n for n in netdev_mcp.HANDLERS if n not in names]
    check("TOOLS 里每个工具都有处理器", not missing, f"缺：{missing}")
    check("HANDLERS 里没有工具表之外的孤儿", not extra, f"多：{extra}")
    check("工具数 = 13（护栏面不能悄悄变窄）", len(names) == 13, f"实际 {len(names)}：{names}")

    # 包装脚本要在（外部 MCP 客户端就是靠它起服务的）
    wrapper = ROOT / "netdev-mcp"
    check("netdev-mcp 启动包装脚本存在且可执行",
          wrapper.is_file() and os.access(wrapper, os.X_OK), str(wrapper))

    # 真跑一次 MCP 握手 —— 这一步能同时验证「协议没写坏」和「启动不会秒崩」
    proc = None
    try:
        env = dict(os.environ)
        env["NETDEV_ROOT"] = str(ROOT)
        proc = subprocess.Popen([_mcp_python(), str(mcp_py)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                cwd=str(ROOT), env=env)
        init = _mcp_roundtrip(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05",
                                                "capabilities": {},
                                                "clientInfo": {"name": "netdev-selftest",
                                                               "version": "1"}}})
        si = ((init or {}).get("result") or {}).get("serverInfo") or {}
        check("MCP initialize 握手成功", si.get("name") == "netdev", str(init)[:200])
        check("握手带 instructions（客户端要把它交给 AI）",
              bool(((init or {}).get("result") or {}).get("instructions")), str(init)[:120])

        tl = _mcp_roundtrip(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = ((tl or {}).get("result") or {}).get("tools") or []
        got = sorted(t.get("name") for t in tools)
        check("MCP tools/list 返回 13 个工具", len(tools) == 13, f"{len(tools)}：{got}")
        check("MCP tools/list 与 netdev_mcp.TOOLS 完全一致", got == sorted(names),
              f"{got} != {sorted(names)}")
    except Exception as e:
        check("MCP 服务端可拉起并握手", False, f"{type(e).__name__}: {e}")
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


# ======================================================================
# 六、AI 工具链一致性 + RPC 后端确已拆除
# ======================================================================
def test_ai_toolchain():
    """AI 手里的工具必须**就是** netdev_mcp 的那 13 个，一个不多一个不少。"""
    print("\n[6] AI 工具链一致性（直连后端）")
    S = load_ui_server()
    if S is None:
        return
    try:
        import netdev_mcp  # noqa: E402
    except Exception as e:
        check("netdev_mcp 可导入（AI 工具来源）", False, f"{type(e).__name__}: {e}")
        return

    # ── 档位表 ──
    check("TOOLSETS 只有 read / read+netdev / full 三档",
          tuple(S.TOOLSETS) == ("read", "read+netdev", "full"), str(S.TOOLSETS))
    check("NETDEV_TOOLSETS 严格对齐 TOOLSETS",
          S.NETDEV_TOOLSETS <= set(S.TOOLSETS) and "read" not in S.NETDEV_TOOLSETS,
          str(S.NETDEV_TOOLSETS))

    # ── 给模型的 schema 必须与 netdev_mcp.TOOLS 对齐 ──
    schema = S._direct_tool_schema()
    names = sorted(t["function"]["name"] for t in schema)
    mcp_names = sorted(t["name"] for t in netdev_mcp.TOOLS)
    check("AI 工具 schema 与 netdev_mcp.TOOLS 名字一致", names == mcp_names,
          f"{names} != {mcp_names}")
    check("schema 是 OpenAI 兼容形状（type=function / 带 parameters）",
          all(t.get("type") == "function" and isinstance(t["function"].get("parameters"), dict)
              for t in schema), str(schema[:1])[:200])
    check("schema 里不含 MCP 专属字段（inputSchema 已转义掉）",
          all("inputSchema" not in t["function"] for t in schema))

    # ── 护栏唯一性：DirectSession 必须复用 netdev_mcp 的处理器，而不是自己实现 ──
    src = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    check("DirectSession 复用 netdev_mcp.HANDLERS（不重写设备逻辑）",
          "netdev_mcp.HANDLERS" in src and "netdev_mcp.envelope" in src)
    check("DirectSession 有工具结果摘要 + 落盘（防大回显冲垮上下文）",
          "_summarize_tool_result" in src and "_truncated" in src)

    # ★ 2026-10-03 事故守门①：摘要器遇到「列表里装字典」不许抛异常 ——
    #   事故现场：netdev_run 的 results = [{"command":…, "output":…}]，
    #   摘要器里裸的 "\n".join(v) 直接抛 TypeError，异常穿透到 _call_tool 的
    #   except → isError=True → 界面「⚠ netdev_run 执行失败」，
    #   而真正的结果（171 行运行配置）被整个丢掉。
    #   落盘指纹：logs/ai_tool/<aid>/ 里**只有 *_raw.txt、没有 *_results.txt**。
    #   这类「后处理把成功伪造成失败」最隐蔽 —— 工具本身完全正常，
    #   偏偏它是 AI 读配置的主力工具，症状看起来像"AI 坏了"。
    class _Probe(S.DirectSession):
        def __init__(self):
            self.aid = "_selftest_summarize"
        def _emit(self, ev):
            pass
    try:
        _p = _Probe()
        _payload = {"device": "x", "ok": True,
                    "results": [{"command": "display current-configuration",
                                 "ok": True, "output": "A" * 3000}],
                    "raw": "A" * 3000}
        out = _p._summarize_tool_result("netdev_run", dict(_payload))
        check("摘要器能吃下「列表里装字典」的 results（不把成功伪装成失败）",
              out.get("_truncated") is True and out.get("ok") is True,
              f"_truncated={out.get('_truncated')!r} ok={out.get('ok')!r}")
        sres = out.get("results")
        check("摘要后 results 仍带全文落盘路径",
              isinstance(sres, dict) and bool(sres.get("全文已落盘")), str(sres)[:140])
        check("摘要成功后不出现回退警告 _note", not out.get("_note"), str(out.get("_note"))[:140])
    except Exception as e:
        check("摘要器能吃下「列表里装字典」的 results（不把成功伪装成失败）",
              False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(ROOT / "logs" / "ai_tool" / "_selftest_summarize", ignore_errors=True)

    # ★ 2026-10-03 事故守门②：失败必须说得清原因 ——
    #   原来 tool_execution_end 只带 isError、不带 error，界面只剩一句
    #   「⚠ xxx 执行失败」。模型和用户都不知道发生了什么，实测模型连猜 4 次
    #   全错，并把 2 次调用吹成 8 次。
    check("DirectSession 会从结果里挑出失败原因", hasattr(S.DirectSession, "_failure_reason"))
    check("失败原因能从 error 字段提取",
          "清单里没有" in S.DirectSession._failure_reason(
              {"ok": False, "error": "✘ 清单里没有 'x'"}), "空")
    check("失败原因也能从结构化 steps/results 里提取",
          S.DirectSession._failure_reason(
              {"ok": False, "results": [{"ok": False, "error": "串口被占用"}]}) == "串口被占用")
    check("成功的结果不提原因",
          S.DirectSession._failure_reason({"ok": True}) == "")

    # ★ 2026-10-04 事故守门③：改完源码但服务没重启 ——
    #   症状是"bug 明明修好了，界面上还是旧行为"。ui/server.py 是进程启动那刻
    #   读进内存的，不重启就一直跑旧逻辑。实测因此白排查了一轮。
    #   现在 doctor / netdev ui status 会自己报「服务跑的代码比源文件旧」。
    cli_src = (ROOT / "netdev_cli.py").read_text(encoding="utf-8")
    check("doctor/ui status 自检「服务跑的代码是否比源文件旧」",
          "_ui_code_stale" in cli_src and "netdev ui restart" in cli_src)
    check("该自检已接进 ui status 与 doctor 两条展示路径",
          cli_src.count("_ui_code_stale(") >= 3, f"引用 {cli_src.count('_ui_code_stale(')} 次")

    # ★ 2026-10-04 事故守门⑤：清单文件必须【按端口分文件】。
    #   第一版写死成 logs/ui-service.code.json —— 于是 tests/test_ui_lifecycle.py
    #   在 8899 起的隔离实例会把 8898 正式实例的清单覆盖掉，自检就对着
    #   错误的基准比对（实测抓到：清单里记的 pid 是个已经退出的临时进程）。
    _srv_src = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    check("陈旧自检的清单按端口分文件（隔离实例不互相覆盖）",
          "ui-service-{port}.code.json" in _srv_src
          and "_write_code_manifest(a.port)" in _srv_src
          and "ui-service-{UI_PORT}.code.json" in cli_src)

    # ★ 2026-10-04 事故守门④：这条自检【自己不许静默失效】。
    #   第一版用 mtime 判断 → 被 tests/test_mcp_hotreload.py 的「改写再还原」
    #   污染（内容大小都不变、只有 mtime 变），会平白让用户重启一次（假警）；
    #   第二版改用内容哈希，却发现 json 没导入 → 异常被 except 吞掉、
    #   永远返回「干净」（静默死）。两种毛病都得由测试兜住。
    #   这里在【临时目录】里造现场，绝不碰真实源文件。
    try:
        import hashlib as _hl
        import json as _json
        import netdev_cli as _cli
        _orig_root = _cli.ROOT
        try:
            with tempfile.TemporaryDirectory() as _td:
                _cli.ROOT = pathlib.Path(_td)
                (_cli.ROOT / "logs").mkdir(parents=True)
                _probe = _cli.ROOT / "_probe.txt"
                _probe.write_text("v1\n", encoding="utf-8")

                def _put_manifest(digest):
                    (_cli.ROOT / "logs" / f"ui-service-{_cli.UI_PORT}.code.json").write_text(
                        _json.dumps({"pid": 0, "started": "2026-10-04 00:00:00",
                                     "files": {"_probe.txt": digest}}), encoding="utf-8")

                _put_manifest(_hl.sha256(b"v1\n").hexdigest())
                check("陈旧自检：内容一致时闭嘴（不假报）",
                      _cli._ui_code_stale()[0] is False, str(_cli._ui_code_stale()))
                _probe.write_text("v2\n", encoding="utf-8")          # 真改内容
                _fired, _why = _cli._ui_code_stale()
                check("陈旧自检：内容变了会开口，且给出补救命令",
                      _fired and "netdev ui restart" in _why, _why)
                _probe.write_text("v1\n", encoding="utf-8")          # 只还原内容
                os.utime(_probe, None)                               # 再碰一下 mtime
                check("陈旧自检：只碰 mtime 不报（这正是它被改成哈希版的原因）",
                      _cli._ui_code_stale()[0] is False, str(_cli._ui_code_stale()))
                (_cli.ROOT / "logs" / f"ui-service-{_cli.UI_PORT}.code.json").unlink()
                check("陈旧自检：没有清单时安静跳过（不猜、不报）",
                      _cli._ui_code_stale() == (False, ""))
        finally:
            _cli.ROOT = _orig_root
    except Exception as e:
        check("陈旧自检可被调用且不静默失效", False, f"{type(e).__name__}: {e}")

    # ── 三个「已拆除」的守门断言：别哪天又被悄悄加回来 ──
    for dead in ("AiSession", "WbSession", "PI_BIN", "_rpc_startable", "pi_heal_locks",
                 "_wb_argv"):
        check(f"pi / WorkBuddy RPC 后端确已拆除：{dead} 不存在",
              not hasattr(S, dead), f"server 模块里又出现了 {dead}")
    for dead_src in ("/api/pi/repair", "mcp__netdev", "--disallowedTools"):
        check(f"源码里不含已下线的通道残留：{dead_src}", dead_src not in src)

    # ── 探测接口只剩 direct 一个后端 ──
    r = S.probe_agents()
    ids = [a.get("id") for a in r.get("agents", [])]
    check("probe_agents 只报 direct 一个后端", ids == ["direct"], str(ids))
    check("probe_agents 的 recommended 只在可用时才给值",
          (r.get("recommended") == "direct") == bool(r["agents"][0].get("authed")),
          f"recommended={r.get('recommended')!r} authed={r['agents'][0].get('authed')!r}")


# ======================================================================
# 七、AI 面板的停止按钮：3×3 点阵（2026-10-04 用户要求「统一风格」）
# ======================================================================
def test_ai_stop_button_dotmatrix():
    """停止按钮的图标沿革：实心方块「■」→ 3×3 方点阵 → 3×3 圆点阵 → **一颗圆点**。

    用户最后一次的要求是「整个换掉」—— 那 9 个格不要了，换成**一颗**小圆点，
    就是设备行「窗格在线」那一颗（7px 圆 + 6px 柔光）。语义也跟着对齐设备行：
    不活动 = 主题主色实心点；活动 = 变绿 + 呼吸。

    这条守的都是**「改了不报错、只会悄悄变样」**的地方 —— 正是本项目专门写测试
    兜住的那一类：

      ① 按钮里必须只有**一颗**点，且不再出现「■」字面量、也不再有点阵的 9 个 `<i>`；
      ② 点的尺寸/柔光必须与设备行那颗**同规格**（7px / 0 0 6px）——
         不一致就变成"两种材质"，但**不会报任何错**；
      ③ 空闲/忙两态靠 .busy 类切；★ 必须用 `animation-name` **长写法** ——
         写成 `animation:dotbeat` 简写会把 duration/timing/iteration-count
         一并重置成默认值，于是 `animation-duration` 变成 `0s`、**动画根本不跑**，
         点就一直静静躺着。观感完全变了，但**没有任何报错**、也不影响任何接口。
      ④ `aiBusy()` 必须同时管到停止按钮（它原先只管 AI 标题后面那个网状图标）。
      ⑤ 颜色一律走 `currentColor`（底色 + 柔光各写一遍就会漏掉一处）。
    """
    print("\n[7] AI 面板停止按钮：一颗圆点")
    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    # ① 按钮本体：一颗点，不是九颗
    check("停止按钮改用 .ai-stop-dot（单颗点）",
          'aria-label="停止"><span class="ai-stop-dot"' in html)
    check("停止按钮里已无「■」字面量", 'aria-label="停止">■' not in html)
    a = html.index('class="ai-stop-dot"')
    seg = html[a:html.index("</button>", a)]
    check("★ 9 个点阵格已整个移除（不再是 <i> 点阵）", seg.count("<i>") == 0, seg)
    check("点元素是自闭的、没有子节点", "<span class=\"ai-stop-dot\" aria-hidden=\"true\"></span>" in html)

    # ② 忙闲两态 +「不能改成简写」这条最容易踩
    check("空闲态显式关掉动画（否则会一直闪）", "animation-name:none" in html)
    check("忙态用 animation-name 切动效", "animation-name:dotbeat" in html)
    check("★ 不许用 animation 简写（会重置 duration → 变成 0s，动画根本不跑）",
          "animation:dotbeat" not in html,
          "写成简写 duration 就是默认 0s，点不会呼吸，且不报任何错")
    check("呼吸的 keyframes 已定义", "@keyframes dotbeat" in html)

    def _rule(sel: str) -> str:
        i = html.index(sel)
        return html[i:html.index("}", i)]

    # ③ 与设备行那颗点**同规格**（尺寸 + 柔光），否则就是两种材质
    dot = _rule(".ai-in button.icon-btn .ai-stop-dot{")
    check("点 7px，与设备行 .st 同尺寸", "width:7px" in dot and "height:7px" in dot, dot)
    check("圆形（border-radius:50%）", "border-radius:50%" in dot, dot)
    check("柔光 0 0 6px，与设备行同款", "box-shadow:0 0 6px" in dot, dot)
    check("★ 底色与柔光都走 currentColor（只写一处会漏）",
          dot.count("currentColor") == 2, dot)
    st = _rule(".st{")
    check("设备行那颗点确实还是 7px（两边没跑偏）", "width:7px" in st and "height:7px" in st, st)
    check("设备行那颗点确实还带 0 0 6px 柔光", "box-shadow:0 0 6px" in html)

    # ④ aiBusy 必须管到它
    js = html[html.index("function aiBusy(on)"):html.index("function termBusy(")]
    check("aiBusy 会切停止按钮的 .busy",
          "getElementById('aiStop')" in js and "classList.toggle('busy'" in js)
    check("aiBusy 仍保留 AI 标题图标的原逻辑（没被改坏）",
          "ico.net" in js and "_aiT" in js)

    # ⑤ 旧设计不许复活
    check("★ 旧的 .dots.stop 点阵写法没有残留",
          "dots stop" not in html and "dots.stop" not in html)
    check("★ 旧的 dotpulse-btn 已随点阵一起去掉（别留死 CSS）",
          "dotpulse-btn" not in html)
    # 反例：品牌标识与「接入中」的点阵仍该在，不许被顺手删掉
    check("品牌标识的 3×3 点阵仍在", ".brand .dots.logo" in html)
    check(".bk-busy（接入中）的点阵仍在", ".bk-busy .dots" in html)
    check("品牌点阵的对角错峰 delay 仍在（没被顺手清掉）",
          ".dots i:nth-child(9){animation-delay:.40s}" in html)


# ======================================================================
# 八、「活动状态 = 绿」这条界面约定（2026-10-04 用户确立）
# ======================================================================
def test_active_state_is_green():
    """用户报障：设备行那个小圆点明明是"窗格在线"，在浅色主题下却是黑的。

    真因不是漏了颜色，是**用错了色源**：那两处"在线"指示点都取 `var(--g)`
    （主题主色）。深色主题里主色本身就是绿，看不出问题；可一到
    `minimal`（--g 纯黑）/ `ink`（--g 近黑）主题，"在线"的黑点和"离线"的
    `#3a3a3a` 深灰**几乎分不出来** —— 指示点等于白放。

    约定：活动/在线一律用**语义色 `--st-on`**（就是「已接入」插头在用的那个绿），
    不跟主题主色走。同一逻辑同时管：设备行圆点、顶栏"在线"点、AI 点阵的忙态。

    守的都是"改了不报错、只有观感悄悄变"的地方，尤其是**最后一条**：
    以后新增浅色主题若忘了定义 `--st-on`，会掉回 `:root` 里那个荧光绿
    `#00ff41` —— 在白底上刺眼且对比度差，但**没有任何报错**。
    """
    print("\n[8] 「活动状态 = 绿」的界面约定")
    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    def rule(sel: str) -> str:
        i = html.index(sel)
        return html[i:html.index("}", i)]

    st_ok = rule(".st.ok{")
    check("设备行「在线」点改用语义色 --st-on", "--st-on" in st_ok, st_ok)
    check("设备行「在线」点不再取主题主色 --g", "var(--g)" not in st_ok, st_ok)

    dot = rule(".dot{")
    check("顶栏「在线」点同一条规则", "--st-on" in dot and "var(--g)" not in dot, dot)

    busy = rule(".ai-in button.icon-btn.busy .ai-stop-dot{")
    check("AI 停止点忙态 = 绿", "color:var(--st-on)" in busy, busy)
    idle = rule(".ai-in button.icon-btn .ai-stop-dot{")
    check("AI 停止点空闲态**不**上绿色（只有活动才绿）", "--st-on" not in idle, idle)

    # 三个状态仍要能互相区分 —— 别为了"在线变绿"把另两个弄没了
    for sel in (".st.off{", ".st.warn{"):
        check(f"另两态仍独立定义：{sel[:-1]}", sel in html)
    check("「离线」点保持中性灰（不抢在线的绿）",
          "#3a3a3a" in rule(".st.off{"), rule(".st.off{"))

    # ★ 浅色主题必须自带 --st-on，否则掉回 :root 的荧光绿（白底上刺眼、无报错）
    for th in ("minimal", "ink"):
        seg = html[html.index(f'[data-theme="{th}"]{{'):]
        seg = seg[:seg.index("}")]
        check(f"浅色主题 {th} 自带 --st-on（否则白底上是荧光绿）",
              "--st-on" in seg, seg[:120])
    # 注意：文件里有**两个** :root 块（基础变量一个、--st-* 语义色一个），
    # 只 index 第一个会误判 —— 这条断言自己踩过一次，所以按"全部 :root 块"来找。
    roots, p = [], 0
    while True:
        try:
            i = html.index(":root{", p)
        except ValueError:
            break
        roots.append(html[i:html.index("}", i)])
        p = i + 1
    check(":root 里定义了 --st-on（深色主题靠它兜底）",
          any("--st-on:" in b for b in roots), f"共 {len(roots)} 个 :root 块")


def main():
    print("=" * 66)
    print("netdev 回归测试：AI 工具链 + 采集缓存 + 宿主 shim 剥离")
    print("=" * 66)
    test_cmd_cache()
    test_detect()
    test_host_shim_strip()
    test_bulk_delete_guard_translation()
    test_py_files_compile()
    test_mcp_contract()
    test_ai_toolchain()
    test_ai_stop_button_dotmatrix()
    test_active_state_is_green()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  NG ", f)
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
