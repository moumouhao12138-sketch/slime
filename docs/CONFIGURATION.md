# 配置参考

Slime Cairn 使用两类配置文件：

| 文件 | 内容 | 是否适合保存凭据 |
|---|---|---|
| `.env` | 模型凭据、镜像、端口、部署环境变量 | 是，但不得提交到版本库 |
| `dispatch.json` | 调度、分支、任务、Worker 和容器策略 | 否 |

Compose 将 `SLIME_DISPATCH_FILE` 指向的文件只读挂载到 Dispatcher 的 `/app/dispatch.json`，默认使用根目录的 `dispatch.json`，并将 `.env` 注入应用容器。

## 1. `.env`

从模板创建：

```sh
cp .env.example .env
```

### 1.1 共享模型配置

| 变量 | 必填 | 说明 |
|---|---|---|
| `SLIME_CODEX_BASE_URL` | 是 | OpenAI 兼容 API 基础地址 |
| `SLIME_CODEX_API_KEY` | 是 | API 密钥 |
| `SLIME_CODEX_MODEL` | 是 | 模型名 |
| `SLIME_CODEX_REASONING_EFFORT` | 否 | 推理强度，模板默认 `xhigh` |

示例：

```dotenv
SLIME_CODEX_BASE_URL=https://provider.example/v1
SLIME_CODEX_API_KEY=replace-with-api-key
SLIME_CODEX_MODEL=replace-with-model-name
SLIME_CODEX_REASONING_EFFORT=xhigh
```

### 1.2 共享回退

以下变量是各适配器未设置专用值时的回退：

```dotenv
SLIME_LLM_BASE_URL=
SLIME_LLM_API_KEY=
SLIME_LLM_MODEL=
```

`dispatch.json` 使用 `${PRIMARY|FALLBACK}` 语法读取第一个非空变量。例如：

```json
"CODEX_MODEL": "${SLIME_CODEX_MODEL|SLIME_LLM_MODEL}"
```

未解析到非空值时，Dispatcher 会拒绝启动对应 Worker。

### 1.3 Pi、Claude 与 DeepSeek Harness

Pi 和 Claude 默认关闭。DeepSeek Harness 默认启用；其专用配置留空时会复用 `SLIME_CODEX_*`，需要独立网关或模型时再填写 `SLIME_DEEPSEEK_*`。

```dotenv
SLIME_PI_BASE_URL=
SLIME_PI_API_KEY=
SLIME_PI_MODEL=

SLIME_CLAUDE_BASE_URL=
SLIME_CLAUDE_API_KEY=
SLIME_CLAUDE_MODEL=

SLIME_DEEPSEEK_BASE_URL=
SLIME_DEEPSEEK_API_KEY=
SLIME_DEEPSEEK_MODEL=
```

Pi 使用配置的 OpenAI 兼容接口。Claude 使用 `ANTHROPIC_*` 环境变量，并将项目级配置目录放在 `/workspace/shared/agent-homes/claude`。DeepSeek Harness 使用原生 DeepSeek Chat Completions 协议；每个任务创建独立的 `DSH_HOME`，并在超时后的 conclude 阶段通过持久化 JSONL 会话恢复同一任务上下文。默认 Worker 配置将 `DSH_REASONING_EFFORT` 设为 `max`。

### 1.4 Compose 部署

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SLIME_APP_IMAGE` | `ghcr.io/moumouhao12138-sketch/slime:0.0.39` | API/Dispatcher 共用镜像 |
| `SLIME_WORKER_IMAGE` | `ghcr.io/moumouhao12138-sketch/slime-worker:0.0.39` | 项目 Worker 镜像 |
| `SLIME_BIND_ADDRESS` | `127.0.0.1` | API 监听地址 |
| `SLIME_PORT` | `8000` | 宿主机端口 |
| `SLIME_DISPATCH_FILE` | `./dispatch.json` | 挂载到 Dispatcher 的调度配置文件 |
| `SLIME_WORKER_PROFILE` | `standard` | 项目 Worker 网络与 capability Profile |
| `SLIME_STOP_WORKERS_ON_EXIT` | `0` | Dispatcher 清理时是否同时停止仍在运行的项目容器 |

应用镜像和 Worker 镜像应使用相同版本标签，避免协议或配置结构不一致。

### 1.5 本地构建

这些变量只影响 `slime build`：

| 变量 | 默认值 |
|---|---|
| `SLIME_PYTHON_IMAGE` | `python:3.13-slim` |
| `SLIME_DOCKER_CLI_IMAGE` | `docker:29-cli` |
| `SLIME_WORKER_BASE_IMAGE` | `kalilinux/kali-rolling` |
| `SLIME_KALI_APT_MIRROR` | `http://http.kali.org/kali` |
| `SLIME_KALI_APT_VERIFY_PEER` | `true` |
| `SLIME_INSTALL_NATIVE_AGENTS` | `true` |
| `SLIME_DEEPSEEK_HARNESS_VERSION` | `0.1.0-rc.7` |
| `SLIME_INSTALL_REFERENCE_ASSETS` | `true` |

### 1.6 可选 Benchmark 集成

```dotenv
BENCHMARK_BASE_URL=
BENCHMARK_TOKEN=
BENCHMARK_TIMEOUT=30
BENCHMARK_AUTOMATION_INTERVAL=3
```

只有 `BENCHMARK_BASE_URL` 和 `BENCHMARK_TOKEN` 同时有效时，评测控制面才会启用。Token 只用于服务端调用，不写入 Worker 提示词。

### 1.7 比赛 AI Agent API

```dotenv
AGENT_MATCH_BASE_URL=https://<platform-host>
AGENT_MATCH_ACCESS_KEY=replace-with-agent-access-key
AGENT_MATCH_TIMEOUT=30
AGENT_MATCH_ENV_POLL_INTERVAL=3
AGENT_MATCH_ENV_READY_TIMEOUT=180
```

`AGENT_MATCH_BASE_URL` 是平台主机地址；程序会固定调用其
`/slab-match/api/v1/agent` 路径。AccessKey 只在 API/Dispatcher 控制面使用，
不进入项目 scope、Worker 环境、提示词、Blackboard、日志或本地 API 响应。

通过本地 API 启动题目时，系统会读取详情；若环境尚未初始化，会请求启动并按
`AGENT_MATCH_ENV_POLL_INTERVAL` 轮询，直到环境可用或达到
`AGENT_MATCH_ENV_READY_TIMEOUT`。题目下发的端点、附件和账号信息会作为项目范围
与上下文写入 Blackboard。

本地接口包括：`GET /agent-match/exercises`、`POST /agent-match/exercises/{id}/start`、
`POST /agent-match/exercises/{id}/submit` 与
`POST /agent-match/exercises/{id}/recover`。竞赛规则、排名和公告也分别通过
`/agent-match/match-info`、`/agent-match/overview` 和 `/agent-match/notices` 暴露。

### 1.8 比赛大模型网关

比赛期间应将 Worker 的 `*_BASE_URL` 设为控制台展示的完整网关 URL，模型 API Key
仍使用上游服务商的 Key。不要将 `AGENT_MATCH_ACCESS_KEY` 作为模型 Key 使用。

Codex Worker 使用 OpenAI Responses 协议，因此网关必须可转发 `/responses`。将
`SLIME_COMPETITION_GATEWAY_ONLY=true` 后，可使用 `*_UPSTREAM_ENDPOINT` 声明每个
Worker 对应的完整上游端点；启动时会拒绝非 HTTPS 网关 URL、未授权端点或协议不匹配
的配置。完整授权端点与按适配器选择规则见 [COMPETITION.md](COMPETITION.md)。

## 2. `dispatch.json`

顶层结构：

```json
{
  "schema_version": 2,
  "container": {},
  "runtime": {},
  "tasks": {},
  "common_env": {},
  "workers": []
}
```

### 2.1 容器配置

默认配置：

```json
{
  "container": {
    "init": true
  }
}
```

默认不设置 CPU、内存和 PID 数量上限，即使用 Docker Engine 可分配的资源。需要限制时显式添加：

```json
{
  "container": {
    "init": true,
    "cpus": "4",
    "memory": "8g",
    "pids_limit": 1024
  }
}
```

将 `cpus`、`memory` 或 `pids_limit` 设为 `null` 表示不限制。资源配置应用于每个项目容器，而不是每个并发任务。

Worker 的其他固定运行属性：

- 使用 `kali` 用户，UID/GID 为 `1000:1000`，可免密使用 `sudo`；
- 根文件系统可写；
- `/tmp` 使用项目容器的 Docker overlay 存储，不设独立容量上限；
- 保留 Docker 默认 Linux capability；
- `/workspace` 挂载到 `slime-workspaces` 中该项目的 `volume-subpath`。

### 2.2 容量配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `runtime.max_workers` | `8` | 所有项目共享的任务并发上限 |
| `runtime.max_project_workers` | `4` | 单项目任务并发上限 |
| `runtime.max_running_projects` | `3` | 同时调度的项目上限 |
| `workers[].max_running` | 取决于 Worker | 单个适配器并发上限 |

实际并发不会超过以上各层当前可用容量。增加这些值会提高模型请求数、容器内进程数、内存使用和 workspace 写入竞争，应结合模型服务配额与宿主机资源调整。

### 2.3 调度与租约

| 字段 | 当前值 | 说明 |
|---|---:|---|
| `runtime.interval` | `3` 秒 | 调度周期 |
| `runtime.lease_seconds` | `15` 秒 | Explore/Bootstrap 租约时长 |
| `runtime.reason_lease_seconds` | `15` 秒 | Reason 租约时长 |
| `runtime.heartbeat_interval` | `3` 秒 | 租约心跳间隔 |
| `runtime.reason_batch_size` | `2` | 触发 Reason 的完成信号批量阈值 |
| `runtime.reason_debounce_seconds` | `1` 秒 | Reason 信号合并等待 |
| `runtime.state_heartbeat_interval` | `2` 秒 | 运行态持久化心跳 |
| `runtime.worker_healthcheck` | `startup_and_task` | Dispatcher 启动和每次任务前检查模型端点 |
| `runtime.healthcheck_timeout` | `60` 秒 | 健康检查超时 |

`worker_healthcheck` 可选：

- `disabled`：不执行模型端点健康检查；
- `startup_only`：Dispatcher 启动时检查一次；
- `startup_and_task`：启动时及每次任务前检查。

必须保持 `heartbeat_interval` 小于两类租约时长。

### 2.4 失败、退避与暂停

| 字段 | 当前值 | 说明 |
|---|---:|---|
| `runtime.max_intent_attempts` | `6` | 单个 Intent 最大租约尝试数；`0` 表示不设上限 |
| `runtime.intent_failure_backoff_seconds` | `30` 秒 | 首次失败退避 |
| `runtime.intent_failure_backoff_max_seconds` | `1800` 秒 | Intent 最大退避 |
| `runtime.worker_rejected_cooldown_seconds` | `5` 秒 | Worker 拒绝后的冷却 |
| `runtime.intent_worker_cycle_cooldown_seconds` | `30` 秒 | 所有可用 Worker 均失败后的轮转冷却 |
| `runtime.reason_failure_cooldown_seconds` | `5` 秒 | Reason 失败冷却 |
| `runtime.reason_failure_pause_threshold` | `3` | 连续 Reason 失败暂停阈值 |
| `runtime.reason_failure_pause_seconds` | `60` 秒 | 首次自动暂停时长 |
| `runtime.reason_failure_pause_max_seconds` | `300` 秒 | 自动暂停时长上限 |

这些配置限制失败循环，不改变模型服务自身的重试行为。

### 2.5 分支选择

当前默认：

```json
{
  "runtime": {
    "growth_selection_mode": "sma_discrete",
    "growth_exploration_probability": 0.12,
    "growth_exploration_min_probability": 0.03,
    "growth_convergence_selections": 24,
    "growth_random_seed": "slime-sma-v1"
  }
}
```

`growth_selection_mode` 可选：

- `sma_discrete`：综合营养、分支历史、强度、新颖度和失败惩罚，并保留逐步收敛的探索概率；
- `nutrient`：始终优先当前营养值最高的可运行 Intent。

分支策略只决定待运行 Intent 的顺序，不会绕过范围、状态、重试时间、租约和容量约束。

### 2.6 任务配置

```json
{
  "tasks": {
    "bootstrap": {
      "timeout": 300,
      "conclude_timeout": 90
    },
    "reason": {
      "timeout": 300,
      "max_intents": 8
    },
    "explore": {
      "timeout": 300,
      "conclude_timeout": 90
    }
  }
}
```

`tasks.reason.max_intents` 是一次 Reason 报告最多创建的新 Intent 数，也就是单次分支生成上限。它不控制：

- 当前项目总共可以积累多少 Intent；
- 同时运行多少任务；
- Reason 多久触发一次。

这三项分别由历史状态、容量配置和 Reason 调度配置决定。

### 2.7 Worker 配置

当前默认 Worker：

| 名称 | 类型 | 默认状态 | 任务类型 | `max_running` |
|---|---|---|---|---:|
| `codex-native` | Codex | 关闭 | bootstrap/explore/reason | `8` |
| `pi-native` | Pi | 关闭 | bootstrap/explore/reason | `4` |
| `claude-native` | Claude Code | 关闭 | bootstrap/explore/reason | `2` |
| `deepseek-harness-native` | DeepSeek Harness | 启用 | bootstrap/explore/reason | `8` |

Worker 条目示例：

```json
{
  "name": "codex-native",
  "type": "codex",
  "execution": "native-agent",
  "task_types": ["bootstrap", "explore", "reason"],
  "max_running": 8,
  "priority": 0,
  "max_report_items": 30,
  "container_preflight": false,
  "env": {
    "CODEX_MODEL": "${SLIME_CODEX_MODEL|SLIME_LLM_MODEL}",
    "CODEX_BASE_URL": "${SLIME_CODEX_BASE_URL|SLIME_LLM_BASE_URL}",
    "OPENAI_API_KEY": "${SLIME_CODEX_API_KEY|SLIME_LLM_API_KEY}"
  },
  "config_overrides_from_env": {
    "model_reasoning_effort": "SLIME_CODEX_REASONING_EFFORT"
  }
}
```

至少一个已启用 Worker 必须支持 `explore` 和 `reason`。`priority` 越小，容量相同时越优先。

### 2.8 常驻提示词

提示词资源位于：

```text
src/slime_cairn/protocol/prompts/default/
├─ AGENTS.md
├─ bootstrap.md
├─ bootstrap_conclude.md
├─ explore.md
├─ explore_conclude.md
└─ reason.md
```

`runtime.prompt_group` 默认是 `default`。`AGENTS.md` 是项目常驻指令，其余文件对应具体阶段。新提示词组必须包含全部文件及代码要求的占位符，才能通过加载校验。

## 3. Worker Profile

| Profile | Docker 网络 | 增加 capability | 说明 |
|---|---|---|---|
| `standard` | `host` | Docker 默认值 | 默认，与 Cairn 的 Worker 网络一致 |
| `raw-network` | `host` | Docker 默认值 + `NET_RAW` | 显式声明原始网络数据包能力 |
| `lab-network-admin` | `slime-lab` | `NET_RAW`、`NET_ADMIN` | 使用 Compose 创建的独立实验网络 |

通过 `.env` 选择：

```dotenv
SLIME_WORKER_PROFILE=standard
```

Profile 是整套部署的项目容器策略。已有项目容器的网络或 capability 与新配置不一致时，Dispatcher 会重建容器并保留 workspace。

## 4. 配置生效

- 修改 `.env`：执行 `./slime up`。
- 修改 `dispatch.json`：执行 `./slime restart dispatcher`。
- 修改镜像构建参数：执行 `./slime build`，再执行 `./slime up`。
- 修改 Worker 镜像：执行 `./slime pull` 和 `./slime up`。

检查 Compose 配置：

```sh
docker compose config -q
```

检查服务读取结果：

```sh
./slime logs dispatcher
./slime runtime --name PROJECT_NAME
```
