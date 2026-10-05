#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：前端 index.html 的两条硬约束（2026-10-05）。

为什么要有这个文件（真实事故驱动）：
  1. index.html 是【单文件、内联 JS、无构建步骤】——语法错了不会在开发时
     报错，只会在用户打开页面时白屏。实测踩到：插入一段启动代码时把原有
     `if(...) ... else ...` 拆散，`else` 悬空 ⇒ 整页 JS 不执行。
     所以：任何改动后必须做语法检查（node --check）。
  2. AI 会话持久化（refresh 不失忆）依赖三个函数成对存在：
     aiSessSave / aiSessRestore / 启动时调用 aiSessRestore —— 少一个
     就是"改了不报错、但刷新仍然失忆"。

用法：
    python3 tests/test_ui_js_syntax.py
无 node 时跳过语法检查（只跑存在性断言），不让环境缺件变成假红。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
HTML = HERE.parent / "ui" / "static" / "index.html"

NODE_CANDIDATES = [
    "/Users/mac/.workbuddy/binaries/node/versions/22.22.2-3/bin/node",
    "/usr/local/bin/node", "/opt/homebrew/bin/node",
]

OK = 0
NG = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, NG
    if cond:
        OK += 1
        print(f"  OK  {name}")
    else:
        NG += 1
        FAILS.append(name + (f" —— {detail}" if detail else ""))
        print(f"  NG  {name}" + (f"  ← {detail}" if detail else ""))


def find_node() -> str:
    for p in NODE_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("node") or ""


def main() -> int:
    src = HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    print(f"== 1. 内联 JS 语法（{len(scripts)} 段 / {sum(len(s) for s in scripts)} 字符）==")
    check("找到内联 script", bool(scripts))
    node = find_node()
    if not node:
        print("  ~   未找到 node，跳过语法检查（不算失败）")
    else:
        p = pathlib.Path(tempfile.mkdtemp(prefix="netdev-js-")) / "bundle.js"
        p.write_text("\n;\n".join(scripts), encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        first = (r.stderr or "").split("^^^^")[0].splitlines()[-3:]
        check("node --check 通过（语法错 = 用户白屏）", r.returncode == 0,
              " / ".join(x.strip() for x in first if x.strip()))

    print("== 2. AI 会话持久化三件套 ==")
    check("定义 aiSessSave", "function aiSessSave(" in src)
    check("定义 aiSessRestore", "async function aiSessRestore(" in src)
    check("启动时调用 aiSessRestore", src.count("aiSessRestore()") >= 2)
    check("存储键存在", "netdev-ui-ai-sess" in src)
    check("恢复走 /api/ai/list 核对", "/api/ai/list" in src)
    check("开会话时上报 name/device",
          "device:dev,name" in src.replace(" ", ""))
    check("新建/切换/关闭都会保存", src.count("aiSessSave();") >= 3,
          f"实际 {src.count('aiSessSave();')} 处")

    print("== 3. 别把首页启动链踩断（曾经的 else 悬空事故）==")
    check("startIconize 的 if/else 仍然成对", re.search(
        r"if\(document\.readyState==='loading'\)\s*document\.addEventListener\('DOMContentLoaded',\s*startIconize\);\s*else\s*startIconize\(\);",
        src) is not None)

    print(f"\n共 {OK + NG} 项：OK {OK} / NG {NG}")
    for x in FAILS:
        print("  -", x)
    return 1 if NG else 0


if __name__ == "__main__":
    sys.exit(main())
