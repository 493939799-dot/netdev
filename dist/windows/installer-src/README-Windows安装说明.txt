════════════════════════════════════════════════════════════════════
  netdev 网络设备工具台 · Windows 10/11 x64 安装说明
════════════════════════════════════════════════════════════════════

【这是什么】
  把串口/SSH/Telnet 接入的网络设备（华为 VRP / H3C / 思科系 /
  锐捷 / 迈普）变成"人机同屏会话"——人和 AI 共用同一块真实终端屏。

【安装前提】
  · Windows 10 / 11 64 位
  · Python 3.10 ~ 3.12（x64），安装时勾选 "Add python.exe to PATH"
      下载：https://www.python.org/downloads/
  · 用串口 Console：USB-Console 线（FTDI / CH340 / CP210x 驱动）

【安装（推荐：双击 exe，图形向导）】
  1. 解压本 zip
  2. 双击 netdev-install.exe
  3. 在向导里选安装目录、勾选「开机自启 / 装完打开界面」，点「开始安装」
  4. 等待完成（2-5 分钟，取决于网速）。日志实时显示在向导窗口里

【安装（命令行方式）】
  解压本 zip，在解压目录里执行：
      powershell -ExecutionPolicy Bypass -File install.ps1
  装到别的目录：
      powershell -ExecutionPolicy Bypass -File install.ps1 -Prefix D:\netops
  只预览不动文件：
      powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun

  安装器会：复制程序 → 装图形启动器 → 建 venv 装依赖 → 从模板生成 config\ →
  建 netdev.cmd 入口并加入用户 PATH → 启动网页服务 → 注册卸载入口 →
  建开始菜单/桌面快捷方式与开机自启 → 跑 doctor 自检。
  幂等，重复执行只更新程序（并重启服务加载新代码），保留你的配置和备份。

【日常使用】
  · 图形启动器：开始菜单或桌面「netdev 工具台」——看服务状态、打开界面、
    启动/重启服务、一键修复、查看日志，双击即用，不弹黑窗。
  · 网页界面：浏览器打开 http://127.0.0.1:8898 → 顶栏「终端」
  · 命令行（新开一个终端窗口）：
      netdev list                    列出设备
      netdev ui                      确保网页服务在跑
      netdev doctor                  体检
      netdev shell <设备名>          终端式同屏会话
      netdev run <设备名> "<命令>"   单条命令快速执行
  · MCP（给支持 MCP 的 AI 客户端）：命令指向 netdev-mcp.cmd
  · 完整使用手册：见交接包中的 netdev-Windows使用手册.md

【服务起不来 / 界面打不开怎么办】
  · 最简单：开始菜单 →「一键修复」，或打开「netdev 工具台」点「一键修复」。
    它会体检并自动修复常见故障（端口被占、服务没起、自启丢失、配置缺项等）。
  · 命令行等价物：netdev doctor（只体检）、一键体检.cmd -Fix（体检并修复）
  · 开机自启：安装时已建（登录后隐藏启动，不弹控制台）。若丢了，跑一次一键修复即可。

【卸载】
      powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\netops\uninstall.ps1"
  默认保留 config\ 和 backups\；加 -Purge 连目录彻底删除。

【写操作安全（四道闸门，不可放松）】
  ① 黑名单命令（reload / format / delete / reset saved-configuration）
     一律拒绝；② 人审弹窗，默认焦点在「否」、超时拒绝；
  ③ 写前强制备份；④ 逐条下发并校验，出错即停。
════════════════════════════════════════════════════════════════════
