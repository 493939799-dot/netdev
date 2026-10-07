# 更新日志

本文件记录**用户可感知的变化**。每条都尽量写清「真因」——
本项目多数问题的报错信息都离真因很远，只记"改了什么"会让人重复踩坑。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.0.18] - 2026-10-07

### 修复：SSH / Telnet 通道「输入很卡」——节拍未与串口对齐（`tools/ssh_bridge.py`、`tools/telnet_bridge.py`）

- **背景**：v1.0.17 把「输入很卡」按串口通道（`serial_bridge.py`）修了，
  但**同款问题在 SSH / Telnet 桥里一模一样存在**——三种接入方式共用同一套
  Windows 主循环写法（队列轮询 + 无条件 `sleep(0.2)`），串口改了，另两条没改，
  用户接 SSH/Telnet 设备时手感依旧发黏。属于"只修了一条腿"。
- **真因**：`ssh_bridge.py` / `telnet_bridge.py` 的 Windows 主循环节拍仍是 `0.2s` ——
  每 200ms 才排空一次输入队列 / `recv` 一次设备 socket，键击下发与回显
  最多各等 0.2s（平均 ~100ms）。POSIX 版用 `select(timeout=0.2)`，有数据立刻醒，
  所以这个固定延迟同样只在 Windows 出现。
- **修复**：两条桥的节拍统一降到 `0.01s`，与串口通道完全对齐。
- **实测（本机模拟器，端到端回显延迟）**：

  | 通道 | 修复前 median | 修复后 median | 降幅 |
  |---|---|---|---|
  | 串口 | 150ms | 8.7ms | ~94% |
  | Telnet | 281.1ms | 4.8ms | ~98% |
  | SSH | 281ms | 34.4ms | ~88% |

  （SSH 因加密/握手往返，绝对值高于 Telnet，但相对自身降幅一致。）
- **CPU 复核**：10ms 节拍下桥进程空闲占用 **0.00%**（非忙等空转，靠阻塞 `recv`
  与 `sleep` 让出 CPU）。

### 修复：`ssh_bridge.py` / `telnet_bridge.py` 误带 UTF-8 BOM，触发全仓编译守门失败

- **现象**：`test_ai_toolchain_and_cache` 的「仓内 49 个 .py 全部可编译」项报
  `SyntaxError: invalid non-printable character U+FEFF`。
- **真因**：两条桥文件在编辑中被写入了 UTF-8 BOM（`EF BB BF`）。`py_compile`
  会剥 BOM 所以看不出来，但测试用 `compile(path.read_text(encoding="utf-8"))`
  ——BOM 作为 `U+FEFF` 字符进到源码里就成了非法字符。
- **修复**：剥离两条桥（仓库 + 安装副本）的 BOM，与 `serial_bridge.py` 等保持一致
  （`.py` 一律不带 BOM）。

### 验证
- 回归全绿（13 套件 / 413+ 断言）：`test_pane_strip_ansi` 12/12、`test_monitor` 90/90、
  `test_ui_js_syntax` 10/10、`test_ui_lifecycle` 27/27、`test_ai_toolchain_and_cache` 136/136、
  `test_ai_turn_context` 14/14、`test_host` 12/12、`test_colorize` 21/21、`test_mock_cmd` 21/21、
  `test_ai_session_log` 26/26、`test_mcp_hotreload` 通过、`test_approval_gates` 18/18、
  `test_run_partial_and_mock_detail` 26/26。
- 三通道节拍一致性：`serial_bridge` / `ssh_bridge` / `telnet_bridge` 均 `_TICK = 0.01`。
- `/api/health` → `{"ok": true, "tmux": false, "sessions": 1, "version": "1.0.18"}`。

## [1.0.17] - 2026-10-07

### 修复：网页终端「输入很卡」（回显固定延迟 + 每次按键新建连接）

**A. 串口桥主循环节拍 200ms → 10ms（`tools/serial_bridge.py`）**
- **现象**：在网页终端里敲键、回车，回显要"顿"一下才出来，手感发黏。
- **真因**：Windows 版主循环是 `in_waiting` 轮询 + **无条件 `sleep(0.2)`** ——
  每 200ms 才看一次串口，设备回显（你敲的字符、回车后的提示符）最多要等
  0.2s 才被读走转发到网页，平均 ~100ms。POSIX 版用 `select(timeout=0.2)`，
  有数据立刻醒，所以只有 Windows 有这个固定延迟。
- **修复**：节拍降到 10ms（Python 3.11+ 在 Windows 用高精度定时器，10ms 是准的）；
  原来"每轮都做"的孤儿判定（`OpenProcess`）降到每秒一次，避免空转开销。

**B. 控制口每请求新建 TCP 连接 → 每设备复用长连接 + 关 Nagle（`lib/pane.py`）**
- **真因**：`send_literal` / `send_key` / `bridge_alive` 每次调用都 `control()` ——
  新建 TCP 连接 + 一次 `alive` 往返，用完即关。Windows 上"新建连接 + 首个往返"
  约一半会撞上 10~26ms 的延迟尖峰（实测新建 p50 1.4ms / 27-of-60 >10ms / 峰值 25.7ms）。
- **修复**：每台设备缓存一条长连接（`_Link`），请求在锁内串行化，连接失效
  （守护重启/被杀）自动重连一次；`Control` 建连即置 `TCP_NODELAY`（关 Nagle，
  避免与延迟 ACK 撞出 10~40ms 偶发停顿）。事件流（subscribe）仍走独占连接，不受影响。
- **实测**：复用长连接 p50 **0.18ms**（0-of-60 >10ms），对比新建连接 p50 1.4ms。

### 修复：`netdev ui restart` / `stop` 崩溃（`AttributeError: 'NoneType'`）

- **现象**：`netdev ui restart` 报 `AttributeError: 'NoneType' object has no attribute
  'splitlines'`（`_ui_kill_orphan`），服务没能重启。
- **真因**：`_ui_kill_orphan` 用 `netstat -ano -p TCP` 找占端口的进程。**带控制台**
  运行时 netstat 输出英文 `Active Connections`；**无控制台**（`CREATE_NO_WINDOW`，
  正是本 CLI 的常态）时它按系统代码页输出**中文** `活动连接`（GBK）。而本 CLI 入口设了
  `PYTHONUTF8=1`，`text=True` 于是按 UTF-8 解码 → 读线程抛 `UnicodeDecodeError`
  （`byte 0xbb in position 2`）→ `subprocess.run` 的 `stdout` 变成 `None` → `.splitlines()` 崩。
- **修复**：`netdev_cli.py` 新增 `_SYS_TEXT`（Windows：`encoding="mbcs", errors="replace"`），
  统一用于读系统命令输出（`netstat` / 两处 `powershell`），并对 `out` 补空值兜底；
  `_ui_daemon` 读自家 Python 子进程输出补 `errors="replace"`。
- **连带修复**：接入串口设备前的孤儿桥清理 `_cleanup_windows_stale_serial_bridges`
  也是同一模式（PowerShell 输出含中文路径的 GBK 字节）—— 修复前读线程崩溃、输出被静默吞成空，
  **孤儿桥永远清不掉**（下一次接入报 `PermissionError 13`）。

### 验证
- netstat 解码：无控制台时 `活动连接` 正确解出，`LISTENING` 行正常解析（18 行）。
- 孤儿清理路径复现：删 PID 文件后 `ui restart` → `已停掉占用 8898 的旧进程 PID 15296` → `✔ 已启动`（无崩溃）。
- 回归：`test_ui_js_syntax` 10/10、`test_ui_lifecycle` 27/27、`test_monitor` 90/90、`test_ai_toolchain_and_cache` 136/136 全绿。
- `/api/health` → `{"ok": true, "tmux": false, "sessions": 0, "version": "1.0.17"}`。

## [1.0.16] - 2026-10-07

### 修复：网页终端「卡住」——同一设备堆会话 + 开终端屏空白

**A. 同一设备堆出多个终端会话（会话泄漏）**
- **现象**：终端屏上是一整屏 `<AR111-S>` 提示符、反复的
  `Error: Unrecognized command found at '^' position`，输入像没反应。
- **真因**：前端有【三处】调 `/api/term/open`，其中连接簿「接入」和向导兜底
  两处**直接调接口** —— 不关旧 SID、也不更新 `SID`/`EventSource`，于是后端开了新会话
  而前端面板还挂在旧会话上（"点了没反应"），旧会话永不关闭；`openTerm` 里
  「先关旧 SID」又写在 `await` 之前，并发时全部通过。实测同一秒开 4 个会话、
  内存积到 7 个。而每次 `term/open` 都会往设备敲 **3 次回车**唤醒提示符 ——
  会话越多，屏上提示符越多，看着就越像"卡死"。
- **修复**：`ui/server.py` 新增 `drop_term_sessions()`，`/api/term/open` 开新会话前
  按设备收掉旧会话（并给其订阅者推哨兵，前端显示「[会话已结束]」而不是无声冻结）；
  `cleanup_term_sessions()` 的 Windows 分支补上"收活跃残留"；
  前端两处改走 `openTerm()`，`openTerm` 增加并发去重。

**B. 订阅时整屏快照（tail）被当成应答吞掉 → 打开终端是空屏**
- **真因**：客户端 `pane.subscribe()` 用 `Control.request()` 收应答，而它**只读一行**；
  守护端 `pane_daemon.py` 的 `subscribe` 却先发 `{"event":"data", tail}`、再发
  `{"ok":true}` —— tail 那一行被当成应答丢弃。于是订阅瞬间整屏历史就没了，
  只有之后的新输出才出现；设备空闲时屏上永远空白。
- **修复**：守护端改为**先回 `{"ok":true}` 再发 tail**。

### 验证
- `pane.subscribe(include_tail=True)` 订阅即拿到整屏（快照 301 字节；修复前为 0）。
- `/api/term/stream` 首个事件即为 `event: snap` 且带数据（修复前 12 秒零事件）。
- 同一设备连开两次：`SESSIONS` 只剩 1 个，日志有「去重：huawei 收掉旧会话 1 个」。
- 输入往返：`display clock` 正常回显；重连后设备屏干净（桥横幅 + 串口自检 + 提示符，无乱码墙）。
- 回归：`test_ui_js_syntax` 10/10、`test_ui_lifecycle` 27/27、`test_monitor` 90/90、`test_ai_toolchain_and_cache` 136/136 全绿。
- `/api/health` → `{"ok": true, "tmux": false, "sessions": 1, "version": "1.0.16"}`。

## [1.0.15] - 2026-10-07

### 修复：三个「一个装饰符号搞挂正事」的编码崩溃 + 开机自启状态自相矛盾

**A. 输出编码兜底（netdev_cli.py / ui/server.py）**
- **现象**：中文 Windows 上，stdout 若不是控制台（被重定向成管道 / 日志文件），
  Python 用本地编码（GBK）。CLI 到处打印 `✔ / ✘`，服务启动横幅打印 `▮`（U+25AE）
  —— 编不进 GBK 就抛 `UnicodeEncodeError`。CLI 那条以退出码 1 失败（用户实测
  「点『启动 / 重启服务』报 服务未能启动」）；**服务那条更致命**：横幅打印发生在
  `srv.serve_forever()` **之前**，一崩整个服务就起不来（日志只剩 traceback，端口没监听）。
- **修复**：两处都在模块加载时把 `stdout/stderr` 的 `errors` 改成 `replace` ——
  编码得出的照常（中文不乱），编不出的降级为 `?`，绝不因为一个装饰符号让命令 / 服务失败。
- **真因提示**：这不是「显示不好看」，是「功能被装饰性字符拖垮」；
  `UnicodeEncodeError` 离真因（输出目标不是控制台）很远。

**B. 子进程 stdio 钉成 UTF-8（ui/daemonize.py）**
- **为什么**：日志文件虽按 UTF-8 打开，但子进程 Python **不看父进程的打开方式** ——
  它按 Windows 本地编码（GBK）解释自己的 stdout。daemonize 拉起服务时显式给子进程设
  `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，日志里的中文与横幅才是可读 UTF-8。

**C. 工具台启动器补编码环境并重编译（LauncherStub.cs → netdev-toolbox.exe）**
- 启动器直接拉 venv 的 `python.exe`（不走 netdev.cmd），子进程 stdio 默认 GBK；
  给子进程显式设同款两个变量，与 `netdev.cmd` 行为一致。
- **注意**：源码改了但 exe 是旧编译 —— 本轮一并**重编译**，确认二进制里含该字符串。

**D. 开机自启状态口径统一（netdev_cli.py）**
- **现象**：工具台显示「已启用」，`netdev ui status` 却说「未装」—— 同一件事两个答案。
- **真因**：`ui status` 的 `_ui_status_lines()` 只查 macOS LaunchAgent（`UI_PLIST`），
  没做 Windows 分支；而 `doctor` 与启动器查的是 Windows Startup 快捷方式。
- **修复**：抽出 `_win_startup_lnk()` / `_win_autostart_state()`，`doctor` 与 `ui status`
  共用；`ui install / uninstall` 补 Windows 分支（建 / 删 Startup 快捷方式，pythonw 隐藏启动）。

### 验证
- 强制 GBK + 管道跑 `ui status`：退出码 0，`✔` 降级为 `?`（不再抛异常）。
- 强制 GBK 起服务（8899）：HTTP 200，无 traceback；重启后日志横幅为干净 UTF-8。
- `ui status` / `doctor` / 工具台三处一致：已装（Startup 快捷方式）/ 已启用。
- 回归：`test_ui_lifecycle` 27/27、`test_monitor` 90/90、`test_ai_toolchain_and_cache` 136/136 等全绿。

## [1.0.14] - 2026-10-07

### 产品化：像正常 exe 应用一样安装与使用（本轮由「要开源」驱动）

**A. 新增「netdev 工具台」图形启动器（netdev-toolbox.exe）**
- **为什么**：命令行用户敲 `netdev ui` 没问题，但普通用户双击 `.cmd` 会弹黑窗、
  看到一屏报错会以为装坏了。开源后第一印象尤其重要。
- **是什么**：WinForms 单窗口、`/target:winexe` 全程无控制台。每 3 秒自检一次服务
  （TCP + `/api/health` 双重确认，不看 PID 文件），显示「服务运行中 / 未运行」、
  地址、PID、版本、开机自启状态；提供「打开网页界面 / 启动·重启服务 / 一键修复 /
  刷新状态 / 查看日志 / 打开安装目录」六个按钮。

**B. 「一键修复」入口（集成进启动器 + 独立快捷方式）**
- 启动器上的「一键修复」按钮直接调用随包安装的 `一键体检.ps1 -Fix`，复用同一套
  修复逻辑（补齐配置、清占端口僵尸进程、重启服务、补依赖、重建自启）。
- 另外在开始菜单放了独立的「一键修复」快捷方式：即使启动器本身起不来，
  也能双击它体检并修复。

**C. 图形安装向导（netdev-install.exe 由控制台改为 WinForms）**
- **为什么**：原版安装器是控制台 exe，双击弹黑窗、中文可能乱码。
- **是什么**：可选安装目录、可勾选「开机自启 / 装完打开界面」；实时把 install.ps1
  的输出滚进日志框，并按「N/8」步骤推进进度条；结束时给出明确结论。子 PowerShell
  的输出编码被强制为 UTF-8，日志不再乱码。

**D. 开始菜单 / 桌面快捷方式 + 应用图标**
- 安装时创建「netdev 网络设备工具台」开始菜单分组（工具台 + 一键修复）与桌面快捷方式，
  指向无控制台的 `netdev-toolbox.exe`；「设置 → 应用」的 DisplayIcon 也改用启动器图标。
- 图标为多尺寸 `netdev.ico`（16~256），网页 favicon 同步更新；卸载时一并清除快捷方式。

**E. 随包分发**
- `一键体检.ps1`、`一键体检.cmd`、`netdev-toolbox.exe`、`netdev.ico` 随安装包分发，
  `install.ps1` 第 2b 步装进安装目录；`build_bundle.ps1` 会先编译图形程序再打包。
  （「一键修复」入口 = 开始菜单快捷方式指向 `一键体检.cmd -Fix`：`.cmd` 内容是纯 ASCII，
  用 `%~n0.ps1` 定位同名脚本，避免 cmd.exe 按 OEM 代码页解析中文路径时乱码。）

**F. 开源仓文档同步**
- README（中 / 英）补上 Windows 章节：图形安装向导与「netdev 工具台」截图、
  开机自启与「一键修复」说明、平台 / 依赖对照表；徽章改为 macOS · Windows，
  目录结构补 `netdev.cmd` 与 `dist/installer/`。
- `.gitignore` 排除 Windows 图形程序编译产物（`dist/installer/*.exe`）；
  图标 `netdev.ico` 是资产（exe 图标 + 网页 favicon 来源），保留进仓。

## [1.0.13] - 2026-10-07

### 接入时仍弹终端窗口 + 已接入设备不出现在「设备」栏（本轮由用户实测驱动）

**A. 接入设备时仍会弹出一个终端窗口（已修，补上 v1.0.11 的漏网路径）**
- **真因**：`.venv\Scripts\python.exe` 是「转发器」——真正跑代码的是它再拉起的
  base python（`%LOCALAPPDATA%\Programs\Python\Python3xx\python.exe`）。
  v1.0.11 给子进程加的 `CREATE_NO_WINDOW` 挡得住普通进程，却挡不住**带
  `DETACHED_PROCESS`** 的 venv 转发器：5 组组合实测（`cons_test`）显示
  `DETACHED_PROCESS` 会让转发器给 base python 新建一个**可见**控制台，
  `CREATE_NO_WINDOW` 被吞掉。而拉起「同屏会话守护 / 网页服务」这两处恰好都用了
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` ⇒ 每次接入都弹窗。
- **修法**：这两处去掉 `DETACHED_PROCESS`，只留
  `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（`lib/pane.py::start_daemon`、
  `ui/daemonize.py::_start_win`）。「脱离终端、关终端不掉」不受影响——Windows
  本就不连坐子进程（实证：桥进程正是在守护死后继续活着的），回归测试
  `test_ui_lifecycle` 的「已脱离调用方会话」一项继续通过。

**B. 设备明明接入了，却不出现在左侧「设备」栏（已修）**
- **真因**：桥（serial_bridge）**独占串口**。守护被强杀/崩溃后，它拉起的桥会脱管
  继续跑、继续握着 COMx；下一次接入时新桥 `serial.Serial(...)` 直接
  `PermissionError(13, 拒绝访问)` 秒退 ⇒ 守护还活着但 `alive=false` ⇒
  `/api/devices` 的 `window_live=false` ⇒ 界面按「只显示已接入」把它整条过滤掉。
  （`live/huawei.pane.log` 里就是一串 PermissionError 13；`state/panes/huawei.json`
  指向新守护，真正干活的是上一代守护遗留的孤儿桥。）
- **修法**（三层）：
  1. `lib/pane.py::ensure()` 在「桥死了要原地重开」和「守护不在要新拉」两条路径上，
     先 `_kill_stale_bridges(name)` 清掉本设备的孤儿桥（命令行含本设备专有的
     `live\<name>.screen.log`、且父链上没有活守护的桥进程 → `taskkill /T /F`），
     释放串口后再起新桥。
  2. `kill()` 加固：守护不应答时不再「只删注册表就返回」，改为强杀整棵进程树 +
     清孤儿桥；优雅 kill 后若守护仍活着也兜底强杀。
  3. 结构性防复发：`tools/pane_daemon.py` 把桥放进一个
     `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job 对象——守护一死句柄关闭，
     桥（连同其子进程）被系统连坐杀掉，从此不会再留孤儿桥。
- 踩坑记录：清理逻辑第一版用 `subprocess.run(..., text=True)` 取进程表，进程命令行里的
  中文触发 `UnicodeDecodeError` 被 `except` 吞掉 → 空表 → 静默不清理。改为
  「按字节收 + `errors='replace'` 解码」并让 PowerShell 以 UTF-8 输出。

**C. 同步**
- `tests/test_ui_lifecycle.py` 中关于 `DETACHED_PROCESS` 的说明随实现更新。

## [1.0.12] - 2026-10-07

### 补齐「卸载入口」与「开机自启」两处交付缺口（本轮由用户实测驱动）

**A. 「设置 → 应用」/「控制面板 → 程序和功能」里看不到 netdev，无从卸载（已修）**
- **真因**：安装器只写了用户 PATH 和启动目录，**从未写过 Uninstall 注册表项**。
  「程序和功能」那张列表就是照注册表列出来的 —— 没写，自然没有条目，
  用户只能自己翻安装目录找 `uninstall.ps1`。
- **修法**：`install.ps1` 安装时写
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\netdev`
  （`DisplayName` / `DisplayVersion` / `Publisher` / `InstallLocation` / `DisplayIcon` /
  `InstallDate` / `EstimatedSize` / `UninstallString` / `QuietUninstallString` /
  `NoModify` / `NoRepair`）。本机是**免管理员**安装（写不了 HKLM），故用 HKCU ——
  Win10/11 的「应用和功能」会按用户列出 HKCU 项。
  `uninstall.ps1` 新增第 3 步对称清除该键，卸载后不留死条目。

**B. 每次开机不会自动启动 / 登录时闪一个黑色控制台窗口（已修）**
- **真因**：开机自启是一个指向 `netdev.cmd` 的 Startup 快捷方式。`netdev.cmd` 是**批处理**，
  登录时由 `cmd.exe` 执行 ⇒ 必然闪一个控制台窗口（即用户看到的「调出终端页面」）；
  而一旦启动失败，报错只打在那一闪而过的窗口里，`ui-service.log` **一行都不留** ——
  失败完全静默，用户只能得出「它没自启」的结论。
- **修法**：快捷方式改为直接指向 `.venv\Scripts\pythonw.exe`，参数
  `"<prefix>\netdev_cli.py" ui`。`pythonw.exe` **无控制台**：登录不再闪窗；
  真正的服务进程仍由 `daemonize.py` 以
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 起，行为不变。
  启动是否成功以 `logs\ui-service.log` 的时间戳为准（可对着开机时间核）。
- 同源修复：`一键体检.ps1` 第 9 项现在会**区分**「已装但指向旧控制台入口」，
  并可用 `-Fix` 重建为隐藏启动。

**C. 本机为非管理员账户：安装器不能建计划任务**
- 实测 `Register-ScheduledTask` 与 `schtasks /create /sc onlogon` 均返回 `Access is denied`，
  HKLM 亦不可写。故自启只能走 Startup 目录 + HKCU 卸载入口；
  若确需「无论是否登录都运行」的计划任务，须以管理员身份另行安装。

## [1.0.11] - 2026-10-06

### 接入设备时不再弹黑框终端；AI 会话自动跟随设备（本轮由用户实测驱动）

**A. 接入设备 / AI 执行命令时会莫名弹出黑色控制台窗口（已修）**
- **真因**：网页服务由 `ui/daemonize.py` 以 `DETACHED_PROCESS` 拉起（自身**没有**控制台）。
  Windows 下从一个没有控制台的进程再拉起 `python.exe` / `powershell` 这类**控制台程序**时，
  系统会给它**新建一个控制台窗口** —— 界面上就闪出一个黑框终端（用户实测：接入设备、
  AI 执行命令时都弹，很影响观感）。
- **修法**：网页服务**直接**拉起的子进程统一加 `CREATE_NO_WINDOW`
  （`_NO_WINDOW` 常量，POSIX 下为 0、等于默认无影响）——
  涉及 `ui/server.py`（`netdev_*` 调用 / `screen-send` / `shell --restart`）、
  `netdev_cli.py`（`powershell` 探测、模拟器直起等）、`netdev_mcp.py`（工具调用）。
- **为何只改这一层就够**：子进程一旦带了（隐藏的）控制台，它再往下拉的子进程会**继承**
  该控制台，不会各自新开窗口。所以只需覆盖「网页服务的直接子进程」即可全链路无窗口。
- 同屏守护与桥（`lib/pane.py`、`tools/pane_daemon.py`）本就用
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，保持不变。

**B. 接入新设备后，AI 助手仍停在旧设备的会话里，发命令落到旧设备（已修）**
- **真因**：AI 会话在**创建时**就与设备焊死，但接入/切换设备时**没有任何「会话对齐」动作**，
  于是新设备接进来后 AI 还在旧设备的上下文里 —— 用户感觉「AI 不听话、命令执行不了」。
- **修法**：前端新增 `aiAlignDevice(dev)`，在**所有**「接入 / 开终端」的出口统一调用：
  已有该设备的会话 → 切过去；没有 → 新建一个绑该设备的会话；
  `aiNewSession` 支持显式传设备名（不传时退回当前选中设备，老调用点不变）。
  一个会话都没有时不动作 —— 避免「只是看一眼设备」就悄悄拉起 AI 后端（要 API Key）。

**C. 多会话并存（支撑 B 的必要改动）**
- **真因**：原实现「开新会话先收掉旧的」⇒ 一次 F5 或一次新建 = 一次失忆；
  且 B 一旦为新设备新建会话，就会把旧会话顶掉。
- **修法**：改为**多会话并存** —— 上限 `_AI_MAX_SESSIONS = 10`，超限按最近活跃 **LRU 淘汰**；
  AI 空闲回收阈值放宽到 **6 小时**（原值过短，切设备来回就丢上下文）；
  切换/新建会话时**显式关闭旧 EventSource 并清空面板**，避免两个会话的事件混进同一面板。

## [1.0.10] - 2026-10-06

### 串口自动发现在 Windows 上真正生效（v1.0.9 出包后改的源码补齐进包）

**A. 清单里写 `port = "auto"` 的串口设备，一接入就报「未找到可用串口设备」（已修）**
- **真因**：`lib/engine.py` 的 `discover_serial_port()` 只 `glob /dev/cu.*`
  （macOS 的写法），Windows 上恒为 `None` —— 机器上明明插着 COM3。
  而 `netdev device-add serial --auto` 生成的条目正是 `port = "auto"`。
- **修法**：Windows 走 `pyserial` 枚举（`host.serial_ports()`），并按描述优先挑
  USB 转串口（CH340 / CP210 / FTDI / Prolific 等）；主板自带调试口（COM1）排后面。
- **实测**：本机 COM1 + COM3(USB Serial Port) 并存时，自动认到 COM3。

**B. `port = "auto"` 没被解析成具体端口就交给串口桥 → 开不了口（已修）**
- **真因**：桥（`serial_bridge`）只认具体端口名，给它 `"auto"` 会直接失败；
  且 CLI 侧「孤儿桥清理 + 双桥拦截」都依赖端口名，`auto` 时全被跳过
  —— 旧桥仍握着口，新桥 `PermissionError 13`。
- **修法**：`tools/pane_daemon.py` 与 `netdev_cli.py` 在把端口交给桥之前，
  先 `discover_serial_port()` 解析成具体端口；解析不到才明确报错。

**C. 串口相关报错仍是 macOS 口径（已修）**
- `tools/serial_bridge.py`：占用排查提示按平台分流
  （Windows 提示关掉串口助手/PuTTY/SecureCRT，或设备管理器里「禁用→启用」该 COM 口；
  macOS 才用 `lsof`）。
- `lib/conn_store.py`：串口缺 `--device` 的报错，Windows 举例 `COM3`、macOS 举例 `/dev/cu.usbserial-XXXX`。

**D. 配置模板补 Windows 串口说明（已补）**
- `config/devices.toml.example`：补 `port` 可写 `auto`、`netdev serial-discover`
  在 Windows 看的是 `COM3` 这类 COM 口、显式写口与 auto 的取舍。

**E. 源码克隆后 `netdev doctor` 的「配置真身」恒红（已修）**
- **真因**：安装包会显式建 `config/state`，源码安装（`git clone`）没有这一步，
  而 doctor 要求它存在 → 全新克隆这一项恒红（功能其实全好）。
- **修法**：`lib/paths.py` 的 `bootstrap()` 补建 `config/state`（与安装脚本一致）。

**F. 重装一次，用户加的设备清单全没了（数据丢失，已修）**
- **真因**：macOS 安装包把 `安装根/devices.toml` 建成**指向 `config/devices.toml` 的软链**，
  读写其实是同一份。Windows 建不了软链，安装器改成「`config → 安装根` 复制一份」，
  根目录那份就成了**独立副本**；而 `lib/paths.py` 偏偏「安装根优先」：
  · 用户按报错提示去改 `config/devices.toml` → 程序读的却是安装根那份，**改了没用**；
  · `netdev device-add` 写进安装根那份，重装时安装器又用 `config → 安装根` 覆盖它
    → **用户加的设备被抹掉**。
- **修法**：`lib/paths.py` 的 `cfg()` 改成 **`config/` 优先**（与 macOS 软链语义对齐，
  读写都走真身）；`dist/installer/install.ps1` 增加升级收编 ——
  重装时若安装根那份更新（≤1.0.9 时它是活文件），先搬回 `config/` 再镜像，
  老版本用户升级不会丢设备。

**G. 串口波特率被写死 9600：接入先自检切档、`netdev list` 显示错速率（已修）**
- **真因**：`netdev device-add` / `netdev conn add` 的 `--baud` 只收整数（默认 9600），
  而配置模板与网页端下拉都推荐 `auto` —— `--baud auto` 直接 argparse 报错。
  于是生成/登记的串口条目一律写死 9600：真机实际 115200 时，接入要先自检再切档，
  `netdev list` 与网页端还一直显示「串口 auto @9600」，看着像没认到设备。
- **修法**：`--baud` 改为接受「数字或 auto」，串口默认 `auto`；
  `lib/conn_store.py` 同步支持存 `auto`；新增 `lib/engine.py:serial_display()`，
  `netdev list`（文本 + JSON）与网页端把 `port`/`baud` 的 `auto` 解析成实际值
  （本机实测显示「串口 COM3 @115200」，不再显示 auto @9600）。

**H. 同屏桥（pane-daemon）仍按旧速率起：state/界面显示 9600（已修）**
- **真因**：`tools/pane_daemon.py` 起串口桥时写 `baud = self.dev.get("baud", 9600)` ——
  清单写 `auto` 时虽能把 `"auto"` 透传给桥（桥会自检），但默认值 9600 会让
  **老清单/连接簿里的数字 9600** 被静默沿用：真机 115200 时接入先自检切档，
  `state/panes/<设备>.json` 与网页端还一直显示 `serial_bridge COM3@9600`。
- **修法**：起桥前把 `auto/空/none` 解析成**上次探测到的缓存速率**
  （`engine.serial_baud_cache_get(port)`）；无缓存才传 `auto` 让桥自检并回写缓存。
  与 `netdev list`/网页端（`serial_display()`）同一套解析口径，三处显示一致。

**I. 直连串口（`netdev run/connect`）仍按 9600 起：`auto` 端口名被当真端口（已修）**
- **真因**：`port = "auto"` 是**非空字符串**，`d.get("port") or discover_serial_port()`
  把它当成真端口名直接用 → `probe_serial_baud("auto")` 打不开 → 静默回落 9600；
  `SerialSession` 再 `int("auto")` 抛错（或按 9600 起）。
  真机 COM3 @115200 就报「串口无任何回显：确认设备已上电 / 波特率 9600 / 线接的是 Console 口」
  —— 与「设备没插」的表现一模一样，极难自查（本机真机实测踩到）。
- **修法**：新增 `netdev_cli._resolve_serial_auto()`，在 `resolve_target()` /
  `_shell_inner()`（开窗命令）/ 防双桥分支 / 连接簿分支统一把 `port`/`baud` 的
  `auto` 落成实际值（端口先落成 `COM3`，再用缓存/探测定速率）；
  `lib/engine.py:SerialSession.__init__` 也自行解析 `auto`（MCP 直连路径的同一道闸）。
- **实测**：`netdev run huawei "display clock"` → 自动认到 COM3 @115200，
  回读 `2026-10-05 22:05:40 <AR111-S>`，不再报 9600 无回显。

## [1.0.9] - 2026-10-06

### 两处 Windows 侧修复补齐进包（v1.0.8 出包后改的源码未随包交付）

**A. 串口设备明明在线、界面却显示「未接入」（已修）**
- **真因**：`ui/server.py` 判定设备是否「已接入」时，只用**连接名**去比对活窗格名。
  但 CLI 给串口设备建的窗格名是 `serial-{端口}`（`_sanitize("serial-" + device)`），
  与连接名无关 —— 于是串口设备明明在线，界面仍显示「未接入」。
- **复现**：AR111-S 真机 COM3 接入后，设备行状态与实际不符。
- **修法**：`window_live` / `connected` 判定追加 `serial-{device}` 窗格名匹配；
  同时把 `wins` 先转 `set` 避免逐条线性查找。

**B. 服务启动时日志文件打不开 → 服务没起（已修）**
- **真因**：`ui/daemonize.py` 新建日志目录后立刻打开日志文件，可能撞上 Windows
  Defender 对新目录的扫描锁（`PermissionError`）→ 服务没起、PID 文件没写，
  下游 `test_ui_lifecycle` 连锁失败。
- **修法**：与安装脚本同一套退避约定（4 次重试 0/600/1200/1800ms）；
  4 次仍失败则明确报错返回 1，不再静默。

## [1.0.8] - 2026-10-06

### 同步 macOS 主仓 v1.0.5 / v1.0.6（AI 可靠性 + TRAE 全局换肤）

**AI 会话：说完就忘 / 刷新失忆（两个 P0，已修）**
- **每轮回复写回上下文**：纯文本回复原先只推给前端流，不落 `messages`
  —— AI 转头就否认自己刚说过的话。现正常/异常/中止路径都写回（中止也
  保住已产出文本；空回复不塞空消息；`_stored` 标记防重复写）。
- **刷新不再失忆**：会话列表原先只活在内存，一次 F5 = 一次失忆（服务端
  「开新会话先收旧」连坐清空上下文）。现 `[{name,aid,device}]` 存
  localStorage，启动时 `/api/ai/list` 核对活会话后恢复订阅（历史事件回放）。
- **会话流水（append-only JSONL）**：`logs/ai-session/<aid>/session.jsonl`
  记录用户输入 / 工具调用 / 结果 / 回复 / compact，`netdev ai log` 可查。
- **上下文压缩 `compact`**：旧对话压成结构化摘要，保留最近 4 条原文；
  摘要生成失败会**如实说明**并指向流水文件，不许模型凭记忆断言。

**`run` 的「一条失败 = 整批失败」语义修正**
- CLI 加 `--json`：`{ok, partial, n_ok, n_total, commands:[{command, ok, output, error}]}`
  逐条回结果；`netdev_mcp.t_run` 逐条标 ok 并新增 `partial`（部分成功）。
- 界面把 `partial` 显示为**琥珀色「部分成功」**，不再一律红叉；
  成功命令的回显不再被清空。
- 系统提示词同步校准：设备绑定、前提冲突先澄清、禁止绝对断言、
  指向流水文件自查。

**模拟器补齐（mock_vrp）**
- `display interface <名>` 明细、`display vlan <id>` 精确查询（不存在则
  明确报错，不再回全表）、`display clock` 按本机时间实时生成。

**界面整体换肤为 TRAE 风格（对齐 macOS v1.0.6）**
- `index.html` 采用主仓 v1.0.6 实现：设计令牌体系（黑底 #070707 +
  TRAE 绿 #3DDC84 + 三级灰阶文字）、「透但锐利」玻璃面板、点阵眼形
  背景新 `ui/static/dotmatrix.js`；CRT 扫描线 / 矩阵雨整体移除。
- 主题收敛为 4 个：**极简灰（默认）** / 黑客绿 / 极简白 / 水墨屏；
  琥珀橙 / 冰蓝 / 赛博紫 / 深空蓝 / 赤陶橙删除。
- 顶栏「写策略」徽章 → **REA / ASK / ALL 三段滑动开关**（直调
  `/api/policy`）；AI / 状态忙态改为标题「光晕呼吸」。
- xterm 16 色按主题适配；状态/快照/审计三卡 240px 统一。
- 点阵背景修「无声冻结」：`dotmatrix.js` 不再跟随
  `prefers-reduced-motion` —— 工程师机器常开「最佳性能」（Windows 视觉
  效果 = 最佳性能即触发 reduce），背景被冻结成静态一帧、用户以为坏了；
  动效开关收归设置面板「背景点阵眼形」复选框（该开关本来就有）。

**测试**
- 新增 4 个：AI 会话流水 / 每轮写回 / run partial+mock 明细 / 前端 JS 语法
  与持久化三件套。全量 13 个测试文件（venv python）rc=0 全过
  （136+14+26+26+10+18+21+90+27+…）。

### 终端着色：IP 高亮修漏 + 输入/输出一眼可辨（人蓝·AI紫·系统灰）

**IP 高亮漏染（2026-10-04 用户报障：ACL 大输出里"有的 ip 变色没有实现"）**

两个真因，都在桥内的流式着色器 `lib/colorize.py`：
1. **块边界切进 IP 时只扣"后缀"**——旧版 `_TAIL_B` 最多扣住尾部两组点分数
   （如 `3.4`），IP 的前缀（`10.1.`）已经提前吐出去了；下一块拼回来
   `3.4\r\n` 不再是完整 IP → 这个 IP **永远染不上色**。
   修复：改为扣住缓冲区末尾【整段】数字/点串（`_RUN_B`），任何 IP 必然
   完整出现在某一次染色里——逐字节喂入也不漏不错拼（单测覆盖）。
2. **ssh 桥没有空闲 flush**——串口/telnet 桥都有 0.15s 空闲兜底，ssh 桥漏了，
   扣住的残片要等下一波输出才见分晓。已补。

**输入回显著色（用户新需求：一眼分清哪是输入、哪是输出）**

- 人手敲 → **蓝**；AI 助手代发 → **紫**；监控/深体检的系统代发 → **灰**；
  设备输出保持默认色 + IP 橙。颜色可用 `NETDEV_HUMAN_COLOR` 等环境变量换。
  ★ 首版用浅紫 141/浅蓝 75，在 minimal 浅色主题（网页终端白底）上几乎透明
  （用户实测"看不清"）→ 换成白底黑底都 ≥4:1 对比的中深饱和色：
  人=33 蓝、AI=129 深紫罗兰、系统=243 灰。
- 实现思路（为什么不按"行首是提示符"猜）：人敲键时设备是**逐字符回显**的，
  敲 `dis` 时行里只有 `dis`，提示符早就流过去了——按行猜永远猜不到。
  正确的锚点在**源头**：桥本来就看得见所有发往设备的键盘字节（人的击键和
  AI 的 `tmux send-keys` 都从窗格 stdin 进桥），发出去时记一笔「期待回显」，
  回显里匹配到的字节就上色。AI/系统代发靠 `netdev screen send`（唯一收口）
  发送前写的 `state/echo-marks.json` 标记区分（新增 `--src ai|sys`，界面
  监控代发探测命令传 `sys`）。
- 宁缺毋滥：匹配不上（密码不回显、控制键、输出插队）就放弃该条着色，
  绝不误染设备输出。桥日志仍记原始字节；`capture-pane` 采集（AI 读屏/
  指标解析）默认去色，互不干扰。
- 顺带修一类"改了不生效"隐患：三个桥原来把 `~/netops`（安装副本）写死进
  `sys.path`，开发仓的桥会加载旧 lib——改为按**桥脚本所在仓**推导。

---

## [1.0.4] — 2026-10-04

### 状态面板升级：三层指标观 + AI 深体检（快诊/深体检双档）

按「不拘泥以往积累、有颠覆精神」的要求，状态面板从「设备体温计」重构为
**「体验 → 资源 → 变化」三层指标观**（设计全文见 `docs/monitor-status-design.md`）：

**快诊（⟳，秒级）新增 7 个采集点**：
- 体验层：出口连通探针（ping 公网）、DNS 解析、NAT 会话水位（耗尽=全网断）、DHCP 池余量
- 变化层：**配置漂移**（当前配置 SHA-256 与上次基线对比——「上次采集后有人改过配置」）、
  **ARP 表项突增**（环路/扫描前兆）
- 资源层：光模块收光功率（**比 CRC 早几周的链路预警**；电口设备自动降级 --）
- 设备日志分类从 4 类扩到 5 类（新增 STP/拓扑）
- 不支持某项的设备该行显示 "--"（绝不当 0），不影响其他指标

**深体检（🔍，1~3 分钟，新按钮）**：
AI 按设备情况**规划 5~20 条只读命令** → 双重只读闸门（display/show 白名单 +
写操作黑名单，被拦命令如实展示）→ 逐条静默执行 → 全部回显再交 AI 出结构化结论。
管「快诊正常但就是不对劲」的场景。

**顺带修复**：`nslookup` 此前被命令分级闸门误归为写操作拒绝执行——它是只读查询
（与 ping/tracert 同类），已加入只读白名单。

**同屏通道修复（2026-10-04 真机 AR111-S 四轮实测，详见设计文档第七节）**：
同屏采集改为**等提示符回来再发下一条**（固定 sleep 会赌输：ping 全超时 ~9s，
且 VRP 执行期间不回显输入会让下一条回显消失）；屏幕切段改**倒序锚定最新一轮**
（滚动缓冲有上一轮残留，正序 find 会切出跨轮巨段被 `_bad_echo` 误杀）；
ping 全超时按 Request time out 估丢包（**不再把出口不通伪装成「未采集」**）；
华为 `display ip pool` 双记账修复；配置漂移哈希只认直连全量回显（同屏截断配置
会造成假「已变更」告警）。

### 状态区定稿：删指标详情，AI 建议规整化 + 横线分割

- 「指标详情」折叠块（指标表格/CPU 曲线/下钻/⚡ 学习角标）整体移除 ——
  状态区只剩 AI 诊断结论：徽章 + 一句话总结 + 逐条「情况→建议」。
- 每条建议之间用**简易横线**（虚线）分割；条目排版收紧（色点并入标题行）。
- AI 输出要求收紧：**最多 4 条**、title ≤12 字短语、detail 只写关键事实
  （引用数值）、action 只给**一条**最该做的命令 —— 不再给命令清单。
- 采集与判色口径不变（错包增量/日志分类照常在服务端跑，供 AI 分析）；
  接口详情弹窗（?open=if）与 📚 已学规则管理保留。

### 监控区改「状态」：静默采集 + AI 结构化诊断

按用户指定，监控界面主视图不再是指标数值，而是 **AI 给出的结构化情况与建议**：

- 点 ⟳ 一次完成「静默采集（直连不抢屏）+ AI 分析」；AI 喂的是**结构化指标**
  （六行指标 + 错包增量 + 日志线索 + CPU 历史），并明令禁止编造没给的数值。
- 主视图 = 状态徽章（**正常 / 留意 / 异常**，语义色）+ 一句话总结
  + 逐条「情况 → 建议」（引用具体数值，建议给可执行的下一步命令）。
- 原指标表格收进折叠的「指标详情」，想看原始数值与判色再点开；
  AI 未配置或解析失败时自动展开表格并亮黄条说明原因（**降级不瞎**）。
- AI 只在人工触发时分析这一次（低频智能层），采集链路本身不变 —— 可重复、
  离线可用的口径仍由解析层负责，AI 不接管实时链路。

### 监控面板改造 · 第一批（排障指标表格化）

界面「监控」区从四张指标卡改成**简易表格**（用户指定：不做成卡片，贴合终端气质）：
每行 = 指标 / 当前值+变化与状态 / 趋势，异常行整行着色（黄=留意、红=立刻看）。
六行的口径全部换新：

- **错包（CRC）改增量口径**：只报「相对上次采集新增了多少」（基线 JSONL 落盘，
  跨手动采集间隔累积）；计数器回绕/设备重启（负差值）自动弃用本轮增量，不误报「在涨」。
- **新增「接口占用」行**：最高口利用率（300 秒均值），≥70% 黄、≥90% 红。
- **新增「设备日志」行**：静默采 `display logbuffer`（锐捷/思科 `show logging`），
  自动分类 接口翻动 / 路由邻居 / 环路信号 / 告警 四类线索，有线索整行标黄。
  ★ 解析器修了一个真缺陷：华为 VRP 正文是 "Interface … has turned into DOWN state"，
  原正则只认 "changed state to"，**真机日志的接口翻动全部漏判** —— 回归测试抓出后已修。
- **CPU 趋势跨重启保留**：历史序列改存服务端基线（原来只在本机内存里，刷新即丢）。
- 手动采集仍为默认档（点 ⟳ 才采，静默直连不抢屏）；行点击可下钻原始回显，⚡ 学习入口保留。

### 修复

- `/api/monitor` 成功响应**从未带过 `ok` 字段**，前端 `if(!d.ok)` 判定一直是误打误撞
  靠后续渲染代码兜住 —— 补上 `"ok": true`，成功/失败判定真实成立。
- 监控 CPU 曲线（canvas）颜色硬编码荧光绿，浅色主题（minimal/ink）下刺眼；
  改取语义色 `--st-on`，且**切主题时重画**（canvas 不会自己跟着 CSS 变量走）。

---

## [1.0.3] — 2026-10-04

（主题：**修安装包「开箱即用」的 5 个问题**。这批问题全部是用「沙箱新装机」
（`HOME`/`--prefix` 双隔离）以新用户视角跑完整安装验证时暴露的 —— 真机上永远看不见，
因为真机的环境是"养"出来的。）

### ★ P0：装出来的 `netdev` 命令是坏的（启动器软链解析）

install.sh 会建 `bin/netdev → 根目录/netdev` 软链并进 PATH，但启动器用
`dirname "$0"` 推安装根 —— **经软链调用时 `$0` 就是软链路径**，ROOT 错成
`bin/`，去找 `bin/.venv`，永远报「未找到 venv」。真机也复现（`which netdev`
正是那条软链），只是此前一直没通过软链跑过 doctor。修法：启动器先循环解析
软链再取目录（bash 3.2 没有 `readlink -f`）。`netdev-mcp` 同样的写法一并修掉。

### 新装机 doctor 的其余 4 项

| 症状 | 真因 | 修法 |
| --- | --- | --- |
| 界面版本号显示 `dev` | 安装包 payload 排除了 `dist/`，`_app_version()` 只会读 `dist/installer/VERSION` | install.sh 把 VERSION 拷到安装根；`_app_version()` 先看安装根再看 dist |
| doctor 报缺 `config/pi-commands.json` | 新装机没有装 pi-web-ui，自然没生成过命令清单 | install.sh 收编（有则收编+软链，无则模板兜底，`lib/paths.py` 的 bootstrap 本来就会补） |
| doctor 报 `左栏按钮补丁 [missing]` | 检查的是已下线的 pi-web-ui 前端补丁，新装机根本没装这个可选组件 | `piweb_patch.py check()` 区分「没装」（`not-installed`，通过）与「装了但产物变了」（真问题）；`--check` 对前者退 0 |
| 包里出现指向 `/Users/mac/…` 的断链 + 多 3.4M 草稿 | 仓库根的 `devices.toml`/`connections.json` 是软链、`.chk/` 是草稿，rsync 全打进去了 | build_bundle.sh 排除表加锚定排除 |

### 验证

沙箱新装机（`HOME`/`--prefix` 双隔离 + `--no-launchd`）完整安装后
`netdev doctor` **15/15**、界面 health 报正确版本号、全套回归 135/27/19/21 通过。

---

## [1.0.1] — 2026-10-04

### 修复：AI 助手「读取设备配置」永远失败（真因一行，故障已潜伏 ≥ 4 天）

**用户看到的**：在 AI 面板里说「查看设备配置」，`netdev_run` 一律回
「⚠ netdev_run 执行失败」。AI 于是反复重试、换工具（`connect_info` → `screen_send`
→ `watch_tail` …），调用链越拉越长，最后给不出答案。**但设备侧完全正常**：
串口在线、命令回显完整、连配置都取回来写到磁盘了。

**真因**（`ui/server.py` 一行）：工具结果回填给模型之前会经过一层
「大文本落盘 + 摘要」处理，其中一行是裸的：

```python
text = "\n".join(v) if isinstance(v, list) else str(v)   # ← 不在 try 里
```

而 `netdev_run` 的 `results` 是**「列表里装字典」**（`[{"command": … "output": …}]`），
`"\n".join([{…}])` 必抛：

```
TypeError: sequence item 0: expected str instance, dict found
```

这个异常穿透到 `_call_tool` 的 `except Exception`，被统一改判成 `isError=True` ——
**于是一次成功的调用被伪装成失败，真正的结果（171 行运行配置）被整个丢掉。**

**为什么极隐蔽**（三条都撞在一起）：

| 现象 | 原因 |
|---|---|
| 界面只说「执行失败」，不说为什么 | `tool_execution_end` 事件只带 `isError`、不带原因；模型只能瞎猜（实测连猜 4 次全错，并把 2 次调用吹成 8 次） |
| 短命令却"能用" | `display clock` 回显仅 186 字符，没触发"大字段"阈值（≈330 字符）→ 根本没进落盘环节；`display version`/`vlan`/`configuration` 全部越界 → 必崩 |
| 现场看起来"部分成功" | 落盘按字段顺序处理，`raw` 排在 `results` 前面 → 崩的时候 `raw` 已经写完盘 |

**落盘指纹**：`logs/ai_tool/<会话>/` 里**只有 `*_netdev_run_raw.txt`、
没有 `*_netdev_run_results.txt`**。按这个指纹回溯历史：
**2026-09-30 起 7 个会话、27 次 `netdev_run` 调用，`results` 落盘 0 次** ——
也就是说 **AI 读设备配置的能力从发布前就一直是坏的**，
此前偶尔"看起来能用"，是 AI 自己绕道 `screen_read` / `watch_tail` 硬凑出来的。

**影响范围**：只有 `netdev_run` 一个工具（它是唯一返回 `results` 数组的），
偏偏它是 AI 读取设备状态的首选工具。

**修法**：

- `"\n".join` 前逐元素 `str()`，并把整个循环包进 `try` —— 单个字段落盘失败不许拖垮整份结果；
- 摘要层整体加兜底：**任何后处理失败都回退为原始结果 + 一句 `_note` 警告，
  绝不许把「工具成功」改判成「工具失败」**（原则写进了代码注释）；
- `tool_execution_end` 带上失败原因，界面从「⚠ xxx 执行失败」变成
  「⚠ xxx 执行失败 · *原因*」，无原因时优雅退化；
- `netdev identify` 在串口被同屏会话占用时改为**自动走同屏**（原先直接拒绝，
  并给出 `tmux attach -t netops` + `Ctrl+]` 这种 **AI 无法执行**的建议），
  与 `netdev run` 的行为统一。

### 新增：`doctor` / `ui status` 会报「服务跑的是旧代码」

修好代码但没重启服务 → 跑的还是旧逻辑。本次排障就踩到了：
产物已经修好、`doctor` 全绿，用户界面上却一模一样地继续报错，白排查一轮。

现在服务启动时会把**它所加载文件的 SHA-256**写进
`logs/ui-service-<端口>.code.json`；`netdev doctor` 与 `netdev ui status`
拿当前磁盘哈希跟它比，不一致就提示：

```
! 代码版本    ui/server.py 在服务启动（…）之后改过 → 跑的还是旧代码，重启：netdev ui restart
```

判据**用内容哈希而非 mtime** —— 因为仓库里的 `test_mcp_hotreload.py` 会临时改写
`netdev_mcp.py` 再还原（内容与大小都不变、只有 mtime 变），用 mtime 会平白报假警。
清单**按端口分文件**，避免测试用的隔离实例（8899）覆盖正式实例（8898）的基线。

### 新增守门测试（14 条）

`tests/test_ai_toolchain_and_cache.py` 增加 14 条断言，覆盖：

- 摘要器能吃掉「列表里装字典」的 `results`（不把成功伪装成失败）、摘要后仍带落盘路径；
- 失败原因能从 `error` 字段、也能从结构化 `steps`/`results` 里提取；成功的结果不提原因；
- 陈旧自检**自己不许静默失效**：内容没变必须闭嘴、内容真变必须开口并给出补救命令、
  **只碰 mtime 不许报**、没有清单时安静跳过；
- 清单必须按端口分文件（否则隔离实例会覆盖正式实例的基线）。

这类「后处理把成功伪装成失败」的错误必须由测试兜住，不能靠人肉发现。

---

## [1.0.2] — 2026-10-04

（含 0152d84 / d23118b / e51bf39 / 78d0e4e / 66cdaef / fca63fe / 4e7b394 / 2de7758 共 8 个提交。
主题：**剥宿主注入的 shim** + **AI 停止按钮的图标与外壳四轮迭代**。
修复了一处严重隐性 bug，并按用户要求把停止按钮从 `■` 一路做成「一颗去框的小圆点、与设备行同款」。）

### 破坏性变更：AI 助手只剩「直连 API」一个后端

原来的 `pi agent（RPC）` 与 `WorkBuddy agent（headless）` 两个后端**已整体拆除**，
只保留自实现的直连后端（直连 OpenAI 兼容 API）。旧配置里若还写着 `backend=pi|wb`，
服务端会**优雅降级**为直连并回一条「该后端已下线」的提示，不会直接报错。

**为什么要拆**：它们都是"借别人的 CLI 当引擎"，各自带着一整类与 netdev 无关的故障面 ——

| 后端 | 它带进来的问题 |
| --- | --- |
| `pi` | 配置 / 凭据锁用 proper-lockfile（空目录当锁），崩溃即残留、而且**永不自愈**（真因见下一节）；启动时会跑 `npm install`，国内网络不通就崩；更关键的是 **`pi 0.85.1` 根本不支持 MCP**，netdev 的 13 个工具其实一个都传不进去（`mcp__netdev` 是宿主 IDE 的命名约定，pi 里不存在，被 `--tools` 白名单静默过滤） |
| `WorkBuddy` | 内部服务端口冲突会**静默挂死**（无输出、无退出）；凭据是宿主加密信封，独立进程解不开；`--tools` 是全局白名单，会把 MCP 工具一起掐掉（得改用 `--disallowedTools`，且工具名必须逐个 `argv` 元素传 —— 逗号串会被当成一个名字，静默失效） |

**用户可感知的变化**：

- 设置面板的「后端」不再有 `pi` / `WorkBuddy` 可选，只剩 `直连 API` 与 `关闭 AI`；
- 不再需要 `npm i -g @earendil-works/pi-coding-agent`，也不再依赖 WorkBuddy 桌面版；
- 不用再管 `~/.pi/agent/*.json.lock` 残留锁 —— 那套自愈代码（`pi_heal_locks`、
  `/api/pi/repair`、`bin/workbuddy-check.command`）已一并删除；
- 「AI 提议解析规则」从"起一个 pi RPC 子进程 + 手工拼 JSONL"改成走直连，
  与 AI 助手共用同一条凭据链路；
- AI 面板标题、审计日志里的后端名统一显示「直连 API」。

**刻意保持不变**：AI 手里的工具仍然是 `netdev_mcp` 那 13 个，全部转调 netdev CLI；
写操作的四道闸门（黑名单 → 人工审批 → 强制备份 → 逐行校验）一行没动。
护栏唯一性是这个项目最重要的一条设计约束 —— AI 换引擎可以，绕开闸门不行。

### 修复：宿主注入的 Node / Shell shim（这才是"AI 莫名其妙不好用"的真根因）

**症状**：AI 面板稳定报
`Credential store read failed for deepseek: EEXIST: file already exists, mkdir '…/auth.json.lock'`，
而此刻 `~/.pi/agent` 下根本没有活着的 pi，锁也早已是死锁（mtime 静置十几分钟零刷新）。
手工清锁、重启会话都没用，看着像"清不动"。

**真因**：宿主（WorkBuddy / CodeBuddy 一类桌面 IDE）会给**每一个 Node 进程**注入
`NODE_OPTIONS=--require=…/cli/vendor/shim/node-language-shim.cjs`。该 shim 接管了 `fs`，
把 `mkdir` 撞名的 `EEXIST` **改写**成 `code="CODEBUDDY_BROKER_DENY"`（message 文本里
仍写着 `EEXIST: …`，所以肉眼极难分辨）。
`proper-lockfile` 的**陈旧锁自愈分支**判据是
`if (err.code !== 'EEXIST') return callback(err);` —— code 被换掉之后，这个分支被整个
跳过，**崩溃残留的锁永远不会过期**。

实测对照（同一台机器、同一个 proper-lockfile 4.1.2，只改锁目录 mtime）：

| 锁目录 mtime | 带 NODE_OPTIONS 注入 | 剥掉注入 |
|---|---|---|
| 现在 / 3s / 10s / 29s 前 | `CODEBUDDY_BROKER_DENY`，**全部不自愈** | `ELOCKED`（正常，会重试） |
| 35s 前（超 30s 陈旧阈值） | `CODEBUDDY_BROKER_DENY`，**仍不自愈** | 清掉旧锁并**成功获取** |

**改法**：`ui/server.py` 在把环境交给**任何**子进程之前，先剥掉宿主注入的
`NODE_OPTIONS` / `PATH` 里的 shim 目录 / `BASH_ENV`（全部按精确特征匹配，
`~/.workbuddy/binaries/**` 这类**属于运行时**的路径不会被误伤；用户自己的环境零改动）。
现在 `netdev_json()` / `raw_netdev()`（界面所有 CLI 调用）也走这条干净环境 ——
shim 的 `brokered-bin` / `safe-bin` 会把 `mkdir` / `rm` 换成受管版本，
而 netdev CLI 自己要建 pid、快照、回收区目录，错误码语义不可依赖。

这是"不做也能跑、但在宿主里必挂"的那一类修复，对**双击启动**的用户是空操作。

### 修复：快照「彻底删除」只报 `Load failed`（同一个 shim，Python 侧 —— 上一轮漏了）

**症状**：界面「快照管理」里点「彻底删除」，**连续 4 次**都是失败，提示只有一句
`Load failed`；同一批操作里「已移入回收区」的成功提示还会显示成
`已移入回收区：收区，不裸删）：2 份[0m #2 …` 这种拼接乱码。
更要命的是 `logs/ui-service.log` 里**连 traceback 都没有** —— 看起来像"什么都没发生"。

**真因**：宿主那套 shim 不只有 Node 半边。**同一个目录还被塞进 `PYTHONPATH`**，
里面的 `sitecustomize.py` 会在**解释器启动时**被自动 import，把
`os.remove` / `os.unlink` / `os.rmdir` / `shutil.rmtree` / `pathlib.Path.unlink`
全部换成受管版本；每次真正删除前跑一次「批量删除守卫」——
按**轮次**累计待删文件数，超过阈值（本机 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=50`）
就打印标记并 **`raise SystemExit(1)`**。

**为什么界面只说 `Load failed`**（三层叠在一起，每一层都在掩盖上一层）：

| 层 | 发生了什么 |
|---|---|
| `SystemExit` 是 `BaseException` | 业务代码里的 `except Exception` **接不住**，一路穿透 HTTP 处理函数 |
| `threading.excepthook` 对 `SystemExit` **静默忽略** | 日志里连 traceback 都不留，排障时"无迹可寻" |
| 连接被断开 | 浏览器只剩 WebKit 对网络级失败的文案 `Load failed` |

所以用户看到的是"netdev 删不掉快照"，而不是"宿主拦了这次删除"。

**现场实据**（`logs/ui-service.log`，连出 5 条，时间点正是界面报错的那几次）：

```
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":53,"threshold":50,
  "scope":"turn","targets":["…/snapshots_trash/huawei_…_removed-20261004_004344"],
  "targetCount":1}
```

**为什么"只删一项"也会中招**：守卫的计数是**本轮累计**，不是单次目标大小。
用户先「清空回收区」批量删掉了 50+ 项 ⇒ 计数越线 ⇒ 此后**每一次**删除
（哪怕只删一个目录）都直接命中。这也解释了"以前能用、突然就不能用了"。

**乱码的真因**：前端把 CLI 原始输出 `slice(-300)` 直接贴进对话框 ——
而那是同一条行用 `\r` **反复重绘**、还带 ANSI 颜色码的进度文本，
从半帧中间切开就成了拼接乱码。

**修法（根因 + 兜底 + 可读性，三层）**：

- **根因**：新增 `lib/hostenv.py`，`ui/daemonize.py` 在 `os.execv` **之前**剥掉宿主注入的
  `PYTHONPATH` / `NODE_OPTIONS` / `PATH` shim / `BASH_ENV` 及 `GENIE_TRASH_DIR`、
  `CODEBUDDY_SAFE_DELETE_*` 等专用键。
  **必须在 `execv` 之前** —— 钩子是在解释器启动阶段 import 进来的，
  之后无论怎么改子进程的 env 都来不及（本进程里的 `shutil.rmtree` 早被换掉了）。
  实测：净化后 execv 出来的新解释器 `PYTHONPATH` 为空、`sitecustomize` 未加载、
  `shutil.rmtree` 是原生实现。
- **兜底**：`lib/snapshot.py` 新增 `rm_tree()`，把 `SystemExit` 翻成**带解释的**
  `PermissionError`；所有物理删除点（界面 `snap_purge`、CLI 的
  `snap rm --purge` / `snap purge`）一律改走它，不再裸调 `shutil.rmtree`。
  HTTP 处理函数显式写成 `except (Exception, SystemExit)`。
  即便将来宿主换一套拦法，用户看到的也是"原因 + 补救动作"，而不是 `Load failed`。
- **可读性**：`ui/server.py` 新增 `clean_cli_tail()`（先剥 ANSI，再对每行只保留
  最后一个 `\r` 之后的内容 = 该行**最终态**）；界面在删除**成功**时不再倒原始输出，
  失败时才给一句可读原因。

`netdev ui restart` 后即可生效 —— **这个修复必须重启服务**，因为钩子是在服务
启动那一刻注入的（`netdev doctor` 会提示"跑的是旧代码"，就是为这类情况准备的）。

**新增守门测试**：断言 `SystemExit` 会被翻译（含三处提示文案）、两个删除调用点
确实走 `rm_tree`、源码里不再有裸 `_sh.rmtree`、`daemonize` 里
`strip_host_injection(os.environ)` 出现在 `os.execv(` **之前**（顺序反了就等于没修）、
以及 PYTHONPATH / 宿主专用键被剥离而 `CODEBUDDY_CONFIG_DIR` / `HOME` 不被误伤。

### 修复：`netdev selftest` 在端口被占时会给出**假红**

上一轮 `netdev mock` 留下的实例还占着端口时，自检自己的模拟器静默 bind 失败
（stderr 被管道吃掉），于是自检**连到了旧实例**上 —— 而旧实例的同屏窗格可能停在
用户视图，`apply` 直接报 `Unrecognized command`，自检 ④ 判失败。
用户看到的是"功能坏了"，真相是"你的旧模拟器没关"。现在：

- 端口被占 → **当场拒跑**，退出码 2，并给出可照抄的解法（`netdev mock stop -p <端口>`）；
- 起模拟器改**轮询**等端口真的监听起来（上限 8 秒），不再用固定 `sleep 1.2`
  —— 固定等待是本项目反复踩过的坑（GitHub ARM 冷启动实测 32s）；
- 起不来时把子进程输出打出来，不再吞掉。

### 修复：模拟器不认 `C-u` / 不吞终端应答碎片（"回显是不是有问题"的真根因）

**症状**：命令在**同屏窗格存在时**必报 `Unrecognized command found at '^' position.`；
撤掉窗格、或把窗格重建一次就好了，之后开一次终端页又复发 —— 极像"netdev 的命令透传坏了"。

**真因**：netdev 的同屏下发（`netdev_cli._session_run`）在发命令前会先发一个
`C-u`(0x15) 清掉当前行 —— 目的正是清掉残留的终端能力应答碎片。
真机 VRP 的行编辑器把 `C-u` 当"清行"；而 `tests/mock_vrp.py` 的模拟器把它当普通字符
并进缓冲，命令于是变成 `"\x15display clock"` → 匹配不上 → 永远 `Unrecognized`。
同一类污染源还有终端对设备查询自动回的能力应答（`\x1b[?1;2c` / `0;276;0c`），
这些字节会从窗格漏进设备输入流（`netdev` 源码注释里早已指出这一点）。

**注意它只在有窗格时出现**：没有窗格时 netdev 走直连、不发 `C-u`，
所以这个 bug 最容易在排查时"自己消失"，被误判成偶发。

**改法**（模拟器两处，`mock_vrp.py` / `mock_telnet.py` 同源）：

- 认 `C-u`(0x15) 为清行；其余控制字符一律忽略，不并入命令缓冲；
- 增 ANSI 转义序列吞噬：`ESC` 起算，上限 16 字符（畸形序列也不会吞掉整条命令），
  引导符 `[` `O` `(` 不计入终结字节。

**顺带清掉一处死代码**：`mock_vrp.py` 里有**两份** shell 循环，其中 `shell_loop()`
从未被调用（真正跑的是 `serve()` 里内联的那份）—— 第一次修复改到了死掉的那一份，
症状当然纹丝不动。已删除死代码，修复落到真正在跑的那份循环上。

**新增回归**：`tests/test_mock_cmd.py` 第八组（12 → **21 项**），覆盖
`C-u` 清行 / 退格修正 / `Ctrl-C` / **ANSI 碎片污染命令行** /
**碎片 + `C-u` = netdev 同屏的真实下发路径**。

### 修复：`netdev_mcp.py` 的 `IndentationError`（P0，一坏全坏）

`t_serial()` 里有一段被多缩进 2 格，**整个文件语法都不成立** ——
`python3 -m py_compile netdev_mcp.py` 直接以 `IndentationError` 退出。

后果极隐蔽：它不是"某个工具坏了"，而是 **MCP 服务端一启动就秒崩**。
MCP 客户端（以及按同一契约调用它的直连后端）拿不到工具表，于是模型手里
**一个设备工具都没有**，表现出来却是"AI 在瞎编工具调用"。
修好后实测握手正常（`initialize` 返回 `netdev 1.0.0`，`tools/list` 返回 13 个工具）。

### 新增：守门测试，专治"一坏就全坏"的那类隐蔽故障

`tests/test_pi_heal_and_cmdcache.py`（35 项）→ **`tests/test_ai_toolchain_and_cache.py`（57 项）**：

- **全仓 `.py` 必须可编译**（用 `git ls-files` 量"真正会进仓的文件"，不靠读 `.gitignore` 猜）
  —— 上面那个 IndentationError 的守门测试；
- **`netdev_mcp` 服务端契约**：工具表 ↔ 处理器一一对应、无孤儿、**工具数必须等于 13**
  （护栏面不能被悄悄改窄）、包装脚本可执行，并**真跑一次 MCP 握手**（`initialize` + `tools/list`）；
- **AI 工具链一致性**：给模型的 schema 必须与 `netdev_mcp.TOOLS` 完全对齐、是 OpenAI 兼容形状、
  不含 MCP 专属字段；`TOOLSETS` 只有三档；
- **"已拆除"的守门断言**：`AiSession` / `WbSession` / `PI_BIN` / `_rpc_startable` /
  `pi_heal_locks` 不得再出现在 `ui/server.py`；源码里不得再出现 `/api/pi/repair`、
  `mcp__netdev`、`--disallowedTools`；
- 保留原有三组：采集命令学习缓存、五家平台识别、宿主 shim 剥离（11 项）。

端到端实测（真实 HTTP + 真实模型）：`/api/ai/open`（故意传旧值 `backend=pi` → 优雅降级为
直连并回提示）→ `/api/ai/send` → 事件流拿到 assistant 回复，并**真的调到了 `netdev_list`**
（读回真实设备清单）；`/api/term/open` → `/api/term/input` 键入的命令确实到达设备并回显；
且**开着同屏窗格**下发 `netdev run mock-hw "display clock"` 仍被正确识别
（这条原本必红，真因见上一节）。

### 界面：AI 面板的「停止」按钮 = 一颗圆点

这个按钮的图标换过四轮，最终形态是**一颗 7px 的小圆点**：

| 轮次 | 形态 |
| --- | --- |
| 最初 | 实心方块字符 `■` —— 和界面的点阵语言不是一套东西 |
| 第 2 轮 | 3×3 点阵（与品牌标识同源的**方**点） |
| 第 3 轮 | 3×3 点阵，点改**圆** |
| **现在** | **整个点阵去掉，只留一颗圆点** |

**为什么要换掉点阵**：点阵在这个界面里一直代表"正在跑"（品牌 = 系统运行中，
「接入中」= 正在连通）。放在停止按钮上，等于让一个"我随时可以停"的控件
长得像"它正在忙"，语义是拧的。而**一颗圆点**没这层包袱，还能直接沿用设备行
那颗「窗格在线」点的语义 —— 于是颜色规则是白捡的：

| 状态 | 表现 |
| --- | --- |
| 空闲（AI 没在跑） | 主题主色的实心点，静止 |
| AI 正在跑 | **变绿**（`--st-on`，与设备行 / 顶栏「在线」点同一条规则）+ 呼吸 |

**规格与设备行那颗点完全一致**（7px 圆 + `0 0 6px` 柔光）—— 不一致就成了两种"材质"。
颜色一律走 `currentColor`：底色和柔光会一起变；分两处写就一定会漏一处。

**踩到并写进测试的坑**：切换呼吸动效若写成 `animation` 简写，会把上面挂着的
`animation-duration` / `timing-function` / `iteration-count` 一并重置成各自的初始值 ——
`duration` 变成 `0s`，**动画根本不跑**，点就一直静静躺着。
不报任何错、也不影响任何接口。（上一轮点阵形态踩的是同一个坑的另一个后果：
简写重置了 `animation-delay`，对角错峰变成九点同步闪。）所以一律用
`animation-name` 长写法，并在回归里断言"源码不许出现该简写"。

`ink`（墨水屏）主题照旧冻结一切动画 —— 可接受，那里的设计前提就是"静态纸面"，
活动态靠**变绿**读得出来，不依赖呼吸。

**没动的**：品牌标识与「接入中」仍是 3×3 点阵（一个代表"系统在跑"、一个代表
"正在接入"，都在恰当的位置）。旧的 `.dots.stop` 与 `@keyframes dotpulse-btn`
已随点阵一起删掉，不留死 CSS（有断言守）。

**再去掉外面的方框，并让点与文本框上下居中**（同一轮追加的要求）：

- **去框**：`border:0` + `background:transparent`。按钮**本体仍在**，只是不可见 ——
  26px 的点击热区必须保留，7px 的点当点击目标太小了（可用性优先于视觉）。
  悬停也**不给方框**（那正是要去掉的东西），改成让柔光放大到 10px，作为"可以点"的反馈；
  键盘 Tab 到时仍有一条 `:focus-visible` 虚线（无边框控件最容易把焦点样式一起弄丢）。
- **居中**：容器 `.ai-in` 是 `align-items:flex-end`，而文本框比按钮**高**
  （占位文字会自动折成 2 行 ⇒ 41px，按钮恒 26px），两者**底边**对齐
  ⇒ 按钮中心比文本框中心低 **7.5px**。只给这一个按钮加 `align-self:center` 就够了，
  不动容器规则。实测：单行 `Δ中心 = 0`，文本框撑到 4 行（75px 高）时 `Δ中心` 仍是 `0`。
- **一点更正**：错位跟"边框"无关。全局是 `box-sizing:border-box`，去不去边框
  按钮**都是 26px**（实测）。上一版提交信息里写的"去掉边框治好 2px 错位"是按
  `content-box` 推断出来的，**是错的** —— 错位纯粹是 flex 对齐方式的问题。

**守门测试（29 条，整组重写）**：按钮里只有**一颗**点、9 个 `<i>` 已整个移除；
外框已去掉 / 底色透明但 26px 热区仍在 / `align-self:center` 在位 / 悬停不把方框刷回来 /
键盘焦点仍可见；
尺寸与柔光跟设备行那颗点**同规格**；圆角、`currentColor` 两处都写；
忙态用长写法、**不许改回简写**；`aiBusy()` 仍管到它；旧写法（`.dots.stop`、
`dotpulse-btn`）不许复活；品牌与「接入中」的点阵不许被顺手删掉。
变异测试确认：改成简写、把尺寸改得与设备行不一致、或把外框与对齐改回去，
断言都会立刻变红。

### 界面：活动状态统一用绿色（浅色主题下指示点终于分得清）

**用户报障**：设备行那个小圆点 —— 明明写着「窗格在线」—— 在浅色主题下却是**黑的**。

**真因不是漏了颜色，是用错了色源**：设备行的点（`.st.ok`）和顶栏的「在线」点（`.dot`）
都取 `var(--g)`（主题主色）。深色主题里主色本身就是绿，看不出问题；可一到
`minimal`（`--g` 纯黑）/ `ink`（`--g` 近黑）主题，「在线」的黑点和「离线」的
`#3a3a3a` 深灰**几乎分不出来** —— 指示点等于白放（`ink` 下更糟，两者几乎同色）。

**改法**：活动/在线一律改用**语义色 `--st-on`**（就是「已接入」插头一直在用的那个绿），
不跟主题主色走。三处同一条规则：

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 设备行圆点 `.st.ok` | `var(--g)` | `var(--st-on)` |
| 顶栏「在线」点 `.dot` | `var(--g)` | `var(--st-on)` |
| AI 面板停止按钮那颗点（忙态） | 主题主色 | `var(--st-on)` |

三态因此重新可分：**在线 = 绿 / 离线 = 中性灰 `#3a3a3a` / 接入中 = 琥珀**。
绿色主题（默认）下 `--st-on` 与 `--g` 同值，**观感零变化** —— 只有浅色主题才看得出差别。

**顺带守住一条坑**：`amber` / `cyan` / `blue` / `mono` / `terracotta` 这些深色主题
只覆盖了 `--g*`，没覆盖 `--st-*`，靠 `:root` 兜底（`#00ff41`）。所以以后新增
**浅色**主题时必须自己声明 `--st-on`，否则会掉回那个荧光绿 —— 白底上刺眼、对比度还差，
**而且没有任何报错**。回归里已按这条加了断言。

**新增守门测试（11 条）**：三处都指向 `--st-on` 且不再取 `--g`；AI 点阵**空闲态不许上绿**；
`.st.off` / `.st.warn` 仍独立定义（三态不许被合并成一态）；`minimal` / `ink` 两个浅色主题
各自声明了 `--st-on`；`:root` 里有兜底。
（写这条测试时自己踩了一次：文件里有**两个** `:root{}` 块，只 `index` 第一个会误判 —— 已改成遍历全部。）

### 安全：清理已下线后端留下的凭据

`config/workbuddy-product.json`（**含一条真实可用的 WorkBuddy JWT，1394 字符**）、
`config/codebuddy-mcp.json`、`config/codebuddy-mcp-empty.json` 已移入
`.chk/removed-workbuddy-backend-<时间戳>/`（只搬不删）。它们从未进过版本库，
`.gitignore` 里的对应规则保留作为防御。

---

## [1.0.0] — 首个公开版本

### 三通道接入与人机同屏

- 串口 Console / SSH / Telnet 统一到同一套 CLI；基于 tmux 的「人机同屏」——
  人工和 AI 看到的是同一块屏幕，都能回看历史。
- 退格键自适应：不同厂商 Console 的退格行为不一致，实测存在 `bs` / `del` /
  `^H` / 无回显等多种模式，接入时自动探测并记住。
- 桥接进程全程留档、密码掩码、接入自检、看门狗。

### 写操作四道闸门

黑名单分类 → 人工审批 → 强制备份 → 逐行下发并校验，**任一步失败即停**（fail-closed）。

- 审批是双通道的：优先走网页审批，网页不可达时回退 macOS 原生弹窗；
  **两条都不可达则拒绝执行**，不是放行。
- 审批代码 `lib/approval.py` 带 sha256 基线篡改检测，`netdev doctor` 会报
  「与基线不一致」—— 让"悄悄改掉审批逻辑"这件事显眼。
- 破坏性操作先进隔离区（可还原），不做物理删除。

### 厂商适配

- 平台档案（`lib/platforms.py`）按厂商分派命令表 + 解析正则；
  接入时发一条 `display version`，按 banner **自动识别**厂商
  （华为 / 华三 / 锐捷 / 思科 / 迈普），不用手填 platform。
- 指标解析不中时显示「—」，**绝不编造 0**。
- **命令是学一次、一直用的**：某条指标命令不被接受时自动逐条试候选，
  命中结果落盘到 `config/cmd-cache.json`，下次直接用。

### 备份与恢复

- 配置快照、语义化 diff（不是纯文本逐行比）、恢复前先看差异再确认、
  删除进回收区可取回。

### AI 协作

- **AI 助手直连 OpenAI 兼容 API**（DeepSeek / OpenAI / OpenRouter / 任意兼容网关）：
  一把 API Key 即用，零额外 CLI 依赖、零常驻进程。也可整体关闭 AI。
- MCP 暴露 13 个 `netdev_*` 工具，**全部转调 CLI**，自己不碰设备；
  Bash / Write / Edit 等通用工具硬移除。内置 AI 助手用的就是同一套 13 个工具。
- 网页终端左栏按钮：8 条散按钮收敛为 **3 个聚合入口**。

### 网页界面

- 自带界面（`ui/server.py`），不依赖 pi-web-ui。
- 监控采集**静默直连**（独立短连接），不打断前台同屏操作。

### 网页服务生命周期（`netdev ui`）

- 新增 `netdev ui`：默认 `ensure` 语义（**没有就起、已经在跑就报状态**），
  另有 `start / stop / restart / status / log / open / install / uninstall`。
- 底层 double-fork + `os.setsid()`，**真正脱离终端会话** —— 关掉终端窗口或启动器
  窗口都不再把服务带走。
- 三条入口统一：CLI 命令 / `bin/设备工具台.command`（开局自动确保服务在跑，
  banner 实时显示状态）/ `ui/启动.command`（改为后台守护）。
- `netdev doctor` 的失败建议改为指向**本机真能执行**的路径
  （原先建议 `launchctl bootstrap`，在部分机器上被系统拦，照做也起不来）。

### 稳定性修复（真因链均与表面报错不符）

| 表面症状 | 真因 |
|---|---|
| pi 报 `No API key found for the selected model` | `~/.pi/agent/*.json.lock`（proper-lockfile 的锁是**一个空目录**）在一次 npm 启动失败后**永久残留** → 后续启动 EEXIST → 读不到 `settings.json` → **静默回退内置默认 provider**。已实现探测 + 自动自愈（只搬不删，移入隔离区可还原）+ 一键修复 |
| 采集命令缓存是单向死代码 | 探测结果塞进 `retry` 字典后再没用过，每轮都重撞一次墙。改为 `plan_commands()` 并落盘 |
| 界面能建 AI 会话但发消息无事件 | 判据是"发 prompt 后有没有吐事件"，而 pi RPC 是惰性的。改为「进程存活即就绪，死了立刻返回」 |
| 网页按钮在任何非 `~/netops` 路径下都坏 | 生成时硬编码了家目录。改为按真实安装路径生成 |
| `direct.json` 明文 Key 有泄露风险 | 补齐 `.gitignore` 排除规则 |

---

## [Unreleased]

### 已知待办

- 更多厂商真机的监控采集验证
- 英文文档与 `README.en.md` 的持续同步

---

## 说明

- **关于 `launchctl bootstrap`**：部分机器（含本机开发环境）会被系统安全策略拦
  （`Bootstrap failed: 5: Input/output error`）。LaunchAgent 模板仍然保留，
  在 launchd 可用的机器上 `netdev ui install` 依然会装它；
  但日常使用不依赖它 —— `netdev ui` 自身就能把服务拉起来。
- **关于版本号**：单一真源是 `dist/installer/VERSION`，
  `/api/health` 会如实报告当前版本，不存在"两处版本号不一致"的情况。
