#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  设备工具台（netdev）· 卸载
#  原则：不裸删。所有东西先移入隔离区 ~/.quarantine-netdev-<时间>/
#  用法： bash uninstall.sh [--yes] [--prefix DIR]
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
YES=0; PREFIX="$HOME/netops"
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --prefix) PREFIX="${2:-$HOME/netops}"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac; shift
done
B="\033[1m"; G="\033[32m"; Y="\033[33m"; X="\033[0m"
TS=$(date +%Y%m%d_%H%M%S); Q="$HOME/.quarantine-netdev-$TS"
printf "${B}即将卸载（先移入隔离区，不直接删除）${X}\n"
printf "  安装目录 : %s\n" "$PREFIX"
printf "  隔离区   : %s\n" "$Q"
printf "  会移除   : ~/netops/bin/netdev 软链、~/.pi/commands.json 里的 8 条命令、补全软链、日志轮转任务\n"
printf "  ${Y}注意${X}：你的配置与快照备份会一起进隔离区（可随时取回）\n"
if [ "$YES" != 1 ]; then
  printf "\n  确认卸载？输入 YES："; read -r a || a=""
  [ "$a" = "YES" ] || { echo "  已取消，什么都没做。"; exit 0; }
fi
mkdir -p "$Q"
# ① 程序目录
if [ -e "$PREFIX" ]; then mv "$PREFIX" "$Q/netops" && echo "  ✔ 程序与配置已移入隔离区"; fi
# ② 外部软链
for l in "$HOME/netops/bin/netdev" "$HOME/.zsh/completions/_netdev" "$HOME/.bash_completion.d/netdev" "$HOME/.pi/commands.json" "$HOME/AGENTS.md"; do
  if [ -L "$l" ] || [ -e "$l" ]; then
    mkdir -p "$Q/external$(dirname "${l#$HOME}")"
    mv "$l" "$Q/external${l#$HOME}" 2>/dev/null && echo "  ✔ 移走 $l"
  fi
done
# ③ 定时任务
launchctl bootout "gui/$(id -u)/com.netdev.logrotate" 2>/dev/null && echo "  ✔ 已卸载日志轮转任务"
if [ -f "$HOME/Library/LaunchAgents/com.netdev.logrotate.plist" ]; then
  mv "$HOME/Library/LaunchAgents/com.netdev.logrotate.plist" "$Q/" && echo "  ✔ 移走 plist"
fi
# ④ 清单
{
  echo -e "原路径\t说明"
  echo -e "$PREFIX\t程序与配置（含 backups 快照）"
  echo -e "~/netops/bin/netdev\t命令行软链"
  echo -e "~/.pi/commands.json\t终端页 8 条命令（原文件已在隔离区）"
  echo -e "~/Library/LaunchAgents/com.netdev.logrotate.plist\t日志轮转任务"
} > "$Q/MANIFEST.tsv"
printf "\n${G}卸载完成（可逆）${X}\n"
printf "  隔离区：%s\n" "$Q"
printf "  想取回：把 $Q/netops 移回 %s 即可；清单见 %s/MANIFEST.tsv\n" "$PREFIX" "$Q"
printf "  确认无用后再手工删除隔离区（建议至少观察一天）。\n"
