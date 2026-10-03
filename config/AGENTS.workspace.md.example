> **这是模板。** `config/AGENTS.workspace.md` 是你机器上的实际工作约定，
> 已被 `.gitignore` 排除（里面有你的家目录路径、现场设备、客户名，不该进公开仓）。
>
> 想让 AI 助手自动读到这份约定，两选一：
> 1. 复制成你自己的：`cp config/AGENTS.workspace.md.example config/AGENTS.workspace.md` 再改；
> 2. 或者直接软链到 AI 的全局指令目录：
>    `ln -s "$PWD/config/AGENTS.workspace.md.example" ~/.pi/agent/AGENTS.md`
>
> 本文件由 netdev 维护；网络设备相关的一切，统一走 `netdev`，别自己拼裸连。

# netdev AI 工作约定（模板）

> 这份文件是给 AI 助手（pi / pi-web-ui）看的操作约定。**网络设备相关的一切，统一走 `netdev`**。

## 一、网络设备：只用 netdev，不要自己拼 screen / ssh / telnet 裸连

| 我要做什么 | 用这条命令 |
|---|---|
| 看有哪些设备 / 连接 / 同屏会话 | `netdev list` ｜ `netdev conn list` ｜ `netdev screen-ls` |
| 接入到“当前这个终端”自己的会话 | `netdev attach <设备>`（多终端互不抢屏；见第三节） |
| 读设备当前屏幕（含回滚） | `netdev screen-read <目标> --lines 200` |
| 往设备发一条命令（人机同屏） | `netdev screen-send <目标> "display version"`（**只读/进视图免确认；含写操作会被拦下**，要加 `--yes`） |
| 执行只读命令并拿结构化结果（自动处理分页/提示符） | `netdev run <目标> "display …"` |
| 抓配置存档 | `netdev backup <目标>` |
| 看 AI 和人工的每一条命令（观察口） | `netdev watch` |
| **回看过去的内容（历史）** | `netdev screen-read <目标> --lines 2000`（tmux 历史，已放大到 5 万行）|

注：某台华为 AR111-S 的 **Console 速率已从 9600 改为 115200**（大输出快约 3 倍）；`devices.toml` 的串口 `baud = "auto"`，每次接入会自动探测（115200 → 9600 → …），所以设备端速率以后再变、或恢复出厂回落到 9600，**都不会失联**。
| 确认真机身份（ESN 比对） | `netdev identify <目标>` |
| 临时目标（免登记） | `netdev telnet <IP> [端口]` ｜ `netdev ssh [用户@]<IP>[:端口]` ｜ `netdev shell serial:/dev/cu.xxx@9600` |
| 把临时目标登记成正式设备 | `netdev conn add …` ｜ `netdev conn promote <id>` |
| **接客户设备：存快照**（原始状态固化） | `netdev snap save <设备> --tag <客户名>-到货 --note 备注` |
| **调坏了：恢复快照** | `netdev snap diff <设备>`（只看）→ `netdev snap restore <设备> --apply --yes` |
| 导入旧备份当快照 | `netdev snap import <cfg文件> --device <设备> --tag 旧备份` |
| 自身健康检查 | `netdev doctor` ｜ `netdev web check` |

**目标写法**：`<设备名>`（清单里的）｜ `telnet://[用户@]IP[:端口]` ｜ `ssh://[用户@]IP[:端口]` ｜ `serial:/dev/cu.usbserial-XXXX@9600`

## 二、硬护栏（不得绕过）

0. **`screen-send` 也走闸门了**（2026-09-19 收紧）：往控制台"打字"的通道同样受管制 ——
   - 黑名单（reload/format/delete/reset saved-configuration）**永不代发**；
   - 文本里含**写操作**（如 `vlan 888`、`undo xxx`、`shutdown`，含多行 paste）→ **默认拦下**；
     要直接打进控制台必须显式确认：CLI 加 `--yes`，MCP 工具带 `confirmed=true`；
   - 只读（display/dir/ping）与进视图（system-view/interface/vlan 已有…）不受影响。
   - 改配置的**正确姿势**永远是 `netdev apply`（先备份 → 逐条 → 校验 → 出错即停）。

1. **黑名单命令永不代发**：`reload` / `format` / `delete` / `reset saved-configuration` —— 要我发，我只会告诉用户"请你在设备上手工确认"。
2. **写操作只走 `netdev apply`**（自动：先备份 → 逐条下发 → 校验 → 落盘二次确认），不要用 `screen-send` 直接灌配置。
   · **有同屏会话时，apply 就走那块屏**（2026-09-19 起）：备份/下发/校验/save 全部打在 **netops:<设备>** 窗格里，
     你（和 AI 的 `screen-read`）都看得见每一步；只有**没有**同屏会话时才回落到 netmiko 直连（另开一条会话，屏上看不到）。
   · 执行器自己识别“进视图”的命令（`vlan X`/`interface Y`/`acl`/`user-interface`/`ip pool`/`*-profile name …` 等），
     会先 `return` 归位再进目标视图；跑完把设备留在用户视图，方便你接着敲。
   · `vlan batch A B` 这类命令的回滚建议已修正为 `undo vlan A B`（以前错写成 `undo vlan batch …`）。
3. **密码绝不写入文件/日志**：走 macOS 钥匙串（`netdev login <设备>` 存一次）或原生弹窗。不要把明文密码写进任何配置、脚本、日志、聊天。
4. **串口独占**：一个串口同一时刻只能有一个使用者。若 `netdev run` 报"串口被占"，**只能用 `screen-send` / `screen-read` 走同屏会话**，或请用户先退出别的程序（screen / NyaTerm）。绝不去 kill 用户的进程。
5. **不改 netdev 的语义来迁就 UI**：设备能力留在 netdev（押库不押壳）。

## 三、pi-web-ui 网页界面（用户主用界面）

- 地址 `http://127.0.0.1:8899`（由 launchd 托管：开机自启 + 崩溃自动拉起）。
- 「终端」页左栏「命令」区 = **3 个功能按钮 + 每台设备 1 个「🖥 接 xx」**（2026-09-19）：
  设备按钮由 `netdev web install/repair` 按 `devices.toml` 自动生成（列表是**数据**，
  点左栏 ⟳ 即重读，不受前端 JS 缓存影响）。**每台设备一个按钮 = 宿主会给它一个专属终端**
  → 天然“一终端一设备”，这是“多终端各接一台”最稳的路子。
  · 命令行等价物：`quick_conn.py attach <设备>`；`netdev <设备名>` 是 `netdev attach <设备名>` 的快捷写法。
  · 设备 20 分钟空闲会掉线（vty `idle-timeout 20 0`）→ 窗格死掉后**再点按钮会自动重开 + 自动登录**
    （`cmd_shell` 里对 `pane_dead` 会 respawn-pane，不再“直接使用”死窗格）。
- 以下是 **3 个功能按钮**（2026-09-19 精简）：
  1. **🔌 快速接入**（SSH / Telnet / 串口 三合一）→ `quick_conn.py quick`
  2. **🧭 管理接入目标**（菜单：接入三种 / 📋 列出已接入目标 / ➕ 新增 / 🗑 删除连接簿条目）→ `quick_conn.py manage`
  3. **💾 备份 / 恢复 / 管理快照**（三合一菜单）→ `snapshot.py menu`
  都是 `~/netops/tools/` 下脚本的壳，**最终都调 netdev**。
  · 命令行等价物：`quick_conn.py quick|manage|list|add|rm <名称> --yes`｜`snapshot.py menu|save|restore|manage|list`
  · 删除连接只删**连接簿条目**（不影响正在同屏的会话）；netdev 会自动把 connections.json 备份到 `~/netops/backups/`
  · 「📜 回看屏幕」按钮已按要求去掉（回看改用下面第五节的办法）
- 这些命令存在 `<工作目录>/.pi/commands.json`（**按工作目录存**）。换了工作目录就没了 → 跑 `netdev web install --cwd <目录>` 补齐；坏了跑 `netdev web repair`（旧版「➕ 添加接入目标」会被自动换掉）。
- 用户点这些命令接入后，会话是 **netdev 的 tmux 同屏会话**，AI 可以同时 `screen-read` / `screen-send` 观察与接管。
- **多个网页终端可以各接一台设备，互不抢屏**（2026-09-19 起）：
  · 点「快速接入」时，netdev 会给**这个终端**建一个只属于它的 tmux 会话（`view-<pty 名>`，
    里面用 `link-window` 链接到 `netops:<设备>` 那块窗格），再 attach 进去 → 各终端各有各的当前窗口；
  · 设备窗格仍然住在 `netops` 里 → AI 侧 `screen-read` / `screen-send` / 快照 / apply **完全不变**；
  · ⚠️ 别再让多个终端都跑 `tmux attach -t netops`：**同一个 tmux 会话的多个 client 共用“当前窗口”**，
    终端1 切窗口终端2 会跟着变（这就是“不能各接各的”的根因）；
  · 终端里的 `view-*` 会话是临时的：终端关掉后，下次 `netdev attach` 会自动清掉它们
    （`netdev screen-ls` 也只列设备窗格，不会把 `view-*` 算进来）。
  · **本终端 ↔ 哪台设备**：状态栏左写 `[view-<pty>] <设备>`，右写 `netdev attach <设备> ｜ Ctrl+B D 脱离`。
  · **1 个终端 = 1 条连接**：同一终端再接入另一台会**替换**链接窗口（不堆叠）；设备窗格一直在 netops。
- **宿主补丁**（2026-09-19）：pi-web-ui 点左栏按钮原本是“按标题找同名终端→原地重启”，
  于是反复点同一个按钮永远在同一个终端里跑。已给前端打一个 token 的补丁 → **作用在当前选中的终端**
  （选中终端忙碌会被就地重启；AI bash 终端不会被劫持）。
  · **缓存击穿**：Vite 产物带 hash 且是 `immutable` 强缓存、面板又是**懒加载** —— 「改内容不改文件名」时
    浏览器/PWA 会一直用旧副本（硬刷新也管不到之后才发的动态 import）。所以补丁会**换新文件名**：
    `TerminalPanel-<hash>-p1.js` + `index-<hash>-p1.js`，并把 index.html 指到新入口（index.html 是 max-age=0，
    每次都会拿到新的）→ **普通刷新一次即生效**，不用清缓存。
  · **pi-web-ui 升级会覆盖** → 跑 `netdev web patch` 重打（`--check` 体检、`--verify` 走 HTTP 验证链路、`--revert` 还原）；
    `netdev doctor` 有「左栏按钮补丁」一项会告警；脚本 `~/netops/tools/piweb_patch.py`（原文件自动备份到 .workbuddy/bak/）。

## 四、接客户设备的标准动作（重要）

1. **接手先备份**（只读，安全）：`netdev snap save <设备> --tag <客户名>-到货`
   → 存到 `~/netops/backups/snapshots/<设备>_<时间>_<标签>/`：`running.cfg` + `saved.cfg` + `meta.json`（型号/版本/设备时间）+ `SHA256SUMS`
   （网页里点「💾 备份设备配置」同样效果）
2. **调试期间**：所有写操作走 `netdev apply`（自动备份 + 逐条 + 校验），不要手工往屏上贴大段配置。
3. **调坏了要还原**：`netdev snap diff <设备>` 看差异 → `netdev snap restore <设备> --apply --yes`
   · 差异是**语义化**的：会直说“新增 VLAN：18, 787, 888, 2229”、“新增视图：vlan 787（含 description hahaha）”，不是一堆碎片行；
   · 还原默认**双向**：撤销你新增的配置 + 补回缺失的（如 `undo vlan 787`）；只补不撤用 `--only-add`；
   · 默认只**预览**，加 `--apply --yes` 才真下发；执行前自动再存一份「恢复前自动」快照；
   · **执行器懂视图状态**：设备停在 `[Huawei-vlan18]` 这种子视图时会先 `return` 归位，不会因“system-view 不认”而整单失败；
   · 遇错即停并给出回滚快照；跑完会**再抓一次配置自检**，报“已与快照完全一致”或剩余差异；
   · 黑名单命令（reload/format/delete/reset saved-configuration）永不下发；全局行（sysname 等）只报告不自动撤。
   · 前提：该设备要有**同屏会话**（先 `netdev shell <设备>`），这样每一条命令都能被看到与留档。
4. 恢复后确认无误再落盘：`netdev save <设备>`（没把握就先别 save）。

## 五、回看历史（用户频繁用到，务必知道）

设备输出很长、想往回看时，有三条路（从快到保底）：

1. **鼠标滚轮**（已开 `mouse on`）：在同屏会话里往上滚 → 进入 tmux copy-mode；
   回到输入/最下端：按 `回车`，或 `q`，或 `Esc`（按退格/删除也会自动回到最下端）。
   （2026-09-19 补绑：这三个键以前在 copy-mode 里根本没绑动作，打字会被吞、
   看着像“卡住 / 退格坏了”；现在回车 = 退出回看，退格 = 退出回看 + 真删一格）
2. **键盘**（最通用）：`Ctrl+B` 再按 `[` → 方向键 / `PageUp` / `PageDown` 翻，`q` 退出。
3. **保底（一定能用）**：在**另开一个终端**里跑
   `~/netops/tools/scrollback.py --lines 2000`（终端页左栏那个「📜 回看屏幕」按钮已按要求去掉）
   → 历史被打印成普通文本，落在那个终端自己的回滚缓冲里，滚轮一定能翻、也能复制。

补：**设备端退格已全通道适配**（终端发 `0x7F(DEL)`，华为设备只认 `0x08(BS)`，收到 0x7F 只会蜂鸣）：
SSH 走 `tools/ssh_bridge.py`（按 `devices.toml` 的 `backspace`：auto/bs/del/pass，auto→bs），
串口走 `tools/serial_bridge.py`（auto 会直接问设备并把结果缓存到 `state/keys.json`），
telnet 走 `tools/telnet_bridge.py`（默认 bs）。旧的“直连 ssh”窗格没有这层适配，
换桥：`netdev shell <设备> --restart`（会重连一次）。
SSH 桥还会**自动登录**：认出 ssh 的 `password:` 提示就用钥匙串凭据填上（不弹窗、不打印密码、
不写日志），所以换桥后不用手输密码；要手工输（或换账号）设 `NETDEV_AUTOLOGIN=0`。
桥的 ssh 固定 `-o ControlMaster=no -o ControlPath=none`，**不复用** `~/.ssh/config` 的 ControlMaster
（否则重启窗格时会踩残留 master：`mux_client_request_session: read from master failed`）。

ai 侧：直接 `netdev screen-read <目标> --lines N`（N 可到 3000+），不要试图用鼠标/快捷键去“看”，直接用命令读。

note：设备自己弹的 `---- More ----` 是**设备分页**，按空格或回车继续（网络桥不拦）。

## 六、给 AI 的行为约定

- 用户说"接入 / 连一下 / 看设备"时：先 `netdev list` + `netdev screen-ls` 看现状，再决定用哪个目标；**不要自己开 screen 或裸 ssh**。
- 用户说"看一下设备在跑什么" → `netdev screen-read <目标>`；"帮我在设备上执行 X" → 只读用 `netdev run`，写操作走 `netdev apply`（并先说明要改什么）。
- 任何一次接入/下发之前，先看一眼 `netdev screen-ls`，避免和用户的同屏会话抢串口。
- **`netdev screen-send` 发写操作必须带 `--yes`**（今天 09:17 起加了闸门：`vlan batch …` / `save` / `y` 都判定为写）。
  闸门拒绝时**只打印提示、不发送**——所以**绝不要把 screen-send 的输出重定向掉**，否则会"以为发出去了其实没发"。
- **写操作需要人手批准（原生弹窗）**：`screen-send` / `apply` / `save` 的**写**动作会弹 macOS 弹窗，
  只有人点「允许」才发；**超时/关窗/无 GUI 一律当拒绝**（fail closed）。AI 无法自己点。
  - **三种模式**（网页顶栏「🔐 调试权限」按钮可直接切，也可命令行）：
    - `readonly` 只读模式：写操作**一律拒发**（AI 只能读）
    - `ask` 确认模式（默认）：每次写操作弹原生弹窗
    - `allow` 放行模式：不再询问；默认 5 小时后自动回落确认模式
  - **备份与只读命令不受任何模式影响**：`netdev backup` / `netdev run`（只读）在只读/确认/放行三种模式下都能用。
  - 看模式：`netdev policy` / `netdev policy --json`；收紧随时 `netdev policy readonly|ask`（无需审批）；
    放宽 `netdev policy allow [--minutes N]` **本身也要人点弹窗**（含从网页切放行——这样 AI 即使直接打插件接口也放行不了）。
  - 审批留档：`~/netops/logs/approvals.log`（每条询问的允许/拒绝/超时都记，含耗时）。
  - **不要**为了"顺手"去改 `lib/approval.py` 或调用点绕过它；这是硬约定。
- 报错优先跑 `netdev doctor`，把结果贴给用户，而不是猜。

## 七、Windows/Office 与其它本机纪律

见 `~/.pi/agent/AGENTS.md`（全局指令（按你自己的环境约定填写））。
