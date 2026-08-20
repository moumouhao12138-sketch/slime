# Slime v0.40

本版本修复 `0.0.39` Worker 镜像的发布构建，同时保留 DeepSeek Harness 原生 Worker 和 Agent Match 混合启动模式。

## 主要变化

- 将 DeepSeek Harness 从 Codex、Claude Code 和 Pi 的组合 npm 安装层中拆出，避免大依赖树在同一事务中重排失败。
- DeepSeek Harness 安装增加 npm 下载重试、退避、超时和版本断言。
- 应用镜像加入 Node.js 20，用于在运行镜像内校验和维护静态前端脚本。
- 发布工作流支持通过加密仓库变量传入可选构建代理；未配置时继续直连。
- 应用、Worker、Compose、README 和部署文档版本统一更新为 `0.0.40`。

## 验证

- 固定版本 `dsh` 独立安装成功，命令路径和版本输出通过断言。
- 应用镜像内 Node.js `v20.19.2` 可执行，完整测试套件 250 项全部通过。
- DeepSeek Harness、Native Worker、Agent Match 和启动模式相关测试通过。
- 发布内容继续排除 `.env`、数据库、运行工作区、会话记录和 `attachments/` 题目附件。

升级前请备份 `slime-data` 与 `slime-workspaces`，并根据 `.env.example` 配置 DeepSeek Harness 网关参数。
