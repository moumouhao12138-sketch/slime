# Slime Cairn

Slime Cairn 是一个持久化、多项目的 Agent 探索运行时。系统以 Blackboard 保存事实、假设、分支、证据和执行历史，由 Dispatcher 调度 `Bootstrap`、`Explore`、`Reason` 三类任务，并为每个项目维护独立的 Docker Worker 容器与 workspace。

当前版本：`0.0.37`

## 主要特性

- **持久化推理**：项目状态和执行记录写入 SQLite，控制面重启后可以继续运行。
- **项目级隔离**：每个项目使用独立 Worker 容器和独立 workspace 子卷。
- **项目内并发**：同一项目的多个任务共享该项目容器、workspace 和进程环境。
- **分支调度**：根据营养值、分支历史、强度、新颖度和失败次数选择待执行 Intent。
- **失败恢复**：使用租约、心跳、退避、重试和自动暂停控制异常循环。
- **统一入口**：Linux 使用 `slime`，部署由 Compose 完成。
- **可观测性**：提供 Web UI、HTTP API、项目状态、Worker 运行态和事件记录。

## 运行架构

```text
浏览器 / slime CLI
        |
        v
API 容器 -------------------- slime-data
        |                      SQLite Blackboard
        v
Dispatcher 容器
        |  Docker Socket
        v
项目 Worker 容器
        |  每项目一个 volume-subpath
        v
slime-workspaces
```

Docker Compose 管理三个服务：

| 服务 | 作用 | 生命周期 |
|---|---|---|
| `api` | Web UI、HTTP API、项目读写 | 常驻 |
| `dispatcher` | 调度、租约、Worker 容器管理 | 常驻 |
| `worker-image` | 拉取或准备 Worker 镜像 | 一次性，退出码 `0` 表示正常 |

默认应用镜像为 `ghcr.io/moumouhao12138-sketch/slime:0.0.37`，默认 Worker 镜像为 `ghcr.io/moumouhao12138-sketch/slime-worker:0.0.37`。

## 系统要求

- Docker Engine
- Docker Compose v2（`docker compose`）
- 当前用户可以访问 Docker Engine
- 能访问配置的模型服务和容器镜像仓库

Python 只用于本地开发和测试，不是部署依赖。

## 快速开始

```sh
git clone https://github.com/moumouhao12138-sketch/slime.git
cd slime
cp .env.example .env
${EDITOR:-vi} .env
chmod +x slime
./slime up
./slime ui
```

默认 Codex Worker 至少需要填写：

```dotenv
SLIME_CODEX_BASE_URL=https://your-provider.example/v1
SLIME_CODEX_API_KEY=your-api-key
SLIME_CODEX_MODEL=your-model
SLIME_CODEX_REASONING_EFFORT=xhigh
```

默认 UI 地址为 <http://127.0.0.1:8000/>，API 文档为 <http://127.0.0.1:8000/docs>，健康检查为 <http://127.0.0.1:8000/health>。

## 常用命令

以下命令在 Linux 使用 `./slime`。

### 部署命令

```sh
./slime up                 # 构建当前源码并重建全部服务
./slime down               # 停止控制面，保留数据卷
./slime restart            # 重启控制面
./slime pull               # 拉取应用和 Worker 镜像
./slime build              # 从当前源码构建镜像
./slime logs -f            # 持续查看日志
./slime ps                 # 查看 Compose 服务状态
./slime ui                 # 打开 Web UI
./slime shell              # 进入 API 容器
```

`./slime up` 会自动构建当前工作树中的应用和 Worker 镜像，并强制重建
Compose 服务。因此修改源码、默认 Prompt 或 `AGENTS.md` 后无需先单独执行
`./slime build`。数据卷和项目工作空间会保留。

### 项目命令

```sh
./slime new --name demo \
  --target https://target.example/ \
  --goal "完成目标并给出可验证结果" \
  --start-mode growth

./slime list
./slime status --name demo
./slime runtime --name demo
./slime pause --name demo
./slime resume --name demo
./slime retry --name demo --intent-id intent_xxx
./slime delete --name demo
```

`pause` 和 `stop` 等价。业务命令由启动器转发到 `api` 容器内的 Slime CLI，因此宿主机不需要安装 Python 包。

## 并发与分支

默认配置位于 `dispatch.json`：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `runtime.max_workers` | `8` | 全部项目共享的最大并发任务数 |
| `runtime.max_project_workers` | `4` | 单个项目的最大并发任务数 |
| `runtime.max_running_projects` | `3` | 同时进入调度的项目数 |
| `tasks.reason.max_intents` | `8` | 一次 Reason 最多新建的 Intent 数 |
| `workers[].max_running` | Codex 为 `8` | 单个 Worker 适配器的并发上限 |

`tasks.reason.max_intents` 控制一次推理可以生成多少个新分支，不等于同时执行多少个分支。实际并发同时受全局、项目和 Worker 三层上限约束。

默认 `standard` Profile 使用 Docker `bridge` 网络，不增加 Linux capability。默认不设置 CPU、内存和 PID 上限，Worker 进程以 UID/GID `65532:65532` 运行。需要限制资源时，在 `dispatch.json` 的 `container` 段显式配置。

默认关闭启动阶段的模型探针，避免上游短暂余额或网络故障导致 Dispatcher 重启；任务仍会记录模型调用错误。需要启动即阻断时，将 `runtime.worker_healthcheck` 改为 `startup_only`。

## 持久化数据

| Docker 卷 | 内容 |
|---|---|
| `slime-data` | SQLite Blackboard 与控制面状态 |
| `slime-workspaces` | 各项目 workspace；每个项目使用独立 `volume-subpath` |

`slime down` 不删除这两个卷。`slime delete --name NAME` 会异步删除指定项目的 Worker、workspace 和 Blackboard 数据。

## 项目结构

```text
src/slime_cairn/
├─ server/                  API、Blackboard、静态 UI
├─ dispatcher/              调度循环、租约和服务生命周期
├─ domain/                  数据模型、营养、分支和验证规则
├─ workers/                 容器生命周期与原生 Agent 适配器
├─ protocol/                报告协议和提示词资源
└─ integrations/benchmark/  可选评测平台集成
```

## 文档

- [部署文档](docs/DEPLOYMENT.md)
- [使用文档](docs/USER_GUIDE.md)
- [配置参考](docs/CONFIGURATION.md)
- [架构说明](docs/ARCHITECTURE.md)

## 本地开发

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

当前测试套件包含 211 项以上的单元与集成测试。提交代码前还应执行：

```sh
docker compose config -q
```
