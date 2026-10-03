╔══════════════════════════════════════════════════════════════════════════╗
║   设备工具台（netdev）· 安装说明                                         ║
║   用途：在任意 Mac 上装好"用浏览器调试网络设备"这套工具                  ║
╚══════════════════════════════════════════════════════════════════════════╝

【它是什么】
  一套本机工具：
    · netdev —— 真正去连设备（串口 / SSH / Telnet）的命令行工具
    · netdev-ui —— 自带浏览器界面（你主要用它的「终端」页，左栏有 3 个聚合入口），端口 8898
    · tmux —— 让你和 AI 看同一块屏幕、可回看历史

【装之前要什么】
  ✓ 一台 macOS 电脑（Apple 芯片或 Intel 都可以，本包为 Apple 芯片构建）
  ✓ 会用一个终端窗口（打开"终端"App，敲命令）
  ✗ 不需要事先装 Python（安装包自带）
  ✗ 不需要事先装 netdev（就是它）
  可选：
    · tmux（同屏/回看必需）：Homebrew 装一下 → brew install tmux
    · Node.js（如果你还想用 pi-web-ui 这个可选旧界面才需要）：brew install node
    · USB 转串口驱动：FTDI / CH340 / CP210x（厂家官网下，装完插线才认）
    · Homebrew 本身：https://brew.sh

【怎么装】
  0) 如果你拿到的是源码仓库（不是 .tar.gz）：
       bash dist/build_bundle.sh
     产物在 ~/Desktop/workbuddy/<日期>_netdev设备工具台_macOS_*.tar.gz
  1) 解压：双击 .tar.gz，或
       tar -xzf 设备工具台_macOS_*.tar.gz && cd 该目录
  2) 先预览（不会动任何文件）：
       bash install.sh --dry-run
  3) 正式安装：
       bash install.sh
     可选加参数：
       --piweb        顺便用 npm 安装网页界面（需联网，约 600MB）
       --no-launchd   不装"日志轮转"定时任务
       --no-hosts     不修改 /etc/hosts（默认会问你要不要加"防卡顿"屏蔽）
       --prefix DIR   换安装目录（默认 ~/netops）
       --workdir DIR  终端页命令装到哪个工作目录（默认 ~，即你的主目录）

  装完会打印一份自检（netdev doctor），全部 ✔ 就算成功。

【装到哪了（心里有个数）】
  ~/netops/                  程序 + 配置 + 备份（一个文件夹，别乱删）
    ├── config/              所有配置真身：设备清单、连接簿、终端页按钮、缓存
    ├── backups/             配置快照与自动备份（命根子）
    ├── bin/设备工具台.command  双击即用的菜单（体检/接入/备份/恢复/打包）
    ├── logs/ live/          运行留档与镜像日志（可删）
    └── README-从这里开始.txt  给"不懂技术"的人看的一页纸
  ~/netops/bin/netdev               命令行入口（软链）
  ~/.pi/commands.json        终端页那 3 个聚合入口（软链到 config/）
  ~/.zsh/completions/_netdev Tab 补全（软链）
  ~/Library/LaunchAgents/com.netdev.logrotate.plist  日志轮转（可选）

【装完怎么用】
  1) 启动网页界面（一个命令，最省事）：
       cd ~/netops && ./netdev ui open
     也可以：双击 ~/netops/ui/启动.command
     还可以：双击 ~/netops/bin/设备工具台.command（它会自动把服务拉起来）
       浏览器打开 http://127.0.0.1:8898 → 顶栏「终端」→ 左栏 3 个聚合入口
     ⚠ 服务是后台守护的：关掉终端窗口/启动器窗口都不会把它带走。
       打不开时先跑 ./netdev ui status（在跑=0，没跑=1），再 ./netdev ui 让它起来。
     完全不想用网页：双击 ~/netops/bin/设备工具台.command
  2) 接设备：USB-Console 线插设备 Console 口 → 终端页点「🔌 串口接入」
     看有哪些串口： netdev serial-discover
  3) 先备份（很重要）：点「💾 备份设备配置」→ 填客户名
  4) 改坏了要还原：点「♻️ 恢复配置备份」→ 选编号 → 按 y
  5) 日常体检： netdev doctor   （15 项全 ✔ 就正常）

【关于"AI 问答"这件事（重要说明）】
  本安装包只装"调试设备的工具链"，不含 pi agent 的登录/密钥：
    · 网页界面本身可以装（它带聊天界面），但要真正"问 AI"，需要在你自己的
      账号/密钥下配置（pi agent 的凭据不在本包里，也不应该被复制传播）。
    · 没有 AI 也完全能用：终端页、3 个入口、备份/恢复、体检 —— 都是本地能力。

【不会自动做 / 不适用的】
  · 不复制任何人的密码（密码在你自己的 macOS 钥匙串里，每台机器各自登录一次）
  · 不改设备的任何配置（只有你主动点"恢复/下发"才会写设备）
  · 不含 pi agent 的凭据；不含你的历史对话（那些在 ~/.pi/agent，属个人数据）
  · 只面向 macOS；Windows/Linux 需要另做（原理相同）

【怎么卸装】
  bash uninstall.sh            # 交互确认；先移入隔离区，不直接删除
  bash uninstall.sh --yes      # 不问直接卸（仍走隔离区）
  卸装会：移走 ~/netops、移走软链、卸载定时任务、把 commands.json 里的 3 条命令
           备份后移除。**你的快照与备份会被一起移入隔离区**（不会消失）。

【出问题怎么办】
  症状                     处理
  ───────────────────────  ─────────────────────────────────────────────
  netdev: command not found  先 source ~/.zshrc 或新开一个终端窗口
  串口插上认不到             装 USB 驱动；再用 netdev serial-discover 看
  终端页按钮不见了           netdev web repair（重新写 3 条）
  网页打不开                 cd ~/netops && ./netdev ui      ← 没有就起、有就报状态
                             还不行看日志：./netdev ui log -n 40
  服务老是"又起不来"         多半是以前用前台方式启动、关窗被杀 → 现在用 ./netdev ui，它是后台守护的
  会话里乱码                 波特率不符 → 重新接入（自动探测）
  想看以前的内容             终端里往上滚滚轮，或按 Ctrl+B 再按 [
  一切不确定                 双击 ~/netops/bin/设备工具台.command → 1（体检）

【版本与兼容】
  本包：netdev v1.0.0（Apple 芯片构建，自带 Python 3.13 + 离线依赖）
  Intel Mac：需要用 Intel 机器重新构建运行时（脚本里已参数化，见 build_bundle.sh）
  说明书与更新日志：装好后在 ~/netops/README.md（说明书）
                ·  ~/netops/README-从这里开始.txt（本文件）
                ·  ~/netops/CHANGELOG.md（改了什么、为什么这么改）
