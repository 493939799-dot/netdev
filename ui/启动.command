#!/bin/bash
# 双击即可启动 netdev-ui。
# 2026-10-01 改：原先用 exec 前台跑，关掉这个终端窗口 = 服务被杀（"服务又起不来了"的常见来源）。
# 现在走 netdev ui（double-fork 后台守护），窗口关了服务照跑；再双击也不会重复起。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DEFAULT="$(cd "$HERE/.." && pwd)"
NETDEV_ROOT="${NETDEV_ROOT:-$ROOT_DEFAULT}"
cd "$NETDEV_ROOT" || exit 1
PY="$NETDEV_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || echo python3)"

printf '\033[1m▮ netdev-ui\033[0m  启动中 …（后台守护，关掉本窗口不受影响）\n\n'
"$PY" "$NETDEV_ROOT/netdev_cli.py" ui open
rc=$?
echo
printf '\033[2m本窗口可以关掉了。停止服务：%s/netdev ui stop\033[0m\n' "$NETDEV_ROOT"
[ $rc -eq 0 ] || { echo; printf '\033[33m起不来时先看体检：%s/netdev doctor\033[0m\n' "$NETDEV_ROOT"; }
printf '\n按回车关闭…'; read -r _ || true
exit $rc
