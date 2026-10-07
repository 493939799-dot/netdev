#!/usr/bin/env python3
r"""netdev MCP 源码热重载端到端测试（子进程 JSON-RPC 驱动；不连任何设备）。

覆盖四项：
  (a) 基线：initialize → tools/list 拿到 N 个工具；tools/call netdev_list 成功（isError=false）
  (b) 行为级热重载：临时往 netdev_mcp.py 的 TOOLS / HANDLERS 各加一个探针工具，
      **不重启进程**再发 tools/list，断言新工具出现，且收到 notifications/tools/list_changed；
      并改 lib/ 下的临时模块，断言同一进程内 lib 代码也立即生效（即上报的核心缺陷那一类）
  (c) 容错：往 lib/ 放语法错误文件，断言服务器不崩、后续 tools/call 仍成功、stderr 有 reload 失败记录；
      再把该文件改好，断言服务器重试并成功（失败条目不计入已重载）
  (d) 只改某个工具的 description（不增删工具）：断言同一进程内新描述在 tools/list 里可见，
      且仍收到 notifications/tools/list_changed（工具定义变化就该通知）

测试全程只有一个 MCP 子进程（不重启），finally 里字节级还原源码、删临时文件并断言清理成功。
运行：.venv\Scripts\python.exe tests\test_mcp_hotreload.py   （Windows，项目根目录下）
      ./.venv/bin/python tests/test_mcp_hotreload.py         （macOS）
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "netdev_mcp.py"
LIB = ROOT / "lib"
PY = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
PROBE = "netdev_hotreload_probe"
PROBE_LIB = LIB / "_broken_probe.py"      # (c) 语法错误用
PROBE_LIB2 = LIB / "_probe_lib.py"        # (b) lib 模块热重载用（即本次报的核心缺陷那一类）
DESC_OLD = '"description": "列出已配置设备与最近镜像流"'      # (d) 只改描述用
DESC_MARK = " [HOTRELOAD-VERIFY]"

PROBE_TOOL_LINE = ('    {"name": "netdev_hotreload_probe", "description": "临时热重载探针（测试用）",\n'
                   '     "inputSchema": {"type": "object", "properties": {}}},\n')
PROBE_FUNC = ('def _probe_handler(_):\n'
              '    import importlib as _il\n'
              '    m = _il.import_module("lib._probe_lib")\n'
              '    return {"probe_value": m.VALUE}\n'
              '\n\n')
PROBE_HANDLER_LINE = '    "netdev_hotreload_probe": _probe_handler,\n'
BROKEN_SRC = "def broken(:\n    pass\n"          # 故意语法错误
FIXED_SRC = "PROBE_MARK = 1\n"                   # 修好后的内容


def md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def with_probe(text):
    assert text.count("TOOLS = [\n") == 1, "定位 TOOLS 失败"
    assert text.count("HANDLERS = {\n") == 1, "定位 HANDLERS 失败"
    text = text.replace("TOOLS = [\n", "TOOLS = [\n" + PROBE_TOOL_LINE, 1)
    text = text.replace("HANDLERS = {\n", PROBE_FUNC + "HANDLERS = {\n", 1)
    return text.replace("HANDLERS = {\n", "HANDLERS = {\n" + PROBE_HANDLER_LINE, 1)


def with_desc_mark(text):
    """只改 netdev_list 的 description（工具名不变、不增删工具）。(d) 用。"""
    assert text.count(DESC_OLD) == 1, "定位 netdev_list 描述失败"
    return text.replace(
        DESC_OLD, f'"description": "列出已配置设备与最近镜像流{DESC_MARK}"', 1)


class McpServer:
    """一个 MCP 子进程 + 行级 JSON-RPC 驱动（通知与响应分开收集）。"""

    def __init__(self):
        # PYTHONUTF8=1：MCP 的工具描述与返回内容含中文，必须让子进程按 UTF-8
        # 写、本测试按 UTF-8 读（下面 encoding= 与它成对），否则在中文 Windows
        # （默认 cp936）上 JSON-RPC 报文会解码失败。
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(
            [str(PY), str(SRC)], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace")
        self._id = 0
        self._out = queue.Queue()
        self._lock = threading.Lock()
        self.notes = []
        self.err = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    # ── 读线程
    def _pump_out(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self.err.append(f"<stdout 非 JSON> {line}")
                continue
            if "id" not in obj:
                with self._lock:
                    self.notes.append(obj)
            self._out.put(obj)

    def _pump_err(self):
        for line in self.proc.stderr:
            self.err.append(line.rstrip("\n"))

    # ── 写读
    def call(self, method, params=None, timeout=20.0):
        self._id += 1
        rid = self._id
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"{method} 响应超时（{timeout}s）")
            try:
                obj = self._out.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"{method} 响应超时（{timeout}s）")
            if obj.get("id") == rid:
                return obj

    def tool_names(self):
        r = self.call("tools/list", {})
        return [t["name"] for t in r["result"]["tools"]]

    def call_tool(self, name, args=None):
        return self.call("tools/call", {"name": name, "arguments": args or {}})

    def note_methods(self):
        with self._lock:
            return [n.get("method") for n in self.notes]

    def wait_note(self, method, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if method in self.note_methods():
                return True
            time.sleep(0.05)
        return False

    def wait_note_count(self, n, timeout=5.0):
        """等通知条数至少到 n（用于"本次改动是否新发了通知"）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.note_methods()) >= n:
                return True
            time.sleep(0.05)
        return False

    def tool_descriptions(self):
        r = self.call("tools/list", {})
        return {t["name"]: t.get("description", "") for t in r["result"]["tools"]}

    def reload_failures(self):
        return [ln for ln in self.err if "hot-reload failed" in ln]

    def wait_stderr(self, needles, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(any(n in ln for ln in self.err) for n in needles):
                return True
            time.sleep(0.1)
        return False

    def alive(self):
        return self.proc.poll() is None

    def destroy(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass


def touch(p, text):
    """改源码：0.05s 拉开 mtime，并按字节写入（保证与还原时的字节级一致）。"""
    time.sleep(0.05)
    p.write_bytes(text.encode("utf-8"))


# ─────────────────────────────────────────────────────────────── 四项测试
def phase_a(s):
    r = s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "mcp-hotreload-test", "version": "1"}})
    info = r["result"]["serverInfo"]
    print(f"  serverInfo = {info}, protocolVersion = {r['result']['protocolVersion']}")
    assert info["name"] == "netdev", f"serverInfo 异常: {info}"

    names = s.tool_names()
    print(f"  工具数 N = {len(names)}")
    print(f"  {names}")
    assert "netdev_list" in names, "基线缺少 netdev_list"
    assert PROBE not in names, "基线里不该有探针工具"

    r = s.call_tool("netdev_list")
    assert r["result"]["isError"] is False, f"netdev_list 失败: {r['result']['content']}"
    payload = json.loads(r["result"]["content"][0]["text"])
    print(f"  tools/call netdev_list → isError=false, devices={len(payload['devices'])} "
          f"(names={[d['name'] for d in payload['devices']]})")
    assert payload["devices"], "netdev_list 没返回设备"
    print(f"  进程存活 = {s.alive()}")
    assert s.alive()
    return len(names)


def phase_b(s, original, baseline_n):
    touch(PROBE_LIB2, "VALUE = 1\n")
    touch(SRC, with_probe(original))
    print(f"  已临时写入探针工具 + {PROBE_LIB2.name}（进程 pid={s.proc.pid}，未重启）")
    assert s.alive(), "改源码后进程不该退出"

    names = s.tool_names()                      # 同一进程内的下一次请求
    print(f"  再次 tools/list → 工具数 N' = {len(names)}")
    assert PROBE in names, f"热重载未生效：tools/list 里没有 {PROBE}"
    assert len(names) == baseline_n + 1, f"工具数应 +1：{baseline_n} → {len(names)}"
    got = s.wait_note("notifications/tools/list_changed", timeout=5)
    print(f"  收到 notifications/tools/list_changed = {got}；已收通知 = {s.note_methods()}")
    assert got, "未收到 tools/list_changed 通知"

    r = s.call_tool(PROBE)                      # 新代码真的在跑
    assert r["result"]["isError"] is False, f"探针工具调用失败: {r['result']['content']}"
    v1 = json.loads(r["result"]["content"][0]["text"])["probe_value"]
    print(f"  同一进程内 tools/call {PROBE} → isError=false, probe_value={v1}")
    assert v1 == 1

    # lib/*.py 变更也要立即生效（本次报的缺陷：改完 lib/engine.py 运行中的 MCP 仍是旧代码）
    notes_before = len(s.note_methods())
    touch(PROBE_LIB2, "VALUE = 2\n")
    r = s.call_tool(PROBE)
    v2 = json.loads(r["result"]["content"][0]["text"])["probe_value"]
    print(f"  同一进程内改 {PROBE_LIB2.name} → probe_value={v2}（lib 热重载生效 = {v2 == 2}）")
    assert v2 == 2, "lib 模块热重载未生效"
    same = len(s.note_methods()) == notes_before
    print(f"  仅 lib 变更时未多发 list_changed 通知 = {same}")
    assert same, "工具集没变却发了 tools/list_changed"

    touch(SRC, original)                        # 撤掉探针（仍在同一进程里观察）
    names = s.tool_names()
    print(f"  撤回探针后 tools/list → N'' = {len(names)}，探针已消失 = {PROBE not in names}")
    assert PROBE not in names, "撤回探针后仍然可见"
    assert len(names) == baseline_n
    assert s.alive()


def phase_c(s, baseline_n):
    touch(PROBE_LIB, BROKEN_SRC)
    print(f"  已放入语法错误文件 {PROBE_LIB}")

    r = s.call_tool("netdev_list")              # 变更后照常服务（用旧代码）
    assert r["result"]["isError"] is False, f"坏文件把服务打崩了: {r['result']['content']}"
    payload = json.loads(r["result"]["content"][0]["text"])
    print(f"  变更后 tools/call netdev_list → isError=false, devices={len(payload['devices'])}"
          f", 进程存活 = {s.alive()}")
    assert s.alive(), "服务器因 reload 失败而死"
    assert len(s.tool_names()) == baseline_n, "坏文件影响了工具集"

    found = s.wait_stderr(["hot-reload failed", PROBE_LIB.name], timeout=10)
    print(f"  stderr 出现 reload 失败记录 = {found}")
    for ln in s.reload_failures():
        print(f"    stderr> {ln}")
    assert found, f"stderr 没有 reload 失败记录；stderr={s.err}"

    # 失败条目不该被记为已重载：改好文件后下一次请求应重试并成功
    before = len(s.reload_failures())
    touch(PROBE_LIB, FIXED_SRC)
    s.call("ping", {})
    time.sleep(0.5)
    after = len(s.reload_failures())
    print(f"  修好文件后重试：失败记录 {before} → {after}（不再新增 = {after == before}）")
    assert after == before, f"修好后仍报 reload 失败：{s.reload_failures()[before:]}"
    assert s.alive()


def phase_d(s, original, baseline_n):
    """只改 netdev_list 的 description（不增删工具）也要生效并发出 list_changed。"""
    base_desc = s.tool_descriptions()["netdev_list"]
    assert DESC_MARK not in base_desc, f"基线描述已带探针: {base_desc!r}"

    notes_before = len(s.note_methods())
    touch(SRC, with_desc_mark(original))
    print(f"  已只改 netdev_list 的 description（+{DESC_MARK!r}，工具名/数量不变，pid={s.proc.pid}，未重启）")
    assert s.alive(), "改描述后进程不该退出"

    descs = s.tool_descriptions()
    print(f"  同一进程内 tools/list → 工具数 = {len(descs)}")
    print(f"    netdev_list 描述 = {descs['netdev_list']!r}")
    assert DESC_MARK in descs["netdev_list"], "描述热重载未生效"
    assert len(descs) == baseline_n, f"本用例不该增删工具：{baseline_n} → {len(descs)}"

    got = s.wait_note_count(notes_before + 1)
    print(f"  新收到通知 = {got}；通知总数 {notes_before} → {len(s.note_methods())}"
          f"，最后一条 = {s.note_methods()[-1]!r}")
    assert got, "改描述后未发出任何通知"
    assert s.note_methods()[-1] == "notifications/tools/list_changed", \
        f"最后一条通知不是 tools/list_changed：{s.note_methods()[-1]!r}"

    touch(SRC, original)                        # 还原描述，仍在同一进程里观察
    back = s.tool_descriptions()["netdev_list"]
    print(f"  还原描述后 tools/list → {back!r}，探针已消失 = {DESC_MARK not in back}")
    assert DESC_MARK not in back, "还原后描述里仍有探针"
    assert back == base_desc, f"描述未回到基线：{back!r} != {base_desc!r}"
    assert s.alive()


# ─────────────────────────────────────────────────────────────── 入口
def main():
    print("== netdev MCP 源码热重载测试 ==")
    print(f"源码: {SRC}")
    print(f"解释器: {PY}")
    if not PY.exists():
        print(f"\n✘ 找不到项目虚拟环境的 Python 解释器：{PY}")
        print("  请先在项目根目录创建 venv 并安装依赖：")
        if os.name == "nt":
            print("    python -m venv .venv")
            print("    .venv\\Scripts\\python.exe -m pip install -r requirements-win.txt")
        else:
            print("    python -m venv .venv")
            print("    .venv/bin/python -m pip install -r requirements.txt")
        print("\n  注意：Windows 开发请用 requirements-win.txt（含 pywinpty / keyring）")
        return 1

    md5_before = md5(SRC)
    original_bytes = SRC.read_bytes()
    original = original_bytes.decode("utf-8")
    print(f"测试开始 netdev_mcp.py md5 = {md5_before}（{len(original.splitlines())} 行）")

    fails = []
    state = {"n": 0}
    s = None
    try:
        s = McpServer()
        steps = [
            ("(a) 基线：initialize / tools/list / tools/call netdev_list", phase_a),
            ("(b) 行为级热重载：临时加工具 → 同一进程立即可见 + list_changed 通知",
             lambda srv: phase_b(srv, original, state["n"])),
            ("(c) 容错：lib 语法错误文件 → 不崩、继续服务、stderr 有记录",
             lambda srv: phase_c(srv, state["n"])),
            ("(d) 只改工具描述 → 同一进程内新描述可见 + list_changed 通知",
             lambda srv: phase_d(srv, original, state["n"])),
        ]
        for label, fn in steps:
            print(f"\n【{label}】")
            try:
                out = fn(s)
                if label.startswith("(a)"):
                    state["n"] = out
                print("  ✔ 通过")
            except Exception as e:
                fails.append(f"{label}: {type(e).__name__}: {e}")
                print(f"  ✘ {type(e).__name__}: {e}")
    except Exception as e:
        fails.append(f"拉起 MCP 子进程: {type(e).__name__}: {e}")
        print(f"\n✘ 拉起 MCP 子进程失败：{type(e).__name__}: {e}")
    finally:
        if s is not None:
            s.destroy()
        SRC.write_bytes(original_bytes)          # 字节级还原源码
        for tmp in (PROBE_LIB, PROBE_LIB2):     # 删掉临时 lib 文件
            if tmp.exists():
                tmp.unlink()
        for pat in ("_probe*", "_broken*"):       # 清掉可能落下的临时 pyc
            for pyc in (LIB / "__pycache__").glob(pat + ".pyc"):
                pyc.unlink()
        print("\n【清理】")
        now = md5(SRC)
        print(f"  netdev_mcp.py md5 = {now}（测试开始为 {md5_before}，一致 = {now == md5_before}）")
        print(f"  临时文件已删除 = {not PROBE_LIB.exists() and not PROBE_LIB2.exists()}"
              f"（{PROBE_LIB} / {PROBE_LIB2}）")
        print(f"  代码库里残留探针工具 = {PROBE in SRC.read_text(encoding='utf-8')}"
              f"，残留描述探针 = {DESC_MARK.strip() in SRC.read_text(encoding='utf-8')}")
        if now != md5_before:
            fails.append("清理：netdev_mcp.py 与测试开始的 md5 不一致")
        if PROBE_LIB.exists() or PROBE_LIB2.exists():
            fails.append("清理：临时 lib 文件仍然存在")
        if PROBE in SRC.read_text(encoding="utf-8"):
            fails.append("清理：netdev_mcp.py 里残留探针工具")
        if DESC_MARK.strip() in SRC.read_text(encoding="utf-8"):
            fails.append("清理：netdev_mcp.py 里残留描述探针")

    print("\n== 结果 ==")
    if fails:
        print("✘ 失败项：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("✔ 全部通过（基线 / 热重载 / 容错 / 描述变更 + 清理校验）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
