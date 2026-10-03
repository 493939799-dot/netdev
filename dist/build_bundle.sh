#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  构建"设备工具台（netdev）· macOS 安装包"
#  用法： bash build_bundle.sh [--with-python] [--out DIR]
#    --with-python  把独立 Python 也打进包（≈112MB → 完全离线安装）
#    （不带该参数则包内只有代码+离线依赖，目标机需自备 python3）
#  产物： <out>/<日期>_netdev设备工具台_macOS_<arch>_安装包.tar.gz
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$SRC/dist"; INST="$DIST/installer"
export PATH="$HOME/.local/bin:$SRC/bin:$HOME/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
WITH_PYTHON=0; OUT="$HOME/Desktop/workbuddy"
while [ $# -gt 0 ]; do
  case "$1" in
    --with-python) WITH_PYTHON=1 ;;
    --out) OUT="${2:-$OUT}"; shift ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac; shift
done
ARCH="$(uname -m)"; TS=$(date +%Y%m%d); VER="$(cat "$INST/VERSION" 2>/dev/null || echo 1.0.0)"
B="\033[1m"; G="\033[32m"; Y="\033[33m"; X="\033[0m"; ok(){ printf "  ${G}✔${X} %s\n" "$*"; }; warn(){ printf "  ${Y}!${X} %s\n" "$*"; }
# 每次构建用一个全新的独立目录，**不做 rm -rf**（2026-10-03 改）。
# 原来复用固定目录 dist/stage 并先 rm -rf，两次实测踩到两个坑：
#   ① 清不掉时（目录里有 .git 之类受保护内容）rm 被文件策略拒绝，
#      但脚本继续跑 —— rsync 是增量合并，于是产出「上一轮 + 这一轮」的并集，
#      混进已删除的文件，而且**没有任何报错**；
#   ② 破坏性删除本身就不该在构建脚本里。
# 改成 mktemp 唯一目录后，"混合包"从结构上就不可能发生。
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/netdev-build-XXXXXX")"
mkdir -p "$STAGE/payload" "$STAGE/runtime" "$OUT"

printf "${B}▶ 1/6 复制程序代码（不含配置/日志/备份/虚拟环境）${X}\n"
# ★ 2026-10-04：仓库根目录有几个指向本机绝对路径的软链
#   （devices.toml / connections.json → $SRC/config/…，bin/netdev → $SRC/netdev）。
#   之前没排除，结果包里带进了「指向 /Users/<我>/… 的断链」——
#   虽然不含真实内容（tar 只存链接本身），但①把我的用户名暴露给下载者；
#   ②目标机上它们是死链，看着像坏包。install.sh 第 4/6 步本来就会重建这三条，
#   所以这里直接不打进去最干净。
#   注意排除词按文件名匹配：config-template/ 里的是 devices.toml.example，不受影响。
rsync -a \
  --exclude '.venv' --exclude 'dist' --exclude 'logs' --exclude 'live' --exclude 'backups' \
  --exclude 'config' --exclude 'state' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.DS_Store' --exclude '.workbuddy' --exclude 'docs' --exclude 'tags' \
  --exclude 'tools/node_modules' --exclude 'tools/package-lock.json' \
  --exclude '.git' --exclude '.archive' --exclude '.github' --exclude '*.bak-*' \
  --exclude 'tools/*.mjs' --exclude 'tools/mk_*.py' --exclude 'tools/ui_doctor.sh' \
  --exclude 'tools/ui_probe*' --exclude 'conn_*_probe.mjs' --exclude 'verify_terminal*' \
  --exclude '/devices.toml' --exclude '/connections.json' --exclude '/bin/netdev' \
  --exclude '/.chk' \
  "$SRC/" "$STAGE/payload/netops/"
# 补全文件单独放（放进 payload/netops/config 会被安装器的 --exclude 'config/*' 挡掉）
mkdir -p "$STAGE/payload/completions"
cp -f "$SRC/config/_netdev" "$SRC/config/netdev.bash" "$STAGE/payload/completions/" 2>/dev/null || true
cp -f "$INST/README-从这里开始.txt.src" "$STAGE/payload/netops/README-从这里开始.txt" 2>/dev/null || true
ok "代码 $(du -sh "$STAGE/payload/netops" | cut -f1)"

printf "${B}▶ 2/6 离线依赖（netmiko/pyserial/scrapli 等）${X}\n"
if [ -d "$DIST/deps" ]; then cp -R "$DIST/deps" "$STAGE/runtime/deps"; ok "依赖 $(du -sh "$STAGE/runtime/deps" | cut -f1)"
else warn "缺少 $DIST/deps → 目标机需联网装依赖（uv pip install -r requirements.txt）"; fi
# requirements.txt：优先用源码树里的锁定版本，否则兜底
if [ -f "$SRC/requirements.txt" ]; then
  cp -f "$SRC/requirements.txt" "$STAGE/payload/netops/requirements.txt"
else
  "$SRC/.venv/bin/python" - <<'PY' > "$STAGE/payload/netops/requirements.txt" 2>/dev/null || printf 'netmiko\npyserial\nscrapli\n' > "$STAGE/payload/netops/requirements.txt"
import importlib.metadata as m
for p in ("netmiko","pyserial","scrapli"):
    try: print(f"{p}=={m.version(p)}")
    except Exception: print(p)
PY
fi

printf "${B}▶ 3/6 独立 Python 运行时${X}\n"
if [ "$WITH_PYTHON" = 1 ]; then
  # 运行时版本必须与离线依赖 dist/deps 编译所用的 Python 版本一致，
  # 否则 cffi/_yaml 等 .so（cpython-31X-darwin.so）加载即崩。
  # 从 deps 里的 .so 反推版本号；取不到再退回 3.12。
  PYVER="$(find "$DIST/deps" -name '_cffi_backend.cpython-*-darwin.so' 2>/dev/null | head -1 | sed -E 's/.*cpython-([0-9]+)-darwin.*/\1/')"
  PYVER="${PYVER:-312}"
  PYVER_MAJMIN="${PYVER:0:1}.${PYVER:1}"
  PYDIR="$(ls -d "$HOME/.local/share/uv/python/cpython-${PYVER_MAJMIN}"* 2>/dev/null | tail -1)"
  if [ -n "$PYDIR" ]; then
    cp -R "$PYDIR" "$STAGE/runtime/python"; ok "已打入 Python（$(du -sh "$STAGE/runtime/python" | cut -f1)）→ 目标机无需自备 Python"
  else
    uv python install "$PYVER_MAJMIN" >/dev/null 2>&1 || true
    PYDIR="$(ls -d "$HOME/.local/share/uv/python/cpython-${PYVER_MAJMIN}"* 2>/dev/null | tail -1)"
    [ -n "$PYDIR" ] && cp -R "$PYDIR" "$STAGE/runtime/python" && ok "已下载并打入 Python（$(du -sh "$STAGE/runtime/python" | cut -f1)）" || warn "取独立 Python 失败，跳过（目标机需自备 python3）"
  fi
else
  warn "按参数跳过（目标机需自备 python3；加 --with-python 可完全离线）"
fi

printf "${B}▶ 4/6 安装器与模板${X}\n"
cp -f "$INST/install.sh" "$INST/uninstall.sh" "$INST/README-安装说明.txt" "$INST/VERSION" "$STAGE/"
mkdir -p "$STAGE/config-template"
# config-template 由仓库里的 config/*.example 生成 —— 模板的**单一真源**是 config/。
# dist/installer/ 下**不再留副本**（2026-10-03 两次踩坑：历史上这里既没有副本、
# install.sh 读不到；后来我建了副本，又变回"同一份东西存两处"的老问题）。
# 文件名按 install.sh 期望的来（它要 config-template/devices.toml.example 和
# config-template/AGENTS.workspace.md，两种命名都有 —— 历史上就是这么不一致的）。
add_tpl() {  # $1=源(.example)  $2=包内名
  [ -f "$SRC/config/$1" ] || { printf "  ✘ 缺模板 config/%s\n" "$1"; exit 1; }
  cp -f "$SRC/config/$1" "$STAGE/config-template/$2"
}
add_tpl devices.toml.example       devices.toml.example
add_tpl connections.json.example   connections.json
add_tpl pi-commands.json.example   pi-commands.json
add_tpl AGENTS.workspace.md.example AGENTS.workspace.md
add_tpl _netdev.example            _netdev
add_tpl netdev.bash.example        netdev.bash
ok "配置模板就绪（$STAGE/config-template，共 $(ls -1 "$STAGE/config-template" | wc -l | tr -d ' ') 个）"
# 关于开机自启：install.sh 现在直接调 `netdev ui install` / `netdev logs install-agent`
# 由产品自己程序化生成 plist，所以**不再需要 dist/installer/launchd/*.tpl 模板**。
# 2026-10-03：菜单的**单一真源**是 bin/设备工具台.command。
# 原来这里另有一份 dist/installer/tools-menu.command，安装时 install.sh 会用它
# 覆盖 bin/设备工具台.command —— 两份副本各自演化，导致源码里修好的菜单
# （netdev ui 自动拉起 / 实时状态 / 「网页服务」菜单）在**新装机器上全部丢失**。
# 同一份东西存两处就一定会不一致，这里从根上改成一处。
[ -f "$SRC/bin/设备工具台.command" ] || { echo "  ✘ 找不到 $SRC/bin/设备工具台.command"; exit 1; }
cp -f "$SRC/bin/设备工具台.command" "$STAGE/tools-menu.command"
chmod +x "$STAGE/install.sh" "$STAGE/uninstall.sh" "$STAGE/tools-menu.command"
ok "安装器就绪（菜单取自 bin/设备工具台.command，仓库里只有这一份）"

printf "${B}▶ 5/6 打包${X}\n"
NAME="${TS}_netdev设备工具台_macOS_${ARCH}_安装包"
mkdir -p "$STAGE/$NAME"
for item in payload runtime config-template install.sh uninstall.sh README-安装说明.txt tools-menu.command VERSION; do
  mv "$STAGE/$item" "$STAGE/$NAME/" 2>/dev/null || true
done
( cd "$STAGE" && tar -czf "$OUT/$NAME.tar.gz" "$NAME" )
ok "$OUT/$NAME.tar.gz  （$(du -sh "$OUT/$NAME.tar.gz" | cut -f1)）"

printf "${B}▶ 6/6 校验${X}\n"
( cd "$OUT" && shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
ok "校验和：$(cut -c1-24 "$OUT/$NAME.tar.gz.sha256")…"
echo "$NAME.tar.gz" > "$DIST/last_bundle.txt"
printf "\n${B}完成${X}：把这个 .tar.gz（和 .sha256）拷到目标 Mac，然后：\n"
printf "  tar -xzf %s.tar.gz && cd %s && bash install.sh --dry-run\n" "$NAME" "$NAME"
