# Slime Cairn

Slime Cairn `v0.0.37` 是一个受 Cairn 和黏菌生长机制启发的持久化多项目 Agent 运行时。它使用 FastAPI 作为控制面、SQLite Blackboard 作为长期状态、一项目一持久 Kali 容器作为执行环境，并允许 Codex、Pi、Claude Code 等原生 CLI Worker 在同一项目内并发探索。

项目面向需要持续收集证据、分支探索、失败恢复和可审计推理的任务。Worker 直接使用容器中的 Shell、文件系统和工具，不经过模型工具网关或远程工具转发层。

## 核心能力

- 持久 Blackboard：保存 Project、Fact、Hypothesis、Intent、Hint、Evidence、Worker Run、Completion 和事件流。
- 一项目一容器：项目拥有独立的持久 Kali 容器和 `/workspace`，停止、恢复或服务重启后仍保留上下文。
- 多 Worker 并发：Codex、Pi、Claude Code 可按健康状态、能力、优先级和容量共同承担任务。
- 三阶段任务协议：Bootstrap 建立初始事实，Explore 并发验证分支，Reason 统一创建后续 Intent 或完成项目。
- 离散 SMA 分支策略：综合营养评分、分支历史、强度、新颖度和失败次数，在利用与探索之间收敛。
- 原子租约与恢复：Intent 通过 SQLite 原子 claim、短租约、心跳、退避和失败上限保证可恢复执行。
- 完整可观测性：本地 Web 面板、图视图、Inspector、Evidence、Worker Run、运行状态和增量事件流。
- Windows/Linux 命令：统一使用 `slime ...`，两个平台共用同一套 Python CLI 实现。
- 可选评测平台：支持 TSec Agent Benchmark challenge 的启动、提示、提交、关闭和并发自动化。

## 运行架构

```text
用户 / Web UI / CLI
        |
        v
FastAPI 控制面 -------- SQLite Blackboard
        |                       |
        |                       +-- Fact / Hypothesis / Intent / Hint
        |                       +-- Lease / Retry / Worker Run / Event
        v
Dispatcher + Scheduler
        |
        +-- Worker 健康、能力和容量选择
        +-- 离散 SMA 分支排序
        +-- Blackboard 原子 claim 与心跳续租
        v
项目专属持久 Kali 容器
        |
        +-- Codex CLI   (Responses)
        +-- Pi CLI      (Chat Completions)
        +-- Claude Code (可选，host config)
        v
结构化报告 -> 校验 -> Blackboard -> 下一轮 Reason
```

Slime 只支持容器执行后端。一个项目拥有一个容器和一个共享 `/workspace`；同一项目的多个 Worker 共享该容器、workspace 和容器进程环境，因此项目内并发任务不是强隔离。不同项目拥有不同容器和宿主 workspace。

## 系统要求

| 项目 | Windows | Linux |
|---|---|---|
| Python | 3.10 或更高 | 3.10 或更高 |
| 容器运行时 | Docker Desktop | Docker Engine |
| Shell | PowerShell 5.1+ | POSIX `sh` |
| 权限 | 当前用户可运行 Docker | 当前用户可运行 `docker` |
| 网络 | 取决于 Worker profile | 取决于 Worker profile |

启动前确认：

1. Docker Engine 正常运行。
2. 已构建 `slime-cairn-kali:0.0.21` 镜像。
3. `.env` 中存在所有已启用 Worker 需要的模型地址、密钥和模型名。
4. 目标任务和网络访问已经获得明确授权。

## 快速开始

以下命令均在项目根目录执行。

### 1. 安装 Python 依赖

Windows PowerShell：

```powershell
python -m pip install -e ".[dev]"
```

Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

Windows 启动器默认使用 `python`，Linux 启动器默认使用 `python3`。如需固定虚拟环境解释器，在启动 Slime 的 Shell 中设置 `SLIME_PYTHON`。

### 2. 创建环境配置

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

Linux：

```sh
cp .env.example .env
```

最小的共享模型配置：

```dotenv
SLIME_LLM_BASE_URL=https://api.example.com/v1
SLIME_LLM_API_KEY=replace-with-a-low-privilege-key
SLIME_LLM_MODEL=replace-with-model-name
```

适配器专用的 `SLIME_CODEX_*` 和 `SLIME_PI_*` 会覆盖共享的 `SLIME_LLM_*`。不要把真实密钥写入 dispatch JSON、项目 scope、Prompt、测试或文档。

### 3. 构建 Kali Worker 镜像

Windows PowerShell：

```powershell
.\scripts\finish-docker.ps1
```

Linux：

```sh
sh scripts/finish-docker.sh
```

构建脚本默认使用：

- 基础镜像：`docker.1ms.run/kalilinux/kali-rolling`
- Kali 软件源：`https://mirrors.ustc.edu.cn/kali/`
- 输出镜像：`slime-cairn-kali:0.0.21`
- Reference Assets：默认不安装

Linux 启用 Reference Assets：

```sh
sh scripts/finish-docker.sh --install-reference-assets true
```

### 4. 安装短命令

Windows PowerShell：

```powershell
.\slime install
```

Linux：

```sh
sh ./slime install
```

重新打开终端后即可直接使用：

```text
slime help
slime doctor
slime up
```

不安装时，Windows 使用 `.\slime ...`，Linux 使用 `sh ./slime ...`。安装只配置当前用户命令，不会启动服务；可用 `slime uninstall` 移除。

### 5. 诊断并启动

```text
slime doctor
slime up
slime ui
```

`doctor` 检查 Docker、Worker 镜像、容器内 CLI、dispatch 配置和已启用模型端点。只有 `ready.service` 为 `true` 时，运行环境才完整可用。

默认 UI 地址：`http://127.0.0.1:8000/`。
FastAPI 交互文档默认位于 `http://127.0.0.1:8000/docs`。

## CLI 命令

| 命令 | 作用 |
|---|---|
| `slime help` | 显示命令示例 |
| `slime install` | 安装当前用户短命令 |
| `slime uninstall` | 移除当前用户短命令 |
| `slime doctor` | 诊断 Docker、镜像、CLI 和模型端点 |
| `slime up` | 后台启动 API 和 Dispatcher |
| `slime down` | 停止 API 和 Dispatcher，保留持久数据 |
| `slime restart` | 重启 API 和 Dispatcher |
| `slime serve` | 前台运行 API，适合开发调试 |
| `slime dispatch` | 前台运行 Dispatcher |
| `slime ui` | 打开本地 Web 面板 |
| `slime new` | 创建项目 |
| `slime list` | 列出项目 |
| `slime status` | 查看项目结果和状态 |
| `slime runtime` | 查看项目容器运行状态 |
| `slime logs` | 查看服务日志 |
| `slime pause` / `stop` | 停止项目和活动 Worker |
| `slime resume` | 恢复同一项目 |
| `slime delete` | 请求删除项目及其项目级运行数据 |

Windows PowerShell 参数沿用 `-Name`、`-Target`、`-Follow` 风格。Linux/Python CLI 同时支持 `-Name` 和标准的 `--name` 风格。

常用示例：

```text
slime list
slime logs -Follow
slime logs -Log all -Tail 200
slime status -Name test-001
slime runtime -Name test-001
slime pause -Name test-001
slime resume -Name test-001
slime delete -Name test-001
```

只检查启动参数而不启动服务：

```text
slime up -DryRun
```

## 创建项目

```text
slime new -Name "test-001" -Target "https://TARGET/" -Goal "收集证据并返回可验证结论。" -StartMode growth
```

### 启动模式

| 模式 | 行为 |
|---|---|
| `growth` | 默认。跳过直接 Bootstrap 解题，从 Reason 开始创建多个 Explore 分支 |
| `direct` | Cairn 风格。先执行 Bootstrap，简单任务可直线完成，再按需进入 Reason |

项目状态变化不会删除记忆：

- `pause` 和 `stop`：将项目设为 `stopped`，取消活动 CLI、围栏并释放租约。
- `resume`：恢复同一 Project、Blackboard、workspace、Evidence 和历史 Worker Run。
- `reopen`：已完成项目可在 UI 中携带反馈重新打开。
- `delete`：异步停止项目 Worker，并删除该项目拥有的容器、workspace 和 Blackboard 数据；这是不可逆的项目级操作。

## 默认 Worker

生产默认配置为 `dispatch.cairn.native.json`。

| Worker | 默认状态 | 协议/入口 | 任务类型 | 并发上限 |
|---|---|---|---|---:|
| `codex-native` | 启用 | Codex / Responses | Bootstrap、Explore、Reason | 4 |
| `pi-native` | 启用 | Pi / Chat Completions | Bootstrap、Explore、Reason | 4 |
| `claude-native` | 禁用 | Claude Code / host config | Bootstrap、Explore、Reason | 2 |

Codex 和 Pi 优先读取各自的 `SLIME_CODEX_*`、`SLIME_PI_*`，没有设置时回退到 `SLIME_LLM_*`。Claude 启用后读取当前用户的 `~/.claude`，并在项目 workspace 中生成可写的项目级 Agent home；未启用 Claude 时不会要求该目录存在。

Worker 的任务类型是能力声明，不是固定模型角色。Dispatcher 根据以下条件选择 Worker：

1. 是否启用且健康。
2. 是否支持当前任务类型。
3. Worker 优先级和剩余容量。
4. 当前 Intent 是否已在本轮被该 Worker 拒绝或失败。

如果策略首选分支没有可用 Worker，Dispatcher 会尝试后续候选分支；未被 claim 的分支不会增加 attempts。

## Bootstrap、Explore 与 Reason

### Bootstrap

用于直接模式的初始探测和事实建立。默认执行超时 `300` 秒；若首轮报告不完整，可在同一会话进行最长 `90` 秒的 conclude。

### Explore

对一个明确 Intent 进行证据收集或假设验证。多个 Explore 可并发运行。默认执行超时 `300` 秒，conclude 最长 `90` 秒。

### Reason

读取完整 Blackboard 图和所有开放 Intent，负责：

- 基于已有事实创建最多 `tasks.reason.max_intents` 个新 Intent，生产默认值为 `8`；
- 整合跨分支证据；
- 返回引用有效 Fact 的 Completion；
- 在收到新 Hint、Fact 或审计信号时重新规划。

Reason 是正常运行路径中创建后续 Intent 和完成项目的唯一规划写入者，避免 Worker 私自产生调度器不会消费的任务。

## Blackboard 与持久记忆

Blackboard 使用 SQLite 保存项目的因果图和运行状态：

- `Fact`：经过证据门校验的事实。
- `Hypothesis`：待验证的假设及其支持 Fact。
- `Intent`：可 claim 的具体工作方向。
- `Hint`：持久的人类判断，会唤醒 Reason。
- `Evidence`：文件、命令输出或外部证据引用。
- `Worker Run`：一次 Worker 执行的模型、状态、报告和错误历史。
- `Completion`：引用 Blackboard Fact 的最终结论。
- `Event`：按不可变 ID 排序的运行事件。

每次 Explore 或 Reason 前，Scheduler 将完整 Blackboard 导出为 Cairn 形状的 `graph.yaml`。Fact 内容、Hint 和 Intent 历史不会因为 token 排名而被静默丢弃；Worker 从持久 workspace 中读取该快照。

## Worker 提示词

Worker 提示词按 Cairn 的方式存放在 `src/slime_cairn/prompts/<group>/`。生产配置通过 `runtime.prompt_group` 选择组，默认组包含：

- `bootstrap.md`、`explore.md`、`reason.md`：三个执行阶段；
- `bootstrap_conclude.md`、`explore_conclude.md`：同会话收尾阶段；
- `AGENTS.md`：项目常驻指令。

项目第一次分配 Worker 时，常驻指令会安装为 workspace 根目录的 `AGENTS.md` 和 `CLAUDE.md`。Codex、Claude Code 和 Pi 因而共享同一份项目环境与结果约束，阶段模板仍只负责当前任务。修改默认提示词不需要再编辑 `native_agent.py`。

## 离散 SMA 分支调度

默认 `growth_selection_mode` 为 `sma_discrete`。它借鉴 Slime Mould Algorithm 的适应度排序、加权吸引、随机探索和迭代收敛，但不把文本任务伪装成连续数值向量。

候选分支适应度：

```text
fitness = 0.60 * 当前 nutrient
        + 0.20 * 同 branch_root_id 的历史最佳 nutrient
        + 0.15 * 当前 strength
        + 0.05 * novelty
        - failure_streak 惩罚
```

其中 failure penalty 每次失败增加 `0.075`，上限为 `0.30`。营养、历史营养和强度会在当前候选集内归一化。

选择规则：

- 1 到 2 个候选：直接选择适应度最高者。
- 3 个及以上候选：通常按指数适应度权重进行吸引选择。
- 探索触发时：从排名较低的一半中按逆适应度抽样。
- 探索概率：前 24 次成功选择中从 `0.12` 线性收敛到 `0.03`。
- 随机种子：由配置种子、Project ID、进程内选择序号和候选 Intent ID 计算 SHA-256。

策略只重新排序已经满足条件的 runnable Intent，不绕过以下硬约束：

- 项目必须为 `running`；
- Intent 必须为 `pending` 且重试时间已到；
- target 必须通过授权 scope 校验；
- attempts、lease、Worker 能力和容量必须有效；
- 最终仍通过 SQLite 按 Intent ID 原子 claim。

每次选择产生 `growth.branch_selected` 事件，其中包含候选排名、评分组成、探索概率、随机值、种子摘要、策略首选 Intent 和 Worker 回退结果。设置以下配置可恢复旧的严格营养排序：

```json
{
  "runtime": {
    "growth_selection_mode": "nutrient"
  }
}
```

收敛计数属于 Dispatcher 进程，服务重启后从 0 开始；所有实际选择仍持久记录在事件流中。

## 并发、租约与失败恢复

默认并发：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `max_workers` | 8 | 全局活动任务上限 |
| `max_running_projects` | 3 | 同时运行的项目 Dispatcher 上限 |
| `max_project_workers` | 4 | 单项目活动任务上限 |
| `lease_seconds` | 15 | Intent 租约时长 |
| `heartbeat_interval` | 3 | 租约心跳间隔 |
| `max_intent_attempts` | 6 | 自动尝试上限 |
| `intent_failure_backoff_seconds` | 30 | 首次重试延迟 |
| `intent_failure_backoff_max_seconds` | 1800 | 重试延迟上限 |

默认六次失败路径的等待时间为 30、60、120、240、480 秒，第六次失败后 Intent 进入终态 `failed`。错误、attempts、Worker Run 和产物不会被删除，Inspector 可手动重新打开同一 Intent。

将 `max_intent_attempts` 设为 `0` 可选择 Cairn 兼容的无限开放队列，但不建议作为常规生产默认值。

## 容器、workspace 与资源限制

一个项目拥有一个持久容器和一个宿主 workspace：

```text
runs/slime-workspaces/<project>/
  context/                     完整 Blackboard 快照
  pods/<worker>/<task>/        Prompt、transcript、result、evidence、manifest
  shared/                      项目内 Worker 共享文件和 Agent home
```

默认不设置 memory、CPU 或 PID quota，这与当前 Cairn 行为一致。容器仍使用 `--init` 回收孤儿进程，单次任务由容器内 `timeout -k 5s` 控制。

需要限制时，在 dispatch JSON 的 `container` 中显式设置：

```json
{
  "container": {
    "init": true,
    "pids_limit": 1024,
    "memory": "8g",
    "cpus": "4"
  }
}
```

省略字段或设为 `null` 表示不限制。修改镜像、网络、init 或 cgroup 设置后，项目容器会在下次启动时重建，但宿主 bind-mounted workspace 保留。

### 网络 Profile

| Profile | Docker 网络 | 附加能力 | 使用场景 |
|---|---|---|---|
| `standard` | `none` | 无 | 完全离线任务 |
| `raw-network` | `bridge` | `NET_RAW` | 默认，需要常规外网或原始报文 |
| `lab-network-admin` | 内部 lab 网络 | `NET_RAW`、`NET_ADMIN` | 明确授权的隔离实验环境 |

Profile 只控制网络和 capability，不提供同一项目内 Worker 之间的强隔离。

## 重要配置

默认 dispatch 文件：`dispatch.cairn.native.json`。示例文件只有通过 `-Config` 或 `SLIME_DISPATCH_CONFIG` 显式选择时才生效：

- `dispatch.cairn.reason-first.example.json`：Explore/Reason-only 生长配置。
- `dispatch.native.example.json`：可复制的原生 Worker 配置模板。

### 健康检查模式

| 值 | 行为 |
|---|---|
| `disabled` | 跳过模型端点探测，仅适合明确的离线场景 |
| `startup_only` | 默认，启动 Dispatcher 前探测所有已启用 Worker |
| `startup_and_task` | 启动前和每个任务前都探测 |

### 常用环境变量

| 变量 | 作用 |
|---|---|
| `SLIME_LLM_BASE_URL` / `API_KEY` / `MODEL` | Codex/Pi 共享回退配置 |
| `SLIME_CODEX_*` | Codex Responses 专用覆盖 |
| `SLIME_PI_*` | Pi Chat Completions 专用覆盖 |
| `SLIME_DISPATCH_CONFIG` | dispatch JSON 路径 |
| `SLIME_CAIRN_DB` | Blackboard SQLite 路径 |
| `SLIME_WORKSPACES_ROOT` | 项目 workspace 根目录 |
| `SLIME_WORKER_PROFILE` | 容器网络 Profile |
| `SLIME_DOCKER_BINARY` | Docker CLI 路径或命令名 |
| `SLIME_GROWTH_SELECTION_MODE` | `sma_discrete` 或 `nutrient` |
| `SLIME_MAX_INTENT_ATTEMPTS` | Intent 自动尝试上限 |
| `SLIME_WORKER_HEALTHCHECK` | Worker 健康检查模式 |

完整环境变量模板见 `.env.example`，完整配置说明见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

## 可观测性与 API

本地 UI 包含：

- Completion 和支持 Fact 的结果优先视图；
- Fact -> Intent -> Fact/Hypothesis 因果图；
- 节点 Inspector、Evidence 和 Worker Run；
- 活动 Worker、租约心跳和容器状态；
- 基于事件 ID 增量拉取的时间线；
- Hint、重试、暂停、恢复、重新打开和删除操作。

主要 API：

```text
GET    /health
POST   /projects
GET    /projects
POST   /projects/bulk-delete
GET    /projects/{project_id}
DELETE /projects/{project_id}
GET    /projects/{project_id}/view
GET    /projects/{project_id}/events?after_id=EVENT_ID&limit=100
PUT    /projects/{project_id}/status
POST   /projects/{project_id}/hints
POST   /projects/{project_id}/complete
POST   /projects/{project_id}/reopen
POST   /projects/{project_id}/facts
POST   /projects/{project_id}/intents
POST   /projects/{project_id}/intents/{intent_id}/retry
POST   /projects/{project_id}/evidence
POST   /projects/{project_id}/hypotheses
GET    /projects/{project_id}/worker-runs
GET    /projects/{project_id}/runtime
```

`/view` 返回完整只读项目投影；`/events` 按不可变事件 ID 前向分页，适合 UI 和本地客户端增量轮询。

## TSec Agent Benchmark Platform

在 `.env` 中设置平台签发的配置：

```dotenv
BENCHMARK_BASE_URL=https://BENCHMARK_PLATFORM
BENCHMARK_TOKEN=replace-with-issued-task-token
BENCHMARK_TIMEOUT=30
BENCHMARK_AUTOMATION_INTERVAL=3
```

Benchmark token 只存在于 API/Dispatcher 控制面，不进入 Worker Prompt、项目 scope、Blackboard 事件或 workspace。平台 challenge 映射为 `bootstrap_enabled: false` 的 Reason-first 项目；错误候选成为 rejected Fact，正确候选成为 verified Fact，只有平台确认全部 flag 后项目才完成。

主要端点：

```text
GET  /benchmark/status
GET  /benchmark/challenges
GET  /benchmark/automation
POST /benchmark/automation/start
POST /benchmark/automation/stop
POST /benchmark/challenges/{unique_code}/start
POST /benchmark/challenges/{unique_code}/hint
POST /benchmark/challenges/{unique_code}/submit
POST /benchmark/challenges/{unique_code}/close
```

自动化状态持久保存在 Blackboard 中，可跨 API/Dispatcher 重启恢复。

## 数据目录与备份

`runs/` 不是临时目录，其中包含：

- SQLite Blackboard；
- API 和 Dispatcher 日志；
- 项目 workspace；
- Prompt、transcript、结构化 result；
- Evidence、manifest 和 graph snapshot；
- 服务进程状态文件。

`.env`、`runs/`、本地数据库、Python cache、构建输出和 Agent session 已在 `.gitignore` 中忽略。删除、移动或压缩 `runs/` 前必须先停止服务并备份。

## 项目结构

```text
slime / slime.cmd                 Linux / Windows 仓库入口
bin/slime / bin/slime.cmd         用户 PATH shim
dispatch.cairn.native.json        生产默认配置
dispatch.*.example.json           配置模板
src/slime_cairn/cli.py            跨平台 CLI 和 Linux 生命周期管理
src/slime_cairn/api.py            FastAPI、UI 和项目 API
src/slime_cairn/blackboard.py     SQLite Blackboard、租约、事件和读模型
src/slime_cairn/branch_policy.py  离散 SMA 分支评分和选择轨迹
src/slime_cairn/dispatcher.py     Worker 选择、并发、租约和停止控制
src/slime_cairn/scheduler.py      上下文导出、报告导入、校验和 Reason 循环
src/slime_cairn/native_agent.py   CLI 执行、健康检查和 JSON 解析
src/slime_cairn/prompting.py      Prompt group 加载、校验和渲染
src/slime_cairn/prompts/default/  阶段提示词和常驻 Worker 指令
src/slime_cairn/worker_manager.py 项目容器生命周期和配置挂载
src/slime_cairn/static/           本地可观测性 UI
scripts/check-e2e-result.py       数据库状态读取与 Evidence 完成检查
scripts/finish-docker.ps1         Windows 镜像构建
scripts/finish-docker.sh          Linux 镜像构建
worker/Dockerfile                 Kali Worker 镜像
tests/                            回归测试
docs/                             架构、CLI、配置、运维和验证文档
runs/                             持久运行数据，不属于源码
```

更详细的文件职责见 [PROJECT_LAYOUT.md](PROJECT_LAYOUT.md)。

## 测试与验证

Windows PowerShell：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests
python -m compileall -q src tests
python scripts\native-docker-smoke.py
slime doctor
```

Linux：

```sh
export PYTHONPATH=src
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
python3 scripts/native-docker-smoke.py
slime doctor
```

当前回归基线为 180 项测试通过；Windows 跳过 1 项 POSIX 专用测试。Linux 容器额外验证 POSIX launcher、用户级安装/卸载和 process-group 停止逻辑。

测试不会调用真实模型提供商，也不要求创建项目容器；`doctor` 和 `native-docker-smoke.py` 才会检查本机 Docker 与 Worker 镜像。

## 常见问题

### `slime` 不是可识别的命令

安装后重新打开终端。也可以直接使用仓库入口：

```text
# Windows
.\slime help

# Linux
sh ./slime help
```

Windows 安装目录是项目的 `bin`；Linux 安装器生成 `~/.local/bin/slime`，必要时在 `~/.profile` 中加入受管理的 PATH 块。

### `doctor` 报 `ready.service: false`

依次检查：

1. Docker Engine 是否运行。
2. `slime-cairn-kali:0.0.21` 是否存在。
3. 已启用 Worker 的 CLI 是否在镜像中。
4. `.env` 的模型地址、协议、模型名和密钥是否匹配。
5. `dispatch.cairn.native.json` 是否启用了一个能够覆盖 Explore 和 Reason 的健康 Worker 集合。

### 启用 Claude 后提示 `~/.claude` 不存在

默认 Claude Worker 是禁用的。启用前先在宿主机完成 Claude Code CLI 登录并确认 `~/.claude` 存在。禁用 Claude 时该目录不会成为启动条件。

### 修改源码后 UI 或 Dispatcher 仍是旧行为

```text
slime restart
slime ui
```

SMA 收敛计数和启动配置在 Dispatcher 进程启动时加载；修改源码或 dispatch 配置后需要重启服务。

### 项目失败后是否会丢失上下文

不会。失败 Intent、重试时间、错误、Worker Run、Evidence 和 workspace 都会保留。终态 Intent 可从 Inspector 手动重试。只有显式删除项目才会清理项目级数据。

### 是否必须设置 memory、CPU 和 PID 限制

不必须。默认与 Cairn 一致，不设置这些 quota。多个 Worker 共享项目容器，因此资源约束是项目容器级而不是 Worker 级；需要防止单项目占满宿主资源时再显式设置 `container.memory`、`container.cpus` 和 `container.pids_limit`。

## 安全边界

- 只对明确授权的 target 创建项目；IntentGate 会校验 target scope 和引用的 Fact。
- Worker 拥有项目容器内的 Shell 和工具能力，应把 Worker 镜像和网络 Profile 视为高权限执行环境。
- `raw-network` 和 `lab-network-admin` 会增加网络能力，只在任务需要且获得授权时使用。
- 默认无 cgroup quota 不代表无限宿主资源是安全的；多租户部署应显式配置限制。
- 模型密钥通过短生命周期 Docker env file 传入 Worker；不要写入持久 Prompt 或 Blackboard。
- Benchmark token 只留在控制面，不下发给 Worker。
- `delete` 会删除项目级持久数据，执行前先备份需要保留的 Evidence 和 workspace。

## 延伸文档

- [部署文档](docs/DEPLOYMENT.md)
- [使用文档](docs/USER_GUIDE.md)
- [CLI](docs/CLI.md)
- [配置说明](docs/CONFIGURATION.md)
- [架构说明](docs/ARCHITECTURE.md)
- [运维说明](docs/OPERATIONS.md)
- [验证说明](docs/VALIDATION.md)
- [项目结构](PROJECT_LAYOUT.md)
