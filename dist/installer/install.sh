#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  设备工具台（netdev）· macOS 一键安装
#  用法：
#     bash install.sh                 # 正常安装（装到 ~/netops）
#     bash install.sh --dry-run       # 只看会做什么，不动任何文件
#     bash install.sh --no-launchd    # 不装开机自启
#     bash install.sh --piweb         # 顺便用 npm 安装 pi-web-ui（需联网）
#     bash install.sh --prefix DIR    # 换安装目录（默认 $HOME/netops）
#     bash install.sh --workdir DIR   # 终端页命令写到哪个工作目录（默认 $HOME）
#  特性：幂等（重复跑只更新程序，保留配置与备份）；离线可用（自带 Python + 依赖）
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
export LC_ALL=C          # macOS 的 sed 在多字节 locale 下会报 "illegal byte sequence"

# ── 参数 ────────────────────────────────────────────────────────────────────
DRY=0; NO_LAUNCHD=0; WITH_PIWEB=0
PREFIX="$HOME/netops"; WORKDIR="$HOME"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-launchd) NO_LAUNCHD=1 ;;
    --piweb) WITH_PIWEB=1 ;;
    --prefix) PREFIX="${2:-$HOME/netops}"; shift ;;
    --workdir) WORKDIR="${2:-$HOME}"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）"; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; X="\033[0m"
ok()   { printf "  ${G}✔${X} %s\n" "$*"; }
warn() { printf "  ${Y}!${X} %s\n" "$*"; }
bad()  { printf "  ${R}✘${X} %s\n" "$*"; }
step() { printf "\n${B}▶ %s${X}\n" "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf "    ${D}[dry-run] %s${X}\n" "$*"; else eval "$@"; fi; }

printf "${B}╔══════════════════════════════════════════════════════════╗${X}\n"
printf "${B}║   设备工具台（netdev）· macOS 安装                         ║${X}\n"
printf "${B}╚══════════════════════════════════════════════════════════╝${X}\n"
VERSION="$(cat "$HERE/VERSION" 2>/dev/null || echo dev)"
printf "  版本 %s ｜ 安装目录 %s ｜ 工作目录 %s\n" "$VERSION" "$PREFIX" "$WORKDIR"
[ "$DRY" = 1 ] && printf "  【dry-run：只预览，不动文件】\n"

# ── 1. 环境检查 ─────────────────────────────────────────────────────────────
step "1/8 环境检查"
if [ "$(uname -s)" != "Darwin" ]; then bad "本安装包仅支持 macOS"; exit 1; fi
ok "macOS $(sw_vers -productVersion 2>/dev/null) / $(uname -m)"
ARCH="$(uname -m)"

# Python：优先自带，其次系统
PY=""
if [ -x "$HERE/runtime/python/bin/python3" ]; then PY="$HERE/runtime/python/bin/python3"; ok "使用安装包自带的 Python（$("$PY" -V 2>&1)）"
elif command -v python3 >/dev/null; then PY="$(command -v python3)"; ok "使用系统 Python（$("$PY" -V 2>&1)）"
else warn "没有 python3 也没有自带运行时 → 请先装 Xcode 命令行工具：xcode-select --install"; fi

# tmux（同屏会话必需）
if command -v tmux >/dev/null || [ -x "$HOME/homebrew/bin/tmux" ] || [ -x /opt/homebrew/bin/tmux ]; then
  ok "tmux 已就绪"
else
  warn "缺 tmux（同屏会话/回看需要它）。装法：brew install tmux   （没有 brew 可先装 Homebrew）"
  warn "缺 tmux 也能用：netdev 会退化为直接连接（但没有多人同屏与历史回看）"
fi

# node/npm（pi-web-ui 需要）
if command -v node >/dev/null; then ok "node $(node -v) / npm $(npm -v 2>/dev/null)"
else warn "缺 node/npm（网页界面 pi-web-ui 需要）。装法：brew install node"; fi

# ── 2. 落盘：程序 ───────────────────────────────────────────────────────────
step "2/8 安装程序到 $PREFIX"
if [ -e "$PREFIX" ] && [ ! -e "$PREFIX/netdev_cli.py" ]; then
  bad "$PREFIX 已存在但不是 netdev 目录 → 请换 --prefix 或先移走"; exit 1
fi
if [ -e "$PREFIX/backups" ]; then ok "检测到已有安装：只更新程序，保留 config/ 与 backups/"; fi
run "mkdir -p '$PREFIX'"
# 复制代码（不覆盖用户配置与备份）
if [ "$DRY" = 0 ]; then
  rsync -a --exclude '.venv' --exclude 'logs/*' --exclude 'live/*' \
        --exclude 'backups/*' --exclude 'config/*' --exclude 'state' \
        --exclude '__pycache__' --exclude '.DS_Store' --exclude 'dist' \
        "$HERE/payload/netops/" "$PREFIX/" 2>/dev/null \
    || { mkdir -p "$PREFIX"; cp -R "$HERE/payload/netops/." "$PREFIX/"; }
else
  printf "    ${D}[dry-run] rsync payload/netops/ → %s/${X}\n" "$PREFIX"
fi
ok "程序文件已就位（netdev / lib / tools / tests）"

# ── 3. 运行环境（venv + 依赖，离线优先） ───────────────────────────────────
step "3/8 建 Python 环境并装依赖（离线优先）"
install_deps() {
  local VENV="$1"
  local REQ="$2"
  local SP="$("$VENV/bin/python" -c "import sysconfig; print(sysconfig.get_path('purelib'))" 2>/dev/null)"
  [ -z "$SP" ] && { warn "无法定位 venv site-packages"; return 1; }
  mkdir -p "$SP"

  # 1) 优先尝试离线依赖
  # 注：沙箱/特殊属性可能导致 cp 返回非零，但文件已就位；以导入测试为最终判据
  if [ -d "$HERE/runtime/deps" ] && [ -n "$SP" ]; then
    cp -R "$HERE/runtime/deps/." "$SP/" 2>/dev/null || true
    ok "已拷入离线依赖（netmiko/pyserial/scrapli 等）"
  fi

  # 2) 导入测试：核心三个包必须都能导入
  local IMPORT_OK=0
  if "$VENV/bin/python" -c "import netmiko, serial, scrapli" >/dev/null 2>&1; then
    ok "依赖导入测试通过（netmiko/serial/scrapli）"
    IMPORT_OK=1
  else
    warn "离线依赖导入测试未通过（Python 版本/平台不匹配）→ 尝试在线安装"
  fi

  # 3) 离线失败则联网安装
  if [ "$IMPORT_OK" != 1 ]; then
    if command -v uv >/dev/null; then
      if uv pip install --python "$VENV/bin/python" -r "$REQ" >/dev/null 2>&1; then
        ok "已联网用 uv 装好依赖"
        IMPORT_OK=1
      else
        warn "uv 在线安装失败"
      fi
    elif [ -x "$VENV/bin/pip" ]; then
      if "$VENV/bin/pip" install -r "$REQ" >/dev/null 2>&1; then
        ok "已联网用 pip 装好依赖"
        IMPORT_OK=1
      else
        warn "pip 在线安装失败"
      fi
    fi
  fi

  # 4) 最终验证
  if [ "$IMPORT_OK" = 1 ] || "$VENV/bin/python" -c "import netmiko, serial, scrapli" >/dev/null 2>&1; then
    ok "依赖就绪"
    return 0
  else
    warn "依赖仍未就绪 → 设备读写功能不可用（可联网后重跑 install.sh）"
    return 1
  fi
}

if [ -n "$PY" ]; then
  if [ "$DRY" = 0 ]; then
    if [ ! -x "$PREFIX/.venv/bin/python" ]; then
      "$PY" -m venv "$PREFIX/.venv" >/dev/null 2>&1 || "$PY" -m venv --without-pip "$PREFIX/.venv"
    fi
    install_deps "$PREFIX/.venv" "$PREFIX/requirements.txt"
  else
    printf "    ${D}[dry-run] %s -m venv %s/.venv ；拷入 runtime/deps 并验证导入${X}\n" "$PY" "$PREFIX"
  fi
else
  warn "跳过（没有 Python）"
fi

# ── 4. 配置收编（config/ 真身 + 外面软链） ──────────────────────────────────
step "4/8 收编配置到 $PREFIX/config（外面只留软链）"
if [ "$DRY" = 0 ]; then
  mkdir -p "$PREFIX/config/state"
  # devices.toml：没有就从模板生成（有就保留）
  if [ ! -e "$PREFIX/config/devices.toml" ]; then
    sed "s|__HOME__|$HOME|g" "$HERE/config-template/devices.toml.example" > "$PREFIX/config/devices.toml"
    ok "生成了设备清单模板 config/devices.toml（请按需改）"
  else ok "已有 config/devices.toml，保留"; fi
  [ -e "$PREFIX/config/connections.json" ] || { cp -f "$HERE/config-template/connections.json" "$PREFIX/config/connections.json" 2>/dev/null || echo '[]' > "$PREFIX/config/connections.json"; }
  # 外面软链（绝对路径，避免断链）
  for pair in "devices.toml:devices.toml" "connections.json:connections.json"; do
    link="${pair%%:*}"; tgt="$PREFIX/config/${pair##*:}"
    if [ -L "$PREFIX/$link" ] || [ ! -e "$PREFIX/$link" ]; then rm -f "$PREFIX/$link"; ln -s "$tgt" "$PREFIX/$link"; fi
  done
  if [ ! -L "$PREFIX/state" ]; then rm -rf "$PREFIX/state"; ln -s "$PREFIX/config/state" "$PREFIX/state"; fi
  ok "配置软链已建立（devices.toml / connections.json / state）"
else
  printf "    ${D}[dry-run] 生成 config/ 与三条软链${X}\n"
fi

# ── 5. 终端页命令（用 CLI 自带的 web repair 生成，保证与 doctor 一致） ─────
step "5/8 安装终端页命令按钮"
CMDFILE="$WORKDIR/.pi/commands.json"
if [ "$DRY" = 0 ]; then
  mkdir -p "$WORKDIR/.pi" "$PREFIX/config"
  # 交给 netdev web repair 生成：它会写出正确格式（3 条聚合命令 + 设备按钮），
  # 并处理旧按钮清理与备份，保证装完 doctor 直接全绿，不再需要用户手动 repair。
  if [ -x "$PREFIX/netdev" ]; then
    if NETDEV_ROOT="$PREFIX" "$PREFIX/netdev" web repair >/dev/null 2>&1; then
      ok "已生成终端页命令（netdev web repair → $CMDFILE）"
    else
      warn "netdev web repair 未成功（可稍后手动执行）"
    fi
  else
    # 兜底：netdev 入口不可用时，退回旧模板（仅保证有东西可点，格式未必对齐 doctor）
    [ -f "$CMDFILE" ] && cp -p "$CMDFILE" "$PREFIX/backups/commands.json.bak-$(date +%Y%m%d_%H%M%S)" 2>/dev/null
    [ -L "$CMDFILE" ] && rm -f "$CMDFILE"
    LC_ALL=C sed "s|__HOME__|$HOME|g" "$HERE/config-template/pi-commands.json" > "$PREFIX/config/pi-commands.json"
    rm -f "$CMDFILE"; ln -s "$PREFIX/config/pi-commands.json" "$CMDFILE"
    ok "已写入 $CMDFILE（软链 → config/pi-commands.json；旧文件已备份）"
  fi
  # 给 AI 的约定：只在没有时创建
  if [ ! -e "$WORKDIR/AGENTS.md" ] && [ -f "$HERE/config-template/AGENTS.workspace.md" ]; then
    sed -e "s|__HOME__|$HOME|g" -e "s|__PREFIX__|$PREFIX|g" "$HERE/config-template/AGENTS.workspace.md" > "$PREFIX/config/AGENTS.workspace.md"
    ln -s "$PREFIX/config/AGENTS.workspace.md" "$WORKDIR/AGENTS.md"
    ok "已放置 $WORKDIR/AGENTS.md（软链 → config/AGENTS.workspace.md）"
  fi
else
  printf "    ${D}[dry-run] 用 netdev web repair 生成 %s（命令按钮）${X}\n" "$CMDFILE"
fi

# ── 6. 说明书 + 工具台 ─────────────────────────────────────────────────────
step "6/8 安装说明书与双击工具台"
if [ "$DRY" = 0 ]; then
  cp -f "$HERE/README-安装说明.txt" "$PREFIX/README-从这里开始.txt" 2>/dev/null || true
  mkdir -p "$PREFIX/bin"; cp -f "$HERE/tools-menu.command" "$PREFIX/bin/设备工具台.command" 2>/dev/null || true
  chmod +x "$PREFIX/bin/设备工具台.command" 2>/dev/null || true
  chmod +x "$PREFIX/netdev" "$PREFIX/netdev-mcp" 2>/dev/null || true
  ln -sf "$PREFIX/netdev" "$PREFIX/bin/netdev" 2>/dev/null || { mkdir -p "$PREFIX/bin"; ln -sf "$PREFIX/netdev" "$PREFIX/bin/netdev"; }
  mkdir -p "$HOME/.zsh/completions" "$HOME/.bash_completion.d" "$PREFIX/config"
  # 2026-10-03：补全脚本原来从 payload/completions/ 取，而那个目录压根不存在
  # （rsync 排除了 config/，源码里也没有 tools/completions/），
  # 加上 [ -f ... ] 静默跳过 —— 结果是"Tab 补全从来没装上过"，也没人报错。
  # 现在统一从 config-template/ 取（由 build_bundle.sh 从 config/*.example 生成），
  # 并把 __PREFIX__ 替换成本机安装目录。
  for cf in _netdev netdev.bash; do
    if [ -f "$HERE/config-template/$cf" ]; then
      sed -e "s|__PREFIX__|$PREFIX|g" -e "s|__HOME__|$HOME|g" \
        "$HERE/config-template/$cf" > "$PREFIX/config/$cf"
    else
      warn "缺 config-template/$cf，Tab 补全装不上（不影响其他功能）"
    fi
  done
  [ -f "$PREFIX/config/_netdev" ] && { rm -f "$HOME/.zsh/completions/_netdev"; ln -s "$PREFIX/config/_netdev" "$HOME/.zsh/completions/_netdev"; }
  [ -f "$PREFIX/config/netdev.bash" ] && { rm -f "$HOME/.bash_completion.d/netdev"; ln -s "$PREFIX/config/netdev.bash" "$HOME/.bash_completion.d/netdev"; }
  ok "已装：README、工具台（bin/设备工具台.command）、命令行 netdev、Tab 补全"
else
  printf "    ${D}[dry-run] 拷 README / 工具台 / 建 $PREFIX/bin/netdev 软链 / 补全${X}\n"
fi

# ── 7. 开机自启与定时任务（可选） ───────────────────────────────────────────
step "7/8 开机自启与定时任务"
if [ "$NO_LAUNCHD" = 1 ]; then warn "按参数跳过（--no-launchd）"
else
  if [ "$DRY" = 0 ]; then
    # 2026-10-03：原来这里 sed 两个 .tpl 文件（launchd/*.plist.tpl），但那两个模板
    # 在仓库里并不存在 —— 复制语句没有 2>/dev/null 也没有存在性判断，
    # sed 读不到文件会直接报错中断安装。改成调产品自己的命令：
    #   netdev ui install          → 写并加载 com.netdev.ui.plist（程序化生成，真源唯一）
    #   netdev logs install-agent  → 写并加载 com.netdev.logrotate.plist
    # 这样"plist 长什么样"只有一处定义（netdev_cli.py / lib 里），不再有第二份模板。
    if [ -x "$PREFIX/netdev" ] && [ -x "$PREFIX/.venv/bin/python" ]; then
      "$PREFIX/netdev" ui install 2>&1 | sed 's/^/    /' | tail -4
      "$PREFIX/netdev" logs install-agent 2>&1 | sed 's/^/    /' | tail -3
    else
      warn "netdev 或 venv 还没就绪，跳过开机自启（装完可手动跑：netdev ui install）"
    fi
  else printf "    ${D}[dry-run] 调 netdev ui install / netdev logs install-agent 装开机自启${X}\n"; fi
fi

# ── 8. 收尾：自检 + 后续步骤 ────────────────────────────────────────────────
step "8/8 自检与后续步骤"
if [ "$DRY" = 0 ] && [ -x "$PREFIX/netdev" ]; then
  "$PREFIX/netdev" doctor --rebless 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | sed 's/^/    /'
fi

if [ "$WITH_PIWEB" = 1 ]; then
  step "额外：安装网页界面 pi-web-ui（需联网，约 600MB）"
  if command -v npm >/dev/null; then
    run "npm i -g pi-web-ui" && ok "pi-web-ui 安装完成" || warn "安装失败（检查网络/镜像）"
    ok "启动：pi-web-ui --port 8899 --cwd $WORKDIR  （或 pi-web-ui server install --port 8899 --cwd $WORKDIR）"
  else warn "没有 npm，跳过"; fi
fi

printf "\n${B}═══ 装好了，接下来做什么 ═══${X}\n"
cat <<EOF
  1) 启动网页界面：双击 $PREFIX/ui/启动.command
     （若安装时未加 --no-launchd，com.netdev.ui.plist 已自动加载，登录后起）
     浏览器打开： http://127.0.0.1:8898 → 顶栏「终端」→ 左栏 8 个按钮
  2) 接设备：USB-Console 线插到设备 Console 口；打开终端页点「🔌 串口接入」
     · 若插上没反应 → 装 USB 串口驱动（FTDI / CH340 / CP210x）
  3) 先备份：点「💾 备份设备配置」→ 填客户名（这一步很重要）
  4) 出问题：双击 $PREFIX/bin/设备工具台.command → 选 1（体检）
  5) 说明书：$PREFIX/README-从这里开始.txt
  6) 卸装：bash uninstall.sh（会先移入隔离区，不直接删除）
EOF
printf "\n${D}安装日志结束。${X}\n"
