<!-- 提 PR 前请先读 CONTRIBUTING.md 的「三条不可让步的架构约束」 -->

## 改了什么

<!-- 一两句话说清：改了什么、为什么 -->

## 自检（任何一项没过就不算完成）

- [ ] `./netdev doctor` 15/15
- [ ] `.venv/bin/python tests/test_ai_toolchain_and_cache.py`（135/135）
- [ ] `.venv/bin/python tests/test_approval_gates.py`（19/19）
- [ ] `.venv/bin/python tests/test_ui_lifecycle.py`（27/27）
- [ ] `.venv/bin/python tests/test_mock_cmd.py`（21/21）

## 泄密自查

- [ ] 改动不含真实设备密码、真实 IP、USB 序列号、本机绝对路径
