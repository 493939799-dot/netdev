# 安全策略

## 支持版本

| 版本 | 状态 |
|---|---|
| 1.0.x | ✅ 修复 |
| < 1.0 | ❌ 不再维护 |

## 报告漏洞

**请不要开 public issue。**

邮件联系维护者，或用 GitHub 的
[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)（仓库设置里开启后可用）。

请一并给出：

- 受影响文件与行号
- 复现步骤（**用模拟器即可，请勿用真实生产设备**）
- 你认为的影响面

一般会在 7 天内回应。

## 这个项目的安全边界

它是一个**会直连网络设备的工具**，所以有几条与一般 CLI 不同的注意事项。

### 1. 凭据从哪来、存在哪

- 设备密码：**不进版本库**。走 macOS 钥匙串 / 环境变量 / 原生弹窗。
  `config/devices.toml` 里只有 `password_env` 这样的**变量名**，没有密码本身。
- AI 后端 Key（直连 API）：存 `config/direct.json`，权限 600，已被 `.gitignore` 排除。
- 屏镜像与历史日志：`logs/`、`live/`、`backups/` 全部被 `.gitignore` 排除。

> ⚠️ **如果你把自己的实例配置提交到任何公开仓，请先撤回并轮换密钥。**
> Git 历史即使后续被 rewrite，密钥也可能已被 clone 或缓存。

### 2. AI 拿不到的能力

- Bash / Write / Edit 等通用工具**硬移除**，AI 只能调 `netdev_*` 这 13 个工具
  （只读用 `netdev_run` 等，写操作只能经 `netdev_apply` / `netdev_save` 显式发起）。
- 内置 AI 助手走**直连 API**，用的就是这同一套工具；它没有任何能绕开 CLI 的能力。
- 写操作必须过**四道闸门**：黑名单分类 → 人工审批 → 强制备份 → 逐行下发并校验。
- 审批是 **fail-closed**：审批通道不可达时拒绝执行，不是放行。
- 审批代码 `lib/approval.py` 带 **sha256 基线篡改检测**，`netdev doctor` 会报
  「与基线不一致」—— 这是为了让"悄悄改掉审批逻辑"这件事显眼。

### 3. 已知的安全设计取舍

| 取舍 | 现状 | 风险 |
|---|---|---|
| 服务默认只监听 `127.0.0.1` | 已是 | 若你改 `--host 0.0.0.0` 对外暴露，**界面没有任何鉴权** —— 任何人都能通过它下发配置。**别这么做**，除非你清楚后果。 |
| 页面无 CSRF token | 同上 | 同上，绑定 localhost 时不构成实际风险；一旦对外暴露即为高危。 |
| 人工审批弹窗 | 默认开启，策略 `ask` | `netdev policy allow --minutes N` 可临时放宽。**别在生产环境长期开着。** |
| 串口物理访问 | 无认证 | 拿到串口线的人就能进设备。这是设备本身的性质，不是本项目的缺陷。 |
| 遥测/上报 | **无** | 本项目不收集任何数据，不连任何服务器（AI 后端除外，由你自己选）。 |

### 4. 依赖安全

`requirements.txt` 已固定版本（`==`）。更新依赖时：

```bash
./.venv/bin/pip list --outdated
./netdev doctor        # 依赖段要仍然是 ✔
```

关注这几个（它们直接处理网络与凭据）：
`netmiko` / `paramiko` / `cryptography` / `pyserial` / `scrapli`。

## 授权范围之外

本工具**不是**防火墙、不是 AAA 系统、不是配置管理平台。
它假设你本来就有设备合法操作权限，只是让这些操作更可见、更可回滚。
请不要拿它去访问未授权的设备。
