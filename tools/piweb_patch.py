#!/usr/bin/env python3
"""piweb_patch.py —— 让 pi-web-ui 左栏按钮“作用在当前选中的终端”。

宿主原行为（前端代码）：
    const s = e.name || e.command;
    const i = n.terminals.find(f => f.title === s);      // ← 按“标题”找同名终端
    if (i) { 原地重启它并运行命令 } else { 新建一个终端并运行 }
于是反复点同一个按钮，永远复用**那个标题的终端**，与“你当前选中哪个终端”无关。

补丁（一个 token）：
    i = n.terminals.find(f => f.id === N && !f.agentBash) || n.terminals.find(f => f.title === s);
    （N = 该组件里“当前选中的终端 id”；!f.agentBash = 不劫持 AI 的 bash 终端）

★ 为什么还要“换新文件名”（缓存击穿）：
  Vite 产物是 `TerminalPanel-<hash>.js` + `index-<hash>.js`，服务器按
  `Cache-Control: public, max-age=31536000, immutable` 发；面板又是**懒加载**，
  所以「改内容不改文件名」时，浏览器（和 PWA 的 cache-first service worker）会一直用旧副本，
  硬刷新的绕过缓存也管不到之后才发出的动态 import。
  做法：把改好的文件另存为 `*-p1.js`，改写入口 chunk 的 import，再把 index.html 的 script src
  指到 `index-*-p1.js`（index.html 是 max-age=0，每次都会拿到新的）→ 全链路都是新 URL，一次普通刷新即生效。

用法：
    piweb_patch.py            # 打补丁 + 缓存击穿（幂等）
    piweb_patch.py --check    # 体检：patched+busted / patched-only / original / missing
    piweb_patch.py --revert   # 全部还原（删 -*-p1.js、index.html 指回原入口、面板恢复原内容）
    piweb_patch.py --verify   # 走 HTTP 验证“浏览器拿到的确实是打过补丁的代码”
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
import time
import urllib.request

HOME = pathlib.Path.home()
WEB = HOME / ".npm-global/lib/node_modules/pi-web-ui/web/dist"
ASSETS = WEB / "assets"
INDEX_HTML = WEB / "index.html"
BAK = HOME / "netops/.workbuddy/bak"
SUFFIX = "-p2"
PORT = 8899

ORIG = "const s=e.name||e.command,i=n.terminals.find(f=>f.title===s);"
PATCHED = ("const s=e.name||e.command,"
           "i=n.terminals.find(f=>f.id===N&&!f.agentBash)||n.terminals.find(f=>f.title===s);")
MARK = "f.id===N&&!f.agentBash"
BADGE_FROM = 'r("commands")'
BADGE_TO = 'r("commands")+"·p2"'          # 左栏标题上的可见标记（用于一眼确认补丁是否生效）


# ── 基本定位 ────────────────────────────────────────────────
def _entry_name() -> str:
    """index.html 里 <script type=module src="/assets/index-….js"> 的名字（可能已带 -p1）"""
    if not INDEX_HTML.exists():
        return ""
    m = re.search(r'src="/assets/(index-[^"]+\.js)"', INDEX_HTML.read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def _orig_entry() -> pathlib.Path | None:
    for f in sorted(ASSETS.glob("index-*.js")):
        if not f.stem.endswith(SUFFIX):
            return f
    return None


def _orig_panel() -> pathlib.Path | None:
    for f in sorted(ASSETS.glob("TerminalPanel-*.js")):
        if not f.stem.endswith(SUFFIX):
            return f
    return None


def _panel_ref(entry_file: pathlib.Path) -> str:
    m = re.search(r"TerminalPanel-[A-Za-z0-9_.-]+\.js", entry_file.read_text(encoding="utf-8", errors="replace"))
    return m.group(0) if m else ""


# ── 状态检查 ────────────────────────────────────────────────
def check() -> tuple[str, str]:
    entry = _entry_name()
    panel_orig = _orig_panel()
    if not entry or not panel_orig or not INDEX_HTML.exists():
        return "missing", f"没找到 pi-web-ui 前端产物（{ASSETS}）——装法变了？"
    busted = entry.endswith(SUFFIX + ".js")
    panel_name = _panel_ref(ASSETS / entry)
    panel_body = (ASSETS / panel_name).read_text(encoding="utf-8", errors="replace") if panel_name else ""
    src_patched = MARK in panel_orig.read_text(encoding="utf-8", errors="replace")
    if busted and MARK in panel_body and "·p2" in panel_body:
        return "patched+busted", f"{panel_name}：已打补丁 + 已换新文件名 + 左栏会显示“命令·p2”标记"
    if src_patched:
        return "patched-only", f"{panel_orig.name}：内容已打补丁，但文件名没换（浏览器可能还在用旧缓存 → 重跑 netdev web patch）"
    if ORIG in panel_orig.read_text(encoding="utf-8", errors="replace"):
        return "original", f"{panel_orig.name}：未打补丁（点按钮仍按标题复用终端）"
    return "missing", f"{panel_orig.name}：找不到补丁锚点（pi-web-ui 升级后代码变了，需要人看一眼）"


def verify_http(port: int = PORT) -> tuple[bool, str]:
    """走 HTTP 把浏览器会拿到的链路抓一遍：/ → 入口 chunk → 面板 chunk → 找补丁标记。"""
    base = f"http://127.0.0.1:{port}"
    try:
        html = urllib.request.urlopen(f"{base}/", timeout=5).read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"取不到 {base}/：{type(e).__name__}（服务没起？）"
    m = re.search(r'src="/assets/(index-[^"]+\.js)"', html)
    if not m:
        return False, "index.html 里没找到入口 chunk"
    entry = m.group(1)
    try:
        etext = urllib.request.urlopen(f"{base}/assets/{entry}", timeout=8).read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"取不到入口 {entry}：{e}"
    pm = re.search(r"TerminalPanel-[A-Za-z0-9_.-]+\.js", etext)
    if not pm:
        return False, f"入口 {entry} 里没找到面板 chunk"
    panel = pm.group(0)
    try:
        ptext = urllib.request.urlopen(f"{base}/assets/{panel}", timeout=8).read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"取不到面板 {panel}：{e}"
    ok = MARK in ptext
    return ok, (f"HTML→{entry} →{panel}：补丁标记 {'在 ✓' if ok else '不在 ✘'}（{'浏览器一次普通刷新即可' if ok else '仍会加载旧代码'}）")


# ── 备份 / 打补丁 / 缓存击穿 ─────────────────────────────────
def _backup(f: pathlib.Path) -> pathlib.Path:
    BAK.mkdir(parents=True, exist_ok=True)
    dst = BAK / f"piweb_{f.name}_{time.strftime('%Y%m%d_%H%M%S')}_orig.js"
    shutil.copy2(f, dst)
    return dst


def bump_sw() -> bool:
    """把 PWA 的 SW 缓存版本从 v1 提到 v2：下一个页面加载会激活新 SW 并删掉旧缓存。
    （不提升的话，sw.js 对 hashed 资源是 cache-first，旧副本会一直命中。）"""
    sw = WEB / "sw.js"
    if not sw.exists():
        return False
    s = sw.read_text(encoding="utf-8")
    if '"pi-web-ui-static-v2"' in s:
        return False
    _backup(sw)
    s = s.replace('"pi-web-ui-static-v1"', '"pi-web-ui-static-v2"').replace('"pi-web-ui-shell-v1"', '"pi-web-ui-shell-v2"')
    sw.write_text(s, encoding="utf-8")
    print("· sw.js 缓存版本 v1 → v2（旧 SW 缓存会在下次加载时被清）")
    return True


def _normalize_index_html():
    """把 index.html 指回“不带补丁后缀”的原始入口（幂等；避免删旧产物时把入口删没了）。"""
    entry = _entry_name()
    if re.search(r"-p\d+\.js$", entry or ""):
        orig = re.sub(r"-p\d+\.js$", ".js", entry)
        html = INDEX_HTML.read_text(encoding="utf-8")
        INDEX_HTML.write_text(html.replace(entry, orig), encoding="utf-8")
        print(f"· index.html 已指回原始入口 {orig}")


def _drop_old_suffixes(keep: str = SUFFIX):
    """删掉**其它**后缀的旧产物（保留当前后缀，免得同一 URL 内容不一致）。"""
    for f in ASSETS.glob("*-p*.js"):
        m = re.search(r"(-p\d+)\.js$", f.name)
        if m and m.group(1) != keep:
            try:
                f.unlink(); print(f"· 清掉旧产物 {f.name}")
            except Exception:
                pass


def apply() -> int:
    _normalize_index_html()
    _drop_old_suffixes()
    state, note = check()
    print(f"· 体检：{note}")
    if state == "patched+busted":
        print("✔ 已经是“打过补丁 + 换过文件名”的状态，不用动。")
        return 0
    if state == "missing":
        print("✘ 无法自动处理（产物结构对不上）。请人工看，或告诉我再适配。")
        return 2

    panel = _orig_panel()
    entry = _orig_entry()
    if not panel or not entry:
        print("✘ 找不到面板/入口 chunk。")
        return 2

    # ① 面板内容打补丁（幂等）
    ptext = panel.read_text(encoding="utf-8")
    if MARK not in ptext:
        if ORIG not in ptext:
            print(f"✘ 锚点没命中（{panel.name}），pi-web-ui 可能升级了。")
            return 2
        print(f"  备份面板：{_backup(panel).relative_to(HOME)}")
        ptext = ptext.replace(ORIG, PATCHED, 1)
        panel.write_text(ptext, encoding="utf-8")
    # 可见标记（幂等）：左栏标题 → 命令·p2
    if "·p2" not in ptext:
        if BADGE_FROM not in ptext:
            print(f"✘ 找不到标记锚点 {BADGE_FROM}")
            return 2
        ptext = ptext.replace(BADGE_FROM, BADGE_TO)
        panel.write_text(ptext, encoding="utf-8")

    # ② 面板另存为新文件名（缓存击穿）
    new_panel = ASSETS / (panel.stem + SUFFIX + ".js")
    new_panel.write_text(ptext, encoding="utf-8")

    # ③ 入口 chunk：把 import 改成新面板名，并另存为新文件名
    etext = entry.read_text(encoding="utf-8")
    if panel.name not in etext:
        print(f"✘ 入口 {entry.name} 里没有引用 {panel.name}，无法改写。")
        return 2
    print(f"  备份入口：{_backup(entry).relative_to(HOME)}")
    etext = etext.replace(panel.name, new_panel.name)
    new_entry = ASSETS / (entry.stem + SUFFIX + ".js")
    new_entry.write_text(etext, encoding="utf-8")

    # ④ index.html 指到新入口
    html = INDEX_HTML.read_text(encoding="utf-8")
    if entry.name not in html:
        print(f"✘ index.html 里没有引用 {entry.name}。")
        return 2
    _backup(INDEX_HTML)
    INDEX_HTML.write_text(html.replace(entry.name, new_entry.name), encoding="utf-8")

    bump_sw()
    state2, note2 = check()
    print(f"✔ 已打补丁 + 缓存击穿：{panel.name} → {new_panel.name}；{entry.name} → {new_entry.name}；index.html 已更新")
    print(f"  复检：{note2}")
    ok, vmsg = verify_http()
    print(f"  HTTP 验证：{vmsg}")
    print("  生效方式：浏览器里**普通刷新**一次即可（不需要清缓存/硬刷新）")
    return 0 if state2 == "patched+busted" and ok else 3


def revert() -> int:
    entry = _entry_name()
    patched_files = [f for f in ASSETS.glob("*-" + SUFFIX.lstrip("-") + ".js")]
    # ① index.html 指回原入口
    if entry.endswith(SUFFIX + ".js"):
        orig_entry = entry[: -len(SUFFIX + ".js")] + ".js"
        html = INDEX_HTML.read_text(encoding="utf-8")
        _backup(INDEX_HTML)
        INDEX_HTML.write_text(html.replace(entry, orig_entry), encoding="utf-8")
        print(f"✔ index.html → {orig_entry}")
    # ② 删掉 -p1 产物
    for f in patched_files:
        f.unlink()
        print(f"✔ 已删除 {f.name}")
    # ③ sw.js 还原
    sw = WEB / "sw.js"
    sbaks = sorted(BAK.glob("piweb_sw.js_*"), key=lambda p: p.stat().st_mtime)
    if sw.exists() and sbaks:
        shutil.copy2(sbaks[-1], sw)
        print(f"✔ sw.js 已还原 ← {sbaks[-1].name}")
    # ④ 面板内容从最近备份还原
    panel = _orig_panel()
    baks = sorted(BAK.glob("piweb_TerminalPanel-*.js"), key=lambda p: p.stat().st_mtime)
    if panel and baks:
        shutil.copy2(baks[-1], panel)
        print(f"✔ 面板内容已还原 ← {baks[-1].name}")
    print("  生效：浏览器普通刷新一次")
    return 0


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if arg == "--check":
        st, note = check()
        print(f"[{st}] {note}")
        sys.exit(0 if st == "patched+busted" else 1)
    if arg == "--verify":
        ok, msg = verify_http()
        print(("✔ " if ok else "✘ ") + msg)
        sys.exit(0 if ok else 1)
    if arg == "--revert":
        sys.exit(revert())
    sys.exit(apply())
