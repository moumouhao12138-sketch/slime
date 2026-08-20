# Slime v0.39

本版本增加 DeepSeek Harness 原生 Worker，并将 Agent Match 默认启动路径切换为混合模式。

## 主要变化

- 增加 `deepseek-harness-native`，支持任务级持久化会话、超时后的同会话 conclude 恢复，以及 Chat Completions 网关校验。
- 默认仅启用 DeepSeek Harness，推理强度为 `max`；Codex、Pi 和 Claude 保留为可选 Worker。
- Agent Match 新建或重新同步的题目使用 `hybrid`：Bootstrap 建立事实后进入 Reason，再创建 Explore 分支。
- Worker 镜像安装并登记 `dsh 0.1.0-rc.7`，同时修复空工具调用元数据和 headless 会话恢复。
- README、部署文档、配置参考和镜像标签统一更新为 `0.0.39`。

## 验证

- DeepSeek Harness、Native Worker、Agent Match 和启动模式相关测试：62 项通过。
- 完整测试套件：应用镜像中 250 项有 249 项通过；唯一失败是应用镜像未包含 Node.js。前端语法检查已在包含 Node.js 的 Worker 镜像中通过；DeepSeek/Agent Match/Native 相关 62 项全部通过。
- 发布快照排除 `.env`、数据库、运行工作区、会话记录和 `attachments/` 题目附件。

升级前请备份 `slime-data` 与 `slime-workspaces`，并根据 `.env.example` 配置 DeepSeek Harness 网关参数。
