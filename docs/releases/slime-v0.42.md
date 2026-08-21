# Slime v0.42

本版本修复评测平台题目未执行 Bootstrap 的启动配置问题，并将旧 Benchmark 入口与 Agent Match 统一到混合启动流程。

## 主要变化

- Benchmark 题目新建项目写入 `start_mode=hybrid` 和 `bootstrap_enabled=true`。
- 评测题目现在先执行 Bootstrap 建立初始事实，再由 Reason 创建 Explore 分支。
- Agent Match 与 Benchmark 两条评测入口的启动语义保持一致。
- 增加 Benchmark 生命周期回归断言，防止启动模式回退为 Reason-first 的 `growth`。
- 应用、Worker、Compose、README 和部署文档版本统一更新为 `0.0.42`。

## 验证

- Benchmark、Agent Match、启动模式、可选 Bootstrap 和 CLI 回归测试共 44 项通过。
- 本地运行中的 API 健康检查通过；新题目实际 scope 为 `hybrid/true`，Dispatcher 日志确认启动 Bootstrap。
- 发布内容继续排除 `.env`、数据库、运行工作区、会话记录和 `attachments/` 题目附件。

升级前请备份 `slime-data` 与 `slime-workspaces`，并根据 `.env.example` 配置模型网关参数。
