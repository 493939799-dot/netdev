#!/bin/zsh
# ==========================================================================
#  一键把本机改动推送到 GitHub
#
#  为什么要有这个脚本：
#    1. 本机没有配 SSH 密钥，也没有缓存凭据，直接 `git push` 会要求输入用户名密码；
#    2. 把 Token **粘在聊天窗口里**会让它进入对话记录 —— 已经出过这个事故，
#       所以这里用隐藏输入（屏幕不显示、不进 shell 历史、不进命令行参数）；
#    3. 推送成功后由本脚本**显式**把 Token 写进钥匙串。
#       不能指望 git 自动记：凭据来自 GIT_ASKPASS 时 git **不会**回写 credential
#       helper（2026-10-03 实测：推送成功后钥匙串里依然没有条目，结果每次都要重新粘）。
#       存进去之后，以后直接 git push 就行，别的工具也能复用同一枚凭据。
#    4. 所有"按回车继续/退出"一律用 read -rs（关掉回显）。
#       已出过事故（2026-10-03）：在"等回车"提示下误粘贴了 Token，终端是回显模式，
#       整个 Token 明文打在屏幕上、又被截图发出去 → 凭据当场泄露、只能作废重发。
#       换成 -s 之后，同样的误操作不会再显示任何字符。**不要把它改回 read -r。**
#
#  用法：双击本文件（或在终端里执行 bin/推送更新.command）
# ==========================================================================
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "找不到仓库目录"; exit 1; }

echo "=============================================="
echo "  推送 netdev 改动到 GitHub"
echo "  仓库：$REPO"
echo "=============================================="
echo

REMOTE_URL="$(git remote get-url origin 2>/dev/null)"
if [ -z "$REMOTE_URL" ]; then
  echo "✘ 这个仓库没有配置 origin 远程。先执行："
  echo "   git remote add origin https://github.com/493939799-dot/netdev.git"
  exit 1
fi
echo "远程：$REMOTE_URL"
echo

echo "── 将要推送的提交 ──"
PENDING="$(git log --oneline origin/main..HEAD 2>/dev/null)"
if [ -z "$PENDING" ]; then
  echo "  （没有新提交，本地已是最新）"
  if [ "${1:-}" != "-f" ]; then
    echo
    echo "按回车键退出。"
    read -rs _
    exit 0
  fi
else
  echo "$PENDING"
fi
echo

echo "── 泄密自检（只查将要进仓的内容）──"
# 说明：下面的模式都是本项目踩过的真实泄密点：
#   USB 转串口适配器序列号 / 串口设备路径 / GitHub Token / 私钥 / 本机绝对路径
#
# ★★ 这些模式必须**分片拼装**，绝不能写成完整字面量 ★★
#   为什么：本脚本自己就是要被推送的文件之一，于是它会出现在自己扫描的 diff 里。
#   规则一旦写成完整字面量，自检就会**命中自己** → 永久假阳性 → 这个脚本
#   从被提交的那一刻起就再也推不动了（只会一直让你加 -f，而 -f 会连真检查一起绕过）。
#   2026-10-03 实测：5 条规则全部命中，脚本被自己的检查拦死。
#   拼装后源码里不存在连续的目标串，扫描不到自己；但运行时拼出的模式仍然完整有效。
LEAK=0
PAT_SN="FTAAM""5SL"                     # USB 转串口适配器序列号
PAT_USB="usb""serial"                   # 串口设备路径
PAT_FGP="github""_pat_"                 # GitHub 细粒度 Token
PAT_CLS="gh""p_"                        # GitHub 经典 Token
PAT_KEY="BEGIN [A-Z ]*PRIV""ATE KEY"    # 私钥头
PAT_DESK="/Users/mac/Desk""top"         # 本机桌面绝对路径
for pat in "$PAT_SN" "$PAT_USB" "$PAT_FGP" "$PAT_CLS" "$PAT_KEY" "$PAT_DESK"; do
  n=$(git diff origin/main..HEAD 2>/dev/null | grep -cE "$pat")
  if [ "$n" != "0" ]; then
    echo "  ⚠ 命中「$pat」$n 处 —— 请先人工确认再推"
    LEAK=1
  fi
done
if [ "$LEAK" = "0" ]; then
  echo "  ✔ 没有命中已知泄密模式"
else
  echo
  echo "检测到可疑内容。要继续推送请重新运行："
  echo "  bin/推送更新.command -f"
  echo
  echo "按回车键退出（未推送）。"
  echo "⚠ 这一步只是在等你按回车，**不接收 Token** —— 请不要在这里粘贴任何"
  echo "   Token / 密码 / 密钥。真正的输入提示出现在稍后的步骤里。"
  read -rs _
  exit 1
fi
echo

echo "──────────────────────────────────────────────"
echo "需要一枚 GitHub Token（Fine-grained PAT），权限至少："
echo "    Contents: Read and write"
echo "    Workflows: Read and write     ← 推 .github/workflows 必需"
echo
echo "还没有？到 https://github.com/settings/personal-access-tokens 新建，"
echo "Repository access 选 netdev 这一个仓库即可。"
echo "──────────────────────────────────────────────"
echo
printf "把 Token 粘贴到这里，然后回车（输入过程屏幕不显示）："
read -rs TOK
echo
echo

if [ -z "$TOK" ]; then
  echo "✘ 没有输入内容，已取消。"
  echo
  echo "按回车键退出。"
  read -rs _
  exit 1
fi

# 用 GIT_ASKPASS 把 Token 交给 git：
#   · 不进命令行参数（ps 看不到）
#   · 不写进任何文件（只放在本进程的环境变量里）
#   · 不落进 shell 历史
ASKPASS_DIR="$(mktemp -d)"
ASKPASS="$ASKPASS_DIR/askpass.sh"
printf '#!/bin/sh\nprintf "%%s" "$NETDEV_PUSH_TOKEN"\n' > "$ASKPASS"
chmod 700 "$ASKPASS"
export NETDEV_PUSH_TOKEN="$TOK"
export GIT_ASKPASS="$ASKPASS"

# 注意：这里**故意**不写 `-c credential.helper=`。
# 本机配的是 osxkeychain，目的是让 git 能复用**已经存在**的凭据；
# 但"推送成功后 git 会自动记住这枚 Token"是不成立的（2026-10-03 实测），
# 所以下面推送成功后会由本脚本**显式**写一次钥匙串。
echo "正在推送……"
echo
if git push origin main; then
  echo
  echo "=============================================="
  echo "  ✔ 推送成功"
  echo "=============================================="
  echo

  # ── 显式把 Token 写进钥匙串 ──────────────────────────────────────────
  # 为什么必须显式做：实测（2026-10-03）git 用 GIT_ASKPASS 拿到凭据后
  # **不会**回写 credential.helper，于是"推送成功但钥匙串仍为空"，
  # 下次推送又要重新粘 Token —— 这也正是"AI 想代你推送却推不动"的原因。
  # 用户名从 remote URL 里现取（https://github.com/<user>/<repo>.git），
  # 不写死，换账号/换机器都不用改脚本。
  GH_USER="$(git remote get-url origin 2>/dev/null \
             | sed -E 's#^https://[^/]+/([^/]+)/.*#\1#')"
  if [ -n "$GH_USER" ]; then
    printf 'protocol=https\nhost=github.com\nusername=%s\npassword=%s\n\n' \
      "$GH_USER" "$NETDEV_PUSH_TOKEN" | git credential-osxkeychain store 2>/dev/null
    if security find-internet-password -s github.com -a "$GH_USER" >/dev/null 2>&1; then
      echo "✔ Token 已写入 macOS 钥匙串 —— 以后 git push 不用再输，"
      echo "  别的工具（包括替你干活的 AI）也能直接用。"
    else
      echo "⚠ Token 没能写进钥匙串（下次推送需重新粘贴，不影响本次推送结果）。"
    fi
  else
    echo "⚠ 没能从 origin 解析出用户名，跳过写钥匙串。"
  fi
  echo "想让它忘掉：钥匙串访问.app 里搜 github.com，删掉对应条目即可。"
  echo
  echo "★ 如果这枚 Token 曾经出现在聊天/截图/任何公开地方，"
  echo "  请立刻到 https://github.com/settings/personal-access-tokens 撤销重发。"
  RC=0
else
  echo
  echo "✘ 推送失败。对照下面几种常见情况："
  echo "  · 要你输用户名密码 → Token 无效或已过期，重新生成一枚"
  echo "  · 403 / workflow scope → Token 少了 Workflows: Read and write 权限"
  echo "  · Could not resolve host → 网络问题，稍后再试"
  RC=1
fi

# 清场：把 Token 从环境与磁盘上撤掉
unset NETDEV_PUSH_TOKEN GIT_ASKPASS
rm -rf "$ASKPASS_DIR"

echo
echo "按回车键关闭窗口。"
read -rs _
exit $RC
