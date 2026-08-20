# Slime v0.41

本版本修复 Worker 镜像在 Kali rolling 更新到 Python 3.14 后的构建失败，并保留 DeepSeek Harness 原生 Worker 和 Agent Match 混合启动模式。

## 主要变化

- Worker 安装 `build-essential`、`cmake` 和 `pkg-config`，支持 `unicorn` 在没有匹配 wheel 时从源码构建。
- 固定并校验 `pwntools 4.15.0`、`pymongo 4.17.0` 和 `unicorn 2.1.2`，避免依赖解析随 PyPI 变化。
- Worker 默认 Kali 软件源改为 `http://kali.download/kali`，绕开易返回 403 的随机镜像跳转；仍可通过 `SLIME_KALI_APT_MIRROR` 覆盖。
- 应用、Worker、Compose、README 和部署文档版本统一更新为 `0.0.41`。

## 验证

- Dockerfile BuildKit 静态检查通过。
- Python 3.14 无缓存安装及 `unicorn` 源码编译通过，三个包导入和版本断言通过。
- Worker Dockerfile 回归测试、Compose 配置检查和核心测试通过。
- 发布内容继续排除 `.env`、数据库、运行工作区、会话记录和 `attachments/` 题目附件。

升级前请备份 `slime-data` 与 `slime-workspaces`，并根据 `.env.example` 配置 DeepSeek Harness 网关参数。
