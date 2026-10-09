#!/usr/bin/env python3
"""netdev Windows x64 安装包构建脚本（CI / 本机皆可跑）。

产物：netdev-windows-x64-installer.zip + .sha256（固定名，供 releases/latest 直链）。

结构（与 2026-10-07 手工首版一致）：
  netdev-windows-x64-installer/
    install.ps1 / uninstall.ps1 / 一键体检.cmd / 一键体检.ps1
    netdev-install.exe / netdev-toolbox.exe / netdev.ico
    README-Windows安装说明.txt / VERSION / config-template/
    payload/netops/            ← 源码取自 git archive HEAD（不是本机工作区！）

设计要点：
  1. payload 用 `git archive HEAD` 生成 —— 只含 git 跟踪的文件，
     本机的 config/devices.toml、logs/、backups/ 天然进不来。
  2. zip 统一 UTF-8 文件名 + 正斜杠路径（根治旧包 GBK 乱码）。
  3. 内置泄密断言：FTAAM5SL / 真实网段 / 敏感文件，命中即失败。

用法：
  python dist/windows/build_windows.py --out <输出目录>
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
SRC = Path(__file__).resolve().parent / "installer-src"
NAME = "netdev-windows-x64-installer"

# ── 泄密/误打包黑名单 ─────────────────────────────────────────────
NEVER_PATHS = (".git/", "config/devices.toml", "config/direct.json",
               "logs/", "backups/", ".venv/", "__pycache__")
NEVER_BYTES = (b"FTAAM5SL", b"192.168.10.4")


def sh(cmd, **kw):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"✘ 命令失败：{' '.join(map(str, cmd))}\n{r.stderr}")
    return r.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="产物输出目录")
    ap.add_argument("--version", default=None,
                    help="包内 VERSION（CI 传 tag 名；缺省读 dist/installer/VERSION）")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.version:
        version = args.version.lstrip("vV")
    else:
        version = (ROOT / "dist/installer/VERSION").read_text(encoding="utf-8").strip()
    if not version:
        sys.exit("✘ 无法确定版本号")
    print(f"版本：{version}")

    stage = Path(tempfile.mkdtemp(prefix="nw-build-")) / NAME
    payload = stage / "payload" / "netops"

    # ── 1. payload：git archive（干净 checkout 的等价物）──────────
    payload.mkdir(parents=True)
    ga = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT,
                        stdout=subprocess.PIPE, check=True)
    subprocess.run(["tar", "-x", "-C", str(payload)],
                   input=ga.stdout, check=True)
    # Windows 包不需要这些（与首版手工包对齐，也减小体积）
    for drop in ("dist", ".github", "docs", "tests"):
        shutil.rmtree(payload / drop, ignore_errors=True)

    # ── 2. 叠加 Windows 专属文件 ─────────────────────────────────
    # netdev-mcp.cmd 是 Windows 专用启动器 → 只进 payload
    mcp_cmd = SRC / "netdev-mcp.cmd"
    if not mcp_cmd.exists():
        sys.exit("✘ installer-src 缺 netdev-mcp.cmd")
    shutil.copy2(mcp_cmd, payload / "netdev-mcp.cmd")
    # 其余 installer-src 内容 → 包顶层
    for item in sorted(SRC.iterdir()):
        if item.name == "netdev-mcp.cmd":
            continue
        dest = stage / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    # ── 3. VERSION ───────────────────────────────────────────────
    (stage / "VERSION").write_text(version, encoding="utf-8")

    # ── 4. 断言：必含 / 必不含 ───────────────────────────────────
    must = ["install.ps1", "uninstall.ps1", "VERSION",
            "config-template/devices.toml.example",
            "payload/netops/netdev_cli.py", "payload/netops/netdev_mcp.py",
            "payload/netops/netdev-mcp.cmd", "payload/netops/ui/server.py"]
    for m in must:
        if not (stage / m).exists():
            sys.exit(f"✘ 组装缺文件：{m}")
    print(f"必含文件 {len(must)} 项 ✔")

    # ── 5. 打 zip（UTF-8 名 + 正斜杠）───────────────────────────
    zip_path = out / f"{NAME}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                zf.write(f, Path(NAME) / f.relative_to(stage))
    n_files = sum(1 for _ in zipfile.ZipFile(zip_path).namelist())

    # ── 6. 泄密终检（对 zip 内容逐文件扫字节）────────────────────
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            for bad in NEVER_PATHS:
                if name.endswith(".gitkeep"):
                    continue          # 空目录占位标记，无害
                if f"/{name}".find(bad.strip("/") + "/") >= 0 or name.startswith(bad):
                    sys.exit(f"✘ 包内出现敏感路径：{name}")
            data = zf.read(info)
            for bad in NEVER_BYTES:
                if bad in data:
                    sys.exit(f"✘ 包内 {name} 命中泄密串：{bad.decode()}")
    print(f"泄密终检 ✔（路径 {len(NEVER_PATHS)} 项 / 内容 {len(NEVER_BYTES)} 项）")

    # ── 7. sha256（与 macOS 包同格式：shasum -a 256 输出）────────
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (out / f"{NAME}.zip.sha256").write_text(f"{digest}  {NAME}.zip\n", encoding="utf-8")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"产物：{zip_path}（{n_files} 个文件，{size_mb:.1f} MB）")
    print(f"校验：{out / (NAME + '.zip.sha256')}")
    shutil.rmtree(stage.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
