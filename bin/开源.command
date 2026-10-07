#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  开源助手（双击即用 · 面向不懂技术的使用者）
#
#  这个脚本会替你把"本地项目"整理成"可以放到 GitHub 上"的状态，
#  然后一步一步告诉你接下来该做什么、每一步要抄哪条命令。
#
#  它不会做的事（重要）：
#   · 不会替你创建 GitHub 账号
#   · 不会把你的代码上传到任何地方（最后那一步必须你自己按回车确认）
#   · 遇到需要你决定的地方会停下来问你，绝不擅自替你决定
#
#  想撤销？什么都不做即可（脚本不改你的源文件，只改 git 的暂存区和 README 里的徽章）。
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || { echo "进不去项目目录，脚本停止"; exit 1; }
export PATH="$HOME/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# --dry：只体检、只报告，什么都不改（拿不准时先跑这个）
DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; X="\033[0m"
# 注意用 %b 而不是 %s：%s 会把颜色转义当普通字符原样打出来（实测踩过）
say()  { printf "%b\n" "$*"; }
ok()   { printf "  ${G}✔${X} %s\n" "$*"; }
warn() { printf "  ${Y}!${X} %s\n" "$*"; }
bad()  { printf "  ${R}✘${X} %s\n" "$*"; }
head2(){ printf "\n${B}══ %s ══${X}\n" "$*"; }
# 提问：$1=问题  $2=怎么填的提示。问句只打一次（原来用 $* 会把两段都打一遍）
ask()  { printf "\n  ${B}%s${X}\n" "$1"; printf "  ${D}%s${X}\n" "$2"; printf "  → "; read -r REPLY; REPLY="${REPLY// /}"; REPLY="${REPLY%/}"; }

# ─────────────────────────────────────────────────────────────
head2 "第 0 步：先检查一下环境"
# ─────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  bad "你的电脑上没有 git，开源需要它。"
  say ""
  say "  ${B}装它（只需一次）：${X}"
  say "    1) 打开这个网址： https://developer.apple.com/xcode/resources/"
  say "    2) 点「Download」下载 Xcode Command Line Tools（约 150MB）"
  say "    3) 双击下载的 .pkg，一路点「继续」"
  say "    4) 装完关掉本窗口，重新双击这个脚本"
  say ""
  read -r _
  exit 1
fi
ok "git 已就绪（$(git --version | awk '{print $3}')）"

if [ ! -d "$ROOT/.git" ]; then
  warn "这个项目还不是 git 仓库，正在初始化…"
  git init -q
  # 显式固定分支名：不同 git 版本默认给的是 master 或 main，
  # 而 GitHub 教程、`git push origin main` 之类都假设叫 main。
  # 不固定的话，将来照着文档敲 `git push origin main` 会报
  # "src refspec main does not match any"，新手会以为代码丢了。
  git branch -M main 2>/dev/null || true
  ok "已初始化，主分支固定叫 main（这一步只是准备，不会上传任何东西）"
fi

# 统计将要进仓的文件
CNT=$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ')
SIZE=$(git ls-files --others --exclude-standard -z 2>/dev/null | xargs -0 du -ck 2>/dev/null | tail -1 | awk '{print $1}')
ok "将要进入版本库的文件：${CNT} 个，约 ${SIZE:-?} KB"

# 泄密自检（这几项必须在列表之外才安全）
# 两道防线都要看：
#   untracked 且未被 .gitignore 排除  → 正常情况下会被 gitignore 挡掉
#   **已经进了暂存区的**              → 有人用 `git add -f` 强行加进来，或 .gitignore 被改坏
# 只看前者会漏掉"强行加入"的情况（实测：git add -f 之后它就不再出现在 others 列表里）。
# 目录要看「除 .gitkeep 外还有没有东西」——
# .gitkeep 是空文件，作用是让空目录能进仓库（logs/ 就是这样），它**应该**被提交。
# 早先按目录名一刀切判断，把 logs/ 误判成危险，向导在这个仓库上直接拒绝执行（实测）。
_list_would_commit() {
  # ★ `-c core.quotePath=false` 不能省：git 输出**中文文件名**时默认加引号并转义成
  #   "logs/\344\270\216\346\227\266..."，于是 grep "^logs/" 完全匹配不上 ——
  #   而本项目到处是中文文件名（README-从这里开始.txt、bin/设备工具台.command…），
  #   等于整个泄密检查是半瞎的（实测踩到：强制加入的中文名文件没被拦住）。
  { git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null
    git -c core.quotePath=false diff --cached --name-only 2>/dev/null; } | sort -u
}
LEAK=""
for f in config/devices.toml config/connections.json config/direct.json \
         config/AGENTS.workspace.md; do
  if _list_would_commit | grep -qx "$f"; then LEAK="$LEAK $f"; fi
done
for d in .venv logs backups live state; do
  EXTRA=$(_list_would_commit | grep "^$d/" | grep -v '/\.gitkeep$')
  if [ -n "$EXTRA" ]; then
    LEAK="$LEAK $d/($(printf '%s\n' "$EXTRA" | wc -l | tr -d ' ') 个文件)"
  fi
done
if [ -n "$LEAK" ]; then
  bad "这些文件本不该进版本库，但它们在里面：$LEAK"
  say "  ${D}请先修好再继续，否则你的设备信息和密钥可能被公开。${X}"
  read -r _; exit 1
fi
ok "泄密自检通过（真机配置、密码文件、日志内容都不在提交列表里）"

# 抓编辑/替换工具留下的临时文件（sed -i、编辑器备份等）。
# 这类文件没被 .gitignore 挡住，会跟着一起提交 —— 实测踩到过。
TMPJUNK=$(git ls-files --others --exclude-standard 2>/dev/null \
          | grep -E '\.(bak|new|tmp|orig|rej|swp)$|^\.!|m[A-Za-z0-9]{6}$' | head -5)
if [ -n "$TMPJUNK" ]; then
  warn "发现几个像是临时文件的家伙（建议先删掉再提交）："
  echo "$TMPJUNK" | sed 's|^|      |'
  say "  ${D}它们多半是编辑文件时留下的残留，删掉不影响你的代码。${X}"
else
  ok "没有临时文件残留"
fi

# ─────────────────────────────────────────────────────────────
head2 "第 1 步：问你几个问题（不知道就照提示填）"
# ─────────────────────────────────────────────────────────────

ask "① 你的 GitHub 用户名是什么？（不是邮箱，是 github.com/后面那一段）" \
    "在浏览器打开 https://github.com ，登录后看右上角头像左边的名字。填在这里："
OWNER="${REPLY:-}"
if [ -z "$OWNER" ]; then bad "没填用户名，无法继续。"; read -r _; exit 1; fi
ok "GitHub 用户名：$OWNER"

ask "② 仓库叫什么名字？（别人将来会搜这个名字，建议用英文）" \
    "直接回车 = 用默认名 netdev"
REPO="${REPLY:-netdev}"
ok "仓库名：$OWNER/$REPO"

ask "③ 代码里显示的名字（别人在提交记录里看到的）" \
    "直接回车 = 用你的 GitHub 用户名 $OWNER"
GITNAME="${REPLY:-$OWNER}"
ok "显示名字：$GITNAME"

say ""
say "  ${D}关于邮箱，两种都可以：${X}"
say "    · 用你注册 GitHub 的那个邮箱  —— 最省事"
say "    · 想保护隐私：GitHub 网页 → 右上头像 → Settings → Privacy →"
say "      打开 \"Keep my email address private\"，然后填"
say "      你的用户名+你的用户ID@users.noreply.github.com"
say "      （用户ID在 Settings → Profile 最下面 \"Change your username\" 那行，"
say "        括号里就是，例如 (用户名 12345) → 填 用户名+12345@users.noreply.github.com）"
ask "④ 代码里用哪个邮箱？" \
    "直接回车 = 跳过（那 git 提交这步会停下来，需要你自己设）"
GITEMAIL="${REPLY:-}"
if [ -z "$GITEMAIL" ]; then
  warn "没填邮箱，脚本无法替你提交。稍后会给你一条命令，照抄即可。"
  CAN_COMMIT=0
else
  ok "邮箱：$GITEMAIL"
  CAN_COMMIT=1
fi

# ─────────────────────────────────────────────────────────────
head2 "第 2 步：把 README 里的占位符换成你的仓库地址"
# ─────────────────────────────────────────────────────────────
if [ "$DRY" = "1" ]; then
  if grep -q "OWNER/REPO" README.md 2>/dev/null; then
    warn "演练模式：README 里还有 OWNER/REPO 占位符，正式跑时会自动换成 $OWNER/$REPO"
  else
    ok "README 占位符已经是你的地址了"
  fi
elif grep -q "OWNER/REPO" README.md 2>/dev/null; then
  # 不用 `sed -i` —— 它在 macOS 上会留下 .bak 或 .!pid!name 之类的临时文件，
  # 那些文件没被 .gitignore 挡住，会跟着一起提交上去（实测踩到）。
  # 改成"写临时文件再原子替换"，不留任何残留。
  if sed "s#OWNER/REPO#${OWNER}/${REPO}#g" README.md > README.md.new \
     && mv README.md.new README.md; then
    ok "README 顶部的徽章已指向你的仓库"
  else
    rm -f README.md.new
    bad "改 README 失败了，脚本停下（不影响你的源文件）"
    read -r _; exit 1
  fi
  if grep -q "OWNER/REPO" README.md 2>/dev/null; then
    warn "README 里还有 OWNER/REPO 没换掉（可能出现了变体写法），不影响上传"
  fi
else
  ok "README 里的占位符已经填过了（或不存在），跳过"
fi

# ─────────────────────────────────────────────────────────────
head2 "第 3 步：暂存文件（还没提交，随时可以反悔）"
# ─────────────────────────────────────────────────────────────
if [ "$DRY" = "1" ]; then
  ok "演练模式：这一步会执行 git add -A（把 $CNT 个文件放进待提交区）"
  STAGED=$CNT
else
  git add -A
  STAGED=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
  ok "已暂存 ${STAGED} 个文件"
  printf "  ${D}其中前 10 个：${X}\n"
  git diff --cached --name-only 2>/dev/null | head -10 | sed 's|^|    |'
  printf "  ${D}…… 其余 %s 个${X}\n" "$(( STAGED > 10 ? STAGED - 10 : 0 ))"
fi

# ─────────────────────────────────────────────────────────────
head2 "第 4 步：确认要提交吗？（这是第一次记录版本，放心）"
# ─────────────────────────────────────────────────────────────
if [ "$DRY" = "1" ]; then
  warn "演练模式：这一步会真的提交。"
  ok "演练模式：提交信息会写「feat: netdev 设备工具台 —— 首个公开版本」"
  STAGED=$CNT
else
say ""
say "  ${D}「提交」的意思是：给这 ${STAGED} 个文件拍一张快照存档。${X}"
say "  ${D}以后每改一次就再拍一张，GitHub 才能记录你的改动过程。${X}"
say ""
printf "  ${B}真的要提交吗？输入 y 确认，其它任意键取消：${X} "
read -r GO
if [ "$GO" != "y" ] && [ "$GO" != "Y" ]; then
  say ""
  warn "已取消，什么都没提交。"
  say "  ${D}文件只是被暂存了，随时可以撤销： cd ~/netops && git reset${X}"
  read -r _
  exit 0
fi

if [ "$CAN_COMMIT" = "1" ]; then
  git config user.name "$GITNAME"
  git config user.email "$GITEMAIL"
  git commit -q -F - <<'MSG'
feat: netdev 设备工具台 —— 首个公开版本

把串口/SSH/Telnet 三种接入统一到一条 CLI 路径，并用 tmux 实现人机同屏：
人工和 AI 看到同一块屏幕，AI 的每一步都可见、可拦、可回滚。

- 三种接入（串口 Console / SSH / Telnet）+ 厂商平台自动识别
- 写操作四道闸门：黑名单 → 人工审批 → 强制备份 → 逐行下发校验，失败即停
- AI 助手走直连 OpenAI 兼容 API：一把 API Key 即用，零额外 CLI 依赖
- 全部测试离线可跑（仓库自带本机模拟器），CI 每次 push 都跑
- 网页服务一条命令管理：netdev ui（没有就起、有就报状态）
MSG
  ok "提交完成！版本号 $(git rev-parse --short HEAD 2>/dev/null)"
else
  bad "因为没填邮箱，这一跳过了。下面第 5 步里有一条命令，照抄就行。"
fi
fi

# ─────────────────────────────────────────────────────────────
head2 "第 5 步：接下来你要做的（还有 3 件事，都是网页上的操作）"
# ─────────────────────────────────────────────────────────────

say ""
say "  ${B}第 1 件：建一个 GitHub 仓库（网页操作，约 2 分钟）${X}"
say ""
say "    打开这个网址："
say "      ${B}https://github.com/new${X}"
say ""
say "    然后照着填："
say "      Repository name        ${B}${REPO}${X}"
say "      Description             ${D}一句话介绍，选填${X}"
say "      ${D}Public（公开）—— 想让别人看、能给你提 issue 就选这个${X}"
say "      ${D}Private（私有）—— 先自己藏着，以后能改成公开${X}"
say "      ${B}不要勾${X} Add a README file"
say "      ${B}不要勾${X} Add .gitignore"
say "      ${B}不要勾${X} Choose a license"
say "      ${D}（这三个我们本地已经有了，勾了会冲突）${X}"
say ""
say "    填完点绿色的 ${B}Create repository${X}"
say ""

say "  ${B}第 2 件：把本地代码推上去（复制 1 条命令）${X}"
say ""
say "    等 GitHub 把它自己的指引页面显示出来，往下找到"
say "    ${B}…or push an existing repository${X} 那一段，里面有一条命令${X}"
say "    形如："
say "      ${D}git remote add origin https://github.com/${OWNER}/${REPO}.git${X}"
say ""
say "    ${Y}【如果你还没设邮箱】${X}先在这条之前执行这一条（把邮箱换成你的）："
say "      ${D}cd ~/netops && git config user.email \"你的邮箱\"${X}"
say "      ${D}cd ~/netops && git config user.name \"你的名字\"${X}"
say ""
say "    建议直接复制这段（含上面两行，改掉邮箱和名字）："
say ""
printf "      ${G}cd ~/netops${X}\n"
printf "      ${G}git config user.email \"你的邮箱@例子.com\"${X}\n"
printf "      ${G}git config user.name \"你的名字\"${X}\n"
printf "      ${G}git remote add origin https://github.com/%s/%s.git${X}\n" "$OWNER" "$REPO"
printf "      ${G}git push -u origin main${X}\n"
say ""

say "  ${B}第 3 件：把 token 填进去（只有这一步需要你多点几下）${X}"
say ""
say "    push 的时候 GitHub 会要你登录。${B}密码那一栏要填 token，不是你的 GitHub 密码。${X}"
say "    token 就是一把「只能用来推代码的临时钥匙」，可以在网页上随时作废。"
say ""
say "    ${B}拿 token 的方法（两种任选一种，页面上的文字略有不同）：${X}"
say ""
say "      ${B}方式一 · 细粒度令牌（GitHub 现在推荐）${X}"
say "        1) 打开 ${B}https://github.com/settings/personal-access-tokens/new${X}"
say "        2) Token name 填 ${D}netdev 我的电脑${X}；Expiration 选 90 days"
say "        3) Resource owner 选你自己"
say "        4) Repository access 选 ${B}Only select repositories${X} → 勾选 ${B}${REPO}${X}"
say "        5) 往下滑到 Repository permissions："
say "           ${B}Contents 改成 Read and write${X}（默认是 Read-only，不改会推不上去）"
say "        6) 最下面点 ${B}Generate token${X}"
say ""
say "      ${B}方式二 · 经典令牌（步骤更少）${X}"
say "        1) 打开 ${B}https://github.com/settings/tokens/new${X}"
say "        2) Note 填 ${D}netdev 我的电脑${X}；Expiration 选 90 days"
say "        3) 往下滑，勾上 ${B}repo${X}（就这一项，别的都不用勾）"
say "        4) 最下面点 ${B}Generate token${X}"
say ""
say "    ${Y}页面会显示一串字符（细粒度是 github_pat_ 开头，经典是 ghp_ 开头）${X}"
say "    ${Y}只显示这一次 —— 关掉页面就再也看不到了，务必先复制下来${X}"
say ""
say "    ${B}怎么粘到终端里：${X}在终端窗口里按 ${B}Command+V${X}（不是 Command+C）。"
say "    ${D}粘对了屏幕上看起来是空白的（密码不显示字符），这是正常的。${X}"
say ""
say "    提示 Username 时填：${B}${OWNER}${X}"
say "    提示 Password 时填：${B}刚才复制的那串 token${X}"
say ""

# ─────────────────────────────────────────────────────────────
head2 "做完之后"
# ─────────────────────────────────────────────────────────────
say ""
say "  ${G}✔${X} 打开 ${B}https://github.com/${OWNER}/${REPO}${X} 就能看到你的项目了"
say ""
say "  ${D}· 你的代码只在这台 Mac 上；换电脑的话重新 clone 就行${X}"
say "  ${D}· 以后改了东西要同步到 GitHub，让 ${B}bin/设备工具台.command${X} 或"
say "    ${D}README.md 里的「参与贡献」小节教你具体步骤${X}"
say "  ${D}· 有人给你提 issue 的话，那个网页就是用来讨论的${X}"
say ""
say "  ${Y}提醒：一旦公开，${X}"
say "  ${Y}① 任何人都能看到全部代码，包括历史版本${X}"
say "  ${Y}② 你以后删过的文件，历史里仍然能翻出来${X}"
say "  ${Y}③ 所以绝对不要把设备密码、IP、序列号提交进去${X}"
say "     ${D}（本项目已经把这几类文件排除在外了，上面第 0 步验过）${X}"
say ""

printf "\n${D}按回车关闭这个窗口…${X}"; read -r _
