#!/bin/bash
# WorkBuddy agent 后端 · 自检脚本（2026-09-29 第四轮加）
#
# 用途：AI 助手切到 WorkBuddy agent 后，若怀疑"凭据过期 / 连不上 / 权限没生效"，
#       双击本文件即可把当前状态一次打全（只读，不消耗额度）。
#
# 背景：这个后端【不需要登录】——它走 CLI 内置的 custom-token 通道直连。
#       别再去找 /login，桌面版的 CLI 没有交互式 TUI，压根没有那条命令。
#       详见 04_变更历史「第四轮」与 03_当前状态与已知问题「四之三」。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DEFAULT="$(cd "$HERE/.." && pwd)"
NETDEV_ROOT="${NETDEV_ROOT:-$ROOT_DEFAULT}"
ROOT="$NETDEV_ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="/usr/bin/python3"

echo "════════ WorkBuddy agent 后端自检 ════════"
echo

echo "── 1) UI 服务（8898）──"
if curl -s --noproxy '*' -o /dev/null -w "" --max-time 3 http://127.0.0.1:8898/ 2>/dev/null; then
  curl -s --noproxy '*' --max-time 20 "http://127.0.0.1:8898/api/agents" \
    | "$PY" -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print("  解析失败:",e); raise SystemExit
for a in d.get("agents",[]):
    if a.get("id")=="wb":
        print("  名称    :",a.get("name"))
        print("  路径    :",a.get("path"))
        print("  版本    :",a.get("version"))
        print("  可用    :",a.get("available"))
        print("  凭据    :",a.get("auth_note"))
        print("  判定    :",a.get("verdict"))
        break
else: print("  未找到 wb 条目")
'
else
  echo "  ✗ 8898 没有响应 —— UI 服务没在跑"
  echo "    起法： cd $ROOT && ./.venv/bin/python ui/server.py --port 8898"
fi

echo
echo "── 2) 凭据直读（绕过 UI）──"
cd "$ROOT/ui" && "$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
try:
    import server as S
except Exception as e:
    print("  导入 server.py 失败：", e); raise SystemExit
tok, src = S._wb_token()
ok, note = S._wb_token_state()
print("  状态    :", "OK" if ok else "不可用")
print("  说明    :", note)
print("  来源    :", src or "(空)")
print("  token   :", ("<hidden, len=%d>" % len(tok)) if tok else "(空)")
print("  桌面账号:", S._wb_desktop_uid() or "(读不到 workbuddy-desktop.info)")
EOF

echo
echo "── 3) 生成物 ──"
for f in workbuddy-product.json codebuddy-mcp.json codebuddy-mcp-empty.json; do
  p="$ROOT/config/$f"
  if [ -f "$p" ]; then
    printf "  %-26s %8s B  mode=%s\n" "$f" "$(stat -f%z "$p")" "$(stat -f%Lp "$p")"
  else
    printf "  %-26s (尚未生成，开一次 AI 会话就会出现)\n" "$f"
  fi
done

echo
echo "── 4) 权限边界（本机实测口径）──"
echo "  期望：模型可见工具 = Read / Glob / Grep + netdev 的 13 个"
echo "  期望：Bash / Write / Edit / Agent / WebFetch 等【不存在】"
echo "  做法：--disallowedTools 逐项硬移除（逗号串会静默失效，务必逐个参数）"

echo
echo "════════ 完毕 ════════"
