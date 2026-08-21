# Slime v0.42

本版本修复评测平台题目未执行 Bootstrap 的启动配置问题，统一 Benchmark 与 Agent Match 的混合启动流程，并修正纯附件题被 UI 误显示为靶机的问题。

## 主要变化

- Benchmark 题目新建项目写入 `start_mode=hybrid` 和 `bootstrap_enabled=true`。
- 评测题目现在先执行 Bootstrap 建立初始事实，再由 Reason 创建 Explore 分支。
- Agent Match 与 Benchmark 两条评测入口的启动语义保持一致。
- 增加 Benchmark 生命周期回归断言，防止启动模式回退为 Reason-first 的 `growth`。
- Agent Match 题目明确区分纯附件、网络靶机和附件加靶机三种资源类型。
- 纯附件题只创建本地分析项目和工作区，不启动或回收远程靶机，也不占用赛事平台靶机名额。
- 测试平台 UI 显示附件文件名和下载链接，并隐藏内部 `attachment://` 调度标识与“关闭实例”操作。
- 混合题继续显示真实网络端点与附件；环境回收后仍保留原始资源类型。
- 应用、Worker、Compose、README 和部署文档版本统一更新为 `0.0.42`。

## 验证

- 干净发行快照全量测试通过：254 项测试和 48 项子测试通过。
- 本地运行中的 API 健康检查通过；新题目实际 scope 为 `hybrid/true`，Dispatcher 日志确认启动 Bootstrap。
- 发布内容继续排除 `.env`、数据库、运行工作区、会话记录和 `attachments/` 题目附件。

升级前请备份 `slime-data` 与 `slime-workspaces`，并根据 `.env.example` 配置模型网关参数。
