# 更新日志

本文件记录**用户可感知的变化**。每条都尽量写清「真因」——
本项目多数问题的报错信息都离真因很远，只记"改了什么"会让人重复踩坑。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

### 修复：AI 助手在宿主注入环境下必定起不来（真因：宿主注入的 Node shim）

**症状**：界面 AI 面板稳定报
`Credential store read failed for deepseek: EEXIST: file already exists, mkdir '…/auth.json.lock'`，
而此刻 `~/.pi/agent` 下根本没有活着的 pi，锁也早已是死锁。手工清锁、重启会话都没用，
看着像"清不动"。

**真因**：宿主（WorkBuddy/CodeBuddy 一类桌面 IDE）会给**每一个 Node 进程**注入
`NODE_OPTIONS=--require=…/cli/vendor/shim/node-language-shim.cjs`。该 shim 接管了 `fs`，
把 `mkdir` 撞名的 `EEXIST` **改写**成 `code="CODEBUDDY_BROKER_DENY"`（message 文本里
仍写着 `EEXIST: …`，所以肉眼极难分辨）。
pi 的配置/凭据锁用的是 `proper-lockfile`，它的**陈旧锁自愈分支**判据是
`if (err.code !== 'EEXIST') return callback(err);` —— code 被换掉之后，这个分支被整个
跳过，**崩溃残留的锁永远不会过期**，pi 每次启动都必挂。

实测对照（同一台机器、同一个 proper-lockfile 4.1.2，只改锁目录 mtime）：

| 锁目录 mtime | 带 NODE_OPTIONS 注入 | 剥掉注入 |
|---|---|---|
| 现在 / 3s / 10s / 29s 前 | `CODEBUDDY_BROKER_DENY`，**全部不自愈** | `ELOCKED`（正常，会重试） |
| 35s 前（超 30s 陈旧阈值） | `CODEBUDDY_BROKER_DENY`，**仍不自愈** | 清掉旧锁并**成功获取** |

**改法**：`ui/server.py` 在把环境交给 pi 子进程之前，先剥掉宿主注入的
`NODE_OPTIONS` / `PATH` 中的 shim 条目 / `BASH_ENV`（全部按精确特征匹配，
`~/.workbuddy/binaries/**` 这类运行时路径**不会**被误伤；用户自己的环境**零改动**）。
这是"不做也能跑、但在宿主里必挂"的那一类修复，对**双击启动**的用户是空操作。

### 修复：`netdev selftest` 在端口被占时会给出**假红**

上一轮 `netdev mock` 留下的实例还占着端口时，自检自己的模拟器静默 bind 失败
（stderr 被管道吃掉），于是自检**连到了旧实例**上 —— 而旧实例的同屏窗格可能停在
用户视图，`apply` 直接报 `Unrecognized command`，自检 ④ 判失败。
用户看到的是"功能坏了"，真相是"你的旧模拟器没关"。现在：

- 端口被占 → **当场拒跑**，退出码 2，并给出可照抄的解法（`netdev mock stop -p <端口>`）；
- 起模拟器改**轮询**等端口真的监听起来（上限 8 秒），不再用固定 `sleep 1.2`
  —— 固定等待是本项目反复踩过的坑（GitHub ARM 冷启动实测 32s）；
- 起不来时把子进程输出打出来，不再吞掉。

### 加固：锁自愈不再可能误搬**活锁**

`pi_heal_locks(force=True)` 原来不看年龄，见空锁目录就搬。若用户自己正在终端里跑 pi，
那个锁是**活的**，搬走会砸掉她的会话。现在 force 会先探测本机有没有别的 pi 进程存活，
有则自动降级为"只搬 mtime > 60s 的陈旧锁"，并在返回值里如实报告 `degraded`。

### 测试

- 新增 `tests/test_pi_heal_and_cmdcache.py` 第 4 组共 11 项断言，把上述根因钉死：
  shim 剥离、`~/.workbuddy/binaries/**` 不误伤、干净环境零改动、`_env_with_node` 端到端。
  该文件总计 35 项。
- 端到端实测（真实 HTTP + 真实 pi + 真实模型）：`/api/ai/open` → `/api/ai/send`
  → 事件流拿到 assistant 回复；`/api/term/open` → `/api/term/input` 键入的命令
  确实到达设备并回显。

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

- **三种后端可切换**：`pi` agent（常驻 RPC）/ WorkBuddy agent（headless）/
  直连 OpenAI 兼容 API（自带 Key），也可整体关闭 AI。
- MCP 暴露 13 个 `netdev_*` 工具，**全部转调 CLI**，自己不碰设备；
  Bash / Write / Edit 等通用工具硬移除。
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

- 端到端跑通 AI 三后端的真实会话（pi 侧受环境限制，见下）
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
