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


def load_ui_server():
    """把 ui/server.py 当模块加载（失败返回 None，并把原因记成一条 NG）。"""
    ui = ROOT / "ui"
    if str(ui) not in sys.path:
        sys.path.insert(0, str(ui))
    try:
        import server as S          # noqa: E402
        check("ui/server.py 可导入（语法 / 依赖无误）", True)
        return S
    except Exception as e:
        check("ui/server.py 可导入（语法 / 依赖无误）", False, f"{type(e).__name__}: {e}")
        return None


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


def main():
    print("=" * 66)
    print("netdev 回归测试：AI 工具链 + 采集缓存 + 宿主 shim 剥离")
    print("=" * 66)
    test_cmd_cache()
    test_detect()
    test_host_shim_strip()
    test_py_files_compile()
    test_mcp_contract()
    test_ai_toolchain()
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
