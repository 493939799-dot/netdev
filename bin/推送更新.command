#!/bin/zsh
# ==========================================================================
#  一键把本机改动推送到 GitHub
#
#  为什么要有这个脚本：
#    1. 本机没有配 SSH 密钥，也没有缓存凭据，直接 `git push` 会要求输入用户名密码；
#    2. 把 Token **粘在聊天窗口里**会让它进入对话记录 —— 已经出过这个事故，
#       所以这里用隐藏输入（屏幕不显示、不进 shell 历史、不进命令行参数）；
#    3. 推送成功后 macOS 钥匙串会自动记住它，**以后直接 git push 就行**。
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
    read -r _
    exit 0
  fi
else
  echo "$PENDING"
fi
echo

echo "── 泄密自检（只查将要进仓的内容）──"
# 说明：这里的模式都是本项目踩过的真实泄密点：
#   USB 转串口适配器序列号 / 用户名密码 / GitHub Token / 私钥 / 本机绝对路径
LEAK=0
for pat in 'AABBCCDD' 'usbserial' 'github_pat_' 'ghp_' 'BEGIN [A-Z ]*PRIVATE KEY' '/Users/mac/Desktop'; do
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
  read -r _
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
  read -r _
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
# 本机配的是 osxkeychain，推送成功后它能记住这枚 Token，
# 以后直接 `git push` 就不用再输了。
echo "正在推送……"
echo
if git push origin main; then
  echo
  echo "=============================================="
  echo "  ✔ 推送成功"
  echo "=============================================="
  echo
  echo "Token 已交给 macOS 钥匙串保管，下次不用再输。"
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
read -r _
exit $RC
