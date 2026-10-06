#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# netdev 一键体检（双击运行 / 终端运行皆可）
# 用法：
#   双击本文件                          → 快速体检（约 10 秒）
#   终端：bash bin/一键体检.command --tests
#                                        → 快速体检 + 全量回归测试
# 覆盖：系统资源 / 依赖 / 服务 / 项目完整性 / 安全 / 设备 / （可选）测试
# 报告自动落盘 logs/体检-<时间>.txt
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p logs
REPORT="logs/体检-$(date +%Y%m%d-%H%M%S).txt"
exec > >(tee "$REPORT") 2>&1

PASS=0; WARN=0; FAIL=0
ok(){   PASS=$((PASS+1)); printf '  \033[32m✔\033[0m %s\n' "$1"; }
warn(){ WARN=$((WARN+1)); printf '  \033[33m⚠ %s\033[0m\n' "$1"; }
bad(){  FAIL=$((FAIL+1)); printf '  \033[31m✘ %s\033[0m\n' "$1"; }
info(){ printf '  \033[2m· %s\033[0m\n' "$1"; }
sec(){  printf '\n\033[1;36m── %s ──\033[0m\n' "$1"; }

printf '\033[1;35m▮ netdev 一键体检\033[0m  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
info "项目根：$ROOT"
WITH_TESTS=0; [ "${1:-}" = "--tests" ] && WITH_TESTS=1

# ── A. 系统与资源 ──────────────────────────────────────────────
sec "A. 系统与资源"
info "系统 $(sw_vers -productVersion 2>/dev/null) · $(uname -m) · 已开机 $(uptime | sed -E 's/.*up +([^,]+),.*/\1/')"
DISK_AVAIL=$(df -g / | awk 'NR==2{print $4}')
if   [ "${DISK_AVAIL:-0}" -ge 20 ]; then ok "磁盘剩余 ${DISK_AVAIL} GB"
elif [ "${DISK_AVAIL:-0}" -ge 10 ]; then warn "磁盘剩余 ${DISK_AVAIL} GB（偏少，建议清理到 20GB 以上）"
else bad "磁盘仅剩 ${DISK_AVAIL} GB —— 随时可能影响日志/备份写入"
fi
LOAD=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
CORES=$(sysctl -n hw.ncpu 2>/dev/null)
if awk "BEGIN{exit !($LOAD < $CORES*2)}" 2>/dev/null; then ok "负载 ${LOAD} / ${CORES} 核（正常）"
else warn "负载 ${LOAD} 已超 ${CORES} 核的 2 倍——系统很忙，体验会卡"
fi

# ── B. 依赖与环境 ──────────────────────────────────────────────
sec "B. 依赖与环境"
PY="$ROOT/.venv/bin/python"
if [ -x "$PY" ]; then
  ok "项目虚拟环境 .venv：$($PY -V 2>&1)"
  "$PY" -c "import paramiko, serial, netmiko" 2>/dev/null \
    && ok "核心库 paramiko / pyserial / netmiko 可导入" \
    || bad "核心库导入失败——运行环境损坏，重跑 install.sh"
else
  warn "无 .venv（若装在其他前缀，用系统 python3 兜底）"
  PY="$(command -v python3)"
fi
TMUX_BIN="$(command -v tmux)"
if [ -n "$TMUX_BIN" ]; then ok "tmux：$TMUX_BIN"; else bad "缺 tmux —— 人机同屏不可用（brew install tmux）"; fi
CU_COUNT=$(ls /dev/cu.* 2>/dev/null | grep -cv Bluetooth || true)
info "串口设备 /dev/cu.*：${CU_COUNT} 个（Bluetooth 已排除）"
[ "$CU_COUNT" -gt 0 ] && info " $(ls /dev/cu.* 2>/dev/null | grep -v Bluetooth | tr '\n' ' ')"

# ── C. 服务与健康 ──────────────────────────────────────────────
sec "C. 网页服务（8898）"
if [ -x "$ROOT/bin/netdev" ]; then ND="$ROOT/bin/netdev"; else ND="$ROOT/netdev"; fi
STATUS_OUT=$("$ND" ui status 2>&1)
if echo "$STATUS_OUT" | grep -q "服务应答.*HTTP 200"; then
  ok "服务应答正常（$(echo "$STATUS_OUT" | grep -o '"version": "[^"]*"')）"
else
  bad "服务未应答——执行 $ND ui 即可拉起"
fi
echo "$STATUS_OUT" | grep "端口" | grep -q "8898" && echo "$STATUS_OUT" | grep "端口" | grep -q "已监听" && ok "端口 8898 已监听" || warn "端口 8898 未监听"
AUTO_TXT=$(echo "$STATUS_OUT" | grep "开机自启")
if echo "$AUTO_TXT" | grep -q "已装未加载"; then
  warn "开机自启已装但未加载——重启电脑服务不会自动起，执行 $ND ui install 修复"
elif echo "$AUTO_TXT" | grep -q "未装"; then
  warn "开机自启未装——重启电脑后需手动起服务（netdev ui install）"
else
  ok "开机自启已配置且已加载"
fi
HEALTH=$(curl -s --max-time 3 http://127.0.0.1:8898/api/health 2>/dev/null)
V_INSTALLED=$(cat "$ROOT/dist/installer/VERSION" 2>/dev/null)
V_HEALTH=$([ -n "$HEALTH" ] && echo "$HEALTH" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('version',''))" 2>/dev/null)
if [ -n "$V_HEALTH" ] && [ "$V_HEALTH" = "$V_INSTALLED" ]; then
  ok "版本一致：${V_HEALTH}（服务 = 安装包标定）"
elif [ -n "$V_HEALTH" ]; then
  warn "版本不一致：服务跑 ${V_HEALTH}，安装标定 $V_INSTALLED —— 改过代码后需 netdev ui restart"
else
  info "服务未起，跳过版本比对"
fi

# ── D. 项目完整性 ──────────────────────────────────────────────
sec "D. 项目完整性"
for f in netdev_cli.py netdev_mcp.py ui/server.py ui/static/index.html \
         config/devices.toml lib/approval.py dist/installer/VERSION; do
  [ -f "$ROOT/$f" ] && ok "关键文件 $f" || bad "缺关键文件 $f"
done
SNAP_N=$(ls "$ROOT/backups/snapshots" 2>/dev/null | wc -l | tr -d ' ')
if   [ "$SNAP_N" -gt 0 ]; then ok "配置快照 ${SNAP_N} 份（出事可回滚）"
else warn "快照 0 份——建议接上设备做一次「立即备份」"
fi
LOG_MB=$(du -sm "$ROOT/logs" 2>/dev/null | awk '{print $1}')
[ "${LOG_MB:-0}" -lt 500 ] && ok "logs/ 占 ${LOG_MB:-0} MB（正常）" \
                           || warn "logs/ 已占 ${LOG_MB} MB，可清理旧日志"
if [ -d "$ROOT/.git" ]; then
  DIRTY=$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  info "git 未提交改动：${DIRTY} 个文件"
fi

# ── E. 安全 ────────────────────────────────────────────────────
sec "E. 安全"
if [ -f "$ROOT/config/direct.json" ]; then
  PERM=$(stat -f '%Lp' "$ROOT/config/direct.json" 2>/dev/null)
  [ "$PERM" = "600" ] && ok "direct.json 权限 600（仅本人可读）" \
                      || bad "direct.json 权限是 ${PERM}（应为 600）——执行 chmod 600 config/direct.json"
  git -C "$ROOT" check-ignore -q config/direct.json \
    && ok "direct.json 已被 gitignore 排除（不会误传 GitHub）" \
    || bad "direct.json 未被 gitignore 排除——有泄露风险！"
else
  info "未配置直连 API Key（AI 助手不可用，其余功能不受影响）"
fi
if grep -qE '^\s*password\s*=' "$ROOT/config/devices.toml" 2>/dev/null; then
  bad "devices.toml 里疑似写了明文密码——应改用 password_env / password_keychain"
else
  ok "devices.toml 无明文密码（走环境变量/钥匙串）"
fi
"$PY" - <<'EOF' 2>/dev/null && ok "审批模块基线校验通过" || warn "审批基线校验跳过或异常（详见 netdev doctor）"
import sys; sys.path.insert(0,'.')
from lib import approval
EOF

# ── F. 设备 ────────────────────────────────────────────────────
sec "F. 设备"
DEVS=$("$PY" - <<'EOF' 2>/dev/null
import tomllib
d = tomllib.load(open('config/devices.toml','rb'))
devs = d.get('device') or d.get('devices') or []
out = []
items = devs.items() if isinstance(devs, dict) else enumerate(devs)
for k, v in items:
    if isinstance(v, dict):
        proto = v.get('protocol','?')
        # 串口不回显端口路径（避免把序列号写进体检报告）
        loc = v.get('host') or ('串口' if proto == 'serial' else '?')
        out.append(f"{v.get('name','?')}({proto}@{loc})")
print(' '.join(out))
EOF
)
if [ -n "$DEVS" ]; then ok "设备清单可解析：$DEVS"; else warn "devices.toml 解析失败或为空"; fi
if nc -z -G 2 127.0.0.1 20022 2>/dev/null; then
  ok "本机模拟器 mock-hw 在跑（127.0.0.1:20022）"
else
  info "模拟器未启动（要用时：./netdev mock start）"
fi

# ── G. 回归测试（可选：--tests） ───────────────────────────────
sec "G. 回归测试"
if [ "$WITH_TESTS" = "1" ]; then
  TFAIL=0; TN=0
  for t in "$ROOT"/tests/test_*.py; do
    TN=$((TN+1))
    if "$PY" "$t" >/dev/null 2>&1; then ok "通过 $(basename "$t")"
    else bad "失败 $(basename "$t")（详情：$PY ${t}）"; TFAIL=$((TFAIL+1)); fi
  done
  info "共 $TN 个测试文件"
else
  info "本次未跑测试（全量约 1-2 分钟）。要跑：bash bin/一键体检.command --tests"
fi

# ── 总结 ───────────────────────────────────────────────────────
printf '\n\033[1;35m══ 体检总结 ══\033[0m\n'
printf '  通过 \033[32m%d\033[0m · 警告 \033[33m%d\033[0m · 失败 \033[31m%d\033[0m\n' "$PASS" "$WARN" "$FAIL"
if   [ "$FAIL"  -gt 0 ]; then printf '  结论：\033[31m存在需要处理的问题（见上方 ✘ 项）\033[0m\n'
elif [ "$WARN"  -gt 0 ]; then printf '  结论：\033[33m整体健康，有 %d 处可留意（见 ⚠ 项）\033[0m\n' "$WARN"
else                         printf '  结论：\033[32m全部正常 ✓\033[0m\n'
fi
echo "  报告已存：$ROOT/$REPORT"
printf '\n\033[2m按回车关闭…\033[0m'; [ -t 0 ] && read -r _
exit 0
