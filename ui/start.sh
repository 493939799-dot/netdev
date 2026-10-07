#!/bin/bash
# netdev-ui 启动包装器（供 LaunchAgent 与手动调用）
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DEFAULT="$(cd "$HERE/.." && pwd)"
NETDEV_ROOT="${NETDEV_ROOT:-$ROOT_DEFAULT}"
cd "$NETDEV_ROOT"
exec ./.venv/bin/python ui/server.py --port 8898 "$@"
