# 部署文档

本文说明如何使用 Docker Compose 部署、升级和维护 Slime Cairn。运行环境和项目依赖均封装在镜像中。

## 1. 部署模型

默认部署包含：

- `api`：常驻控制面，提供 Web UI 和 HTTP API。
- `dispatcher`：常驻调度服务，通过 Docker Socket 管理项目 Worker。
- `worker-image`：一次性镜像准备服务，成功完成后状态为 `Exited (0)`。
- `slime-data`：保存 SQLite Blackboard。
- `slime-workspaces`：保存所有项目 workspace，每个项目占用独立子目录。

应用与 Worker 使用两个独立镜像：

```text
ghcr.io/moumouhao12138-sketch/slime:0.0.37
ghcr.io/moumouhao12138-sketch/slime-worker:0.0.37
```

Dispatcher 挂载 `/var/run/docker.sock`。因此运行 Dispatcher 的身份具有管理该 Docker Engine 的能力，部署主机应只允许受信任的管理员访问。

## 2. 前置条件

- 现代 Linux 发行版
- Docker Engine
- Docker Compose v2 插件
- 当前用户可以执行 `docker` 命令

确认环境：

```sh
docker version
docker compose version
docker info
```

## 3. 首次部署

### 3.1 获取项目

```sh
git clone https://github.com/moumouhao12138-sketch/slime.git
cd slime
```

### 3.2 创建环境配置

```sh
cp .env.example .env
${EDITOR:-vi} .env
chmod +x slime
```

默认 Codex Worker 至少需要有效的模型地址、密钥和模型名：

```dotenv
SLIME_CODEX_BASE_URL=https://your-provider.example/v1
SLIME_CODEX_API_KEY=your-api-key
SLIME_CODEX_MODEL=your-model
SLIME_CODEX_REASONING_EFFORT=xhigh
```

`.env` 包含凭据，不应提交到版本库。

### 3.3 启动

```sh
./slime up
./slime ps
```

`up` 会构建当前工作树中的应用和 Worker 镜像，并强制重建全部 Compose
服务。修改源码、Prompt 或 `AGENTS.md` 后，直接再次执行 `up` 即可生效。

若没有提前创建 `.env`，启动器会从 `.env.example` 创建它并以退出码 `2` 停止。填写配置后再次执行 `up`。

### 3.4 验证

```sh
./slime ps
./slime logs api dispatcher
```

默认检查地址：

```text
UI       http://127.0.0.1:8000/
OpenAPI  http://127.0.0.1:8000/docs
Health   http://127.0.0.1:8000/health
```

健康检查应返回 HTTP `200`。`api` 和 `dispatcher` 应保持运行；`worker-image` 显示 `Exited (0)` 是正常结果。

## 4. 日常管理

| 命令 | 作用 |
|---|---|
| `./slime up` | 构建当前源码并重建全部服务 |
| `./slime down` | 停止 Compose 服务，保留数据 |
| `./slime restart` | 快速重启现有服务，不重新构建 |
| `./slime pull` | 拉取应用和 Worker 镜像 |
| `./slime build` | 从当前源码构建镜像 |
| `./slime logs -f` | 持续查看日志 |
| `./slime ps` | 查看服务状态 |
| `./slime shell` | 进入 API 容器 |

查看单个服务日志：

```sh
./slime logs -f api
./slime logs -f dispatcher
```

停止控制面不会删除 `slime-data` 或 `slime-workspaces`。若要确保某个项目的 Worker 先停止，应先暂停该项目：

```sh
./slime pause --name PROJECT_NAME
```

## 5. 使用本地源码构建

`up` 已包含本地构建。需要只构建、不启动服务时执行：

```sh
./slime build
```

构建参数来自 `.env`：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SLIME_PYTHON_IMAGE` | `python:3.13-slim` | 应用镜像基础环境 |
| `SLIME_DOCKER_CLI_IMAGE` | `docker:29-cli` | 注入应用镜像的 Docker CLI |
| `SLIME_WORKER_BASE_IMAGE` | `kalilinux/kali-rolling` | Worker 基础镜像 |
| `SLIME_KALI_APT_MIRROR` | Kali 官方镜像 | Worker 软件源 |
| `SLIME_INSTALL_NATIVE_AGENTS` | `true` | 安装 Codex、Claude、Pi CLI |
| `SLIME_INSTALL_REFERENCE_ASSETS` | `false` | 构建时下载额外参考资产 |

Worker 镜像体积较大，首次构建需要较长时间和稳定网络。

## 6. 升级与回滚

升级到 `.env` 中指定的镜像版本：

```sh
./slime pull
./slime up
./slime ps
```

升级前建议备份两个数据卷。回滚时，将 `.env` 中的镜像标签改回已验证版本，再执行相同命令：

```dotenv
SLIME_APP_IMAGE=ghcr.io/moumouhao12138-sketch/slime:0.0.37
SLIME_WORKER_IMAGE=ghcr.io/moumouhao12138-sketch/slime-worker:0.0.37
```

当 Worker 镜像引用或镜像 ID 变化时，Dispatcher 会按需重建项目 Worker 容器；项目 workspace 保存在独立数据卷中。

## 7. 数据备份

执行备份前暂停仍在运行的项目，再停止控制面：

```sh
./slime down
```

在项目根目录执行：

```sh
docker run --rm \
  --mount type=volume,src=slime-data,dst=/source,readonly \
  --mount type=bind,src="$PWD",dst=/backup \
  alpine tar -czf /backup/slime-data.tar.gz -C /source .

docker run --rm \
  --mount type=volume,src=slime-workspaces,dst=/source,readonly \
  --mount type=bind,src="$PWD",dst=/backup \
  alpine tar -czf /backup/slime-workspaces.tar.gz -C /source .
```

备份应同时保存 `.env` 和 `dispatch.json`，并采用单独的凭据保护措施。

恢复时应保持服务停止，创建目标卷后将压缩包解压到对应卷，再执行 `up`。不要把不同时间点的 Blackboard 和 workspace 备份混合恢复。

## 8. 数据删除

删除单个项目：

```sh
./slime delete --name PROJECT_NAME
```

该请求异步停止项目任务，并删除该项目的容器、workspace 子卷和 Blackboard 记录。

删除整套部署及其数据卷：

```sh
./slime down -v
```

这是不可恢复的数据操作，执行前应确认备份有效。

## 9. 网络与端口

默认仅监听本机：

```dotenv
SLIME_BIND_ADDRESS=127.0.0.1
SLIME_PORT=8000
```

项目 Worker 默认使用 `standard` Profile：

```dotenv
SLIME_WORKER_PROFILE=standard
```

`standard` 使用 Docker `bridge` 网络且不增加 capability。只有确实需要时才选择其他 Profile；详细配置见 [CONFIGURATION.md](CONFIGURATION.md)。

## 10. 故障排查

### API 一直不健康

```sh
./slime ps
./slime logs api
docker compose config
```

重点检查端口占用、镜像是否可用以及 `slime-data` 是否可写。

### Dispatcher 反复重启

```sh
./slime logs dispatcher
docker compose config -q
```

重点检查 `dispatch.json` 的 JSON 语法、Worker 环境变量是否完整，以及 Docker Socket 是否可访问。

### 无法拉取 GHCR 镜像

确认镜像可公开读取；私有包需先登录：

```sh
docker login ghcr.io
./slime pull
```

也可以执行 `./slime build` 使用当前源码本地构建。

### 项目节点持续失败

```sh
./slime status --name PROJECT_NAME
./slime runtime --name PROJECT_NAME
./slime logs -f dispatcher
```

检查模型 endpoint、模型名、密钥、Worker 健康状态和 Intent 的最后错误。修复原因后可重试指定 Intent：

```sh
./slime retry --name PROJECT_NAME --intent-id intent_xxx
```

### 配置修改没有生效

- 修改 `.env` 后执行 `./slime up`，让 Compose 按新环境重建服务。
- 修改 `dispatch.json` 后执行 `./slime restart dispatcher`。
- 修改镜像构建参数后执行 `./slime build`，再执行 `./slime up`。
