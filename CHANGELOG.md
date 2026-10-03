# 更新日志

本文件记录**用户可感知的变化**。每条都尽量写清「真因」——
本项目多数问题的报错信息都离真因很远，只记"改了什么"会让人重复踩坑。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
