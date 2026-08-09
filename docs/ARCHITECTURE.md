# 架构说明

Slime Cairn 是单机、多项目、持久化的 Agent 调度系统。它把控制面、项目执行环境和持久化数据分开，通过结构化协议将模型输出写入 Blackboard，并由 Reason 持续创建或收敛探索分支。

## 1. 设计目标

- 项目、事实、分支、证据和失败历史可以跨服务重启保留；
- 不同项目使用不同容器和 workspace；
- 同一项目可以并发执行多个任务并共享上下文；
- 分支生成、分支排序和任务并发分别配置；
- 模型输出必须通过结构化解析与领域校验后才能改变 Blackboard；
- 部署由 Docker Compose 和预构建镜像承担。

## 2. 总体拓扑

```text
                        Docker Compose
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  Browser / CLI                                               │
│       |                                                      │
│       v                                                      │
│  ┌──────────────┐       ┌────────────────────┐              │
│  │ API          │       │ Dispatcher         │              │
│  │ FastAPI + UI │       │ Scheduler + Docker │              │
│  └──────┬───────┘       └───────┬────────────┘              │
│         |                        |                            │
│         └──────────┬─────────────┘                            │
│                    v                                          │
│              slime-data                                       │
│              SQLite Blackboard                                │
│                                                               │
│  worker-image：一次性确认 Worker 镜像可用                     │
└────────────────────────────┬─────────────────────────────────┘
                             | Docker Socket
                             v
                 ┌─────────────────────────┐
                 │ 项目 Worker 容器         │
                 │ Codex / Pi / Claude CLI │
                 │ /workspace              │
                 └────────────┬────────────┘
                              v
                 slime-workspaces/<project-id>
```

## 3. Compose 服务

### API

API 容器运行 FastAPI，职责包括：

- 提供 Web UI 与 OpenAPI；
- 创建、查询、暂停、恢复和删除项目；
- 提供 Fact、Intent、Hint、Evidence、Completion 和事件接口；
- 输出 Dispatcher 与项目运行态；
- 提供可选 Benchmark 控制接口。

API 只挂载 `slime-data`，不直接管理项目容器。

### Dispatcher

Dispatcher 是执行控制中心，职责包括：

- 从 Blackboard 发现 `running` 项目；
- 为项目创建调度实例；
- 选择可运行 Intent 和可用 Worker；
- 原子 claim 租约并维持心跳；
- 创建、复用、停止或重建项目 Worker 容器；
- 调用 Agent CLI，解析结构化报告；
- 将通过校验的结果写回 Blackboard；
- 处理重试、退避、暂停、完成和删除。

Dispatcher 挂载：

- `slime-data:/data`；
- `slime-workspaces:/workspaces`；
- `${SLIME_DISPATCH_FILE:-./dispatch.json}:/app/dispatch.json:ro`；
- `/var/run/docker.sock:/var/run/docker.sock`。

### worker-image

`worker-image` 不承载任务。它在控制面启动前完成镜像拉取或构建检查，然后以退出码 `0` 结束。Dispatcher 创建的实际项目容器使用同一个 Worker 镜像。

## 4. 控制面与执行面

控制面由 `api` 和 `dispatcher` 组成。二者使用同一个应用镜像，但运行不同入口，并通过 `slime-data` 中的 Blackboard 交换状态。

执行面由 Dispatcher 动态创建的项目 Worker 容器组成。每个项目只有一个持久容器：

```text
Project A -> slime-<project-a-id> -> workspace 子卷 A
Project B -> slime-<project-b-id> -> workspace 子卷 B
```

Codex、Pi、Claude 等 Worker 名称表示 Agent 适配器和调度容量，不表示每个适配器各有一个容器。一个项目内的不同任务通过 `docker exec` 进入同一个项目容器。

因此隔离边界是项目，不是项目内的并发任务：

- 不同项目拥有不同容器和 workspace 子卷；
- 同一项目的任务共享文件、进程环境和容器网络；
- 项目内并发任务应避免同时覆盖同一路径或修改同一全局工具状态。

## 5. Workspace 与容器生命周期

`slime-workspaces` 是 Docker named volume。Dispatcher 先创建项目子目录，再用 Docker 的 `volume-subpath` 将该目录挂载为项目容器的 `/workspace`。

项目容器生命周期：

1. 项目进入 `running`；
2. Dispatcher 计算容器名、Profile 和 workspace 子路径；
3. 容器不存在时创建并启动；
4. 容器配置匹配时直接复用；
5. 镜像、网络或资源配置变化时重建容器，但保留 workspace；
6. 项目暂停或完成时停止容器；
7. 项目删除时移除容器和对应 workspace 子目录。

默认容器属性：

- 用户 `65532:65532`；
- 只读根文件系统；
- `/tmp` 为临时可写文件系统；
- `no-new-privileges`；
- 丢弃全部 capability；
- `standard` Profile 使用 `bridge` 网络；
- 不设置 CPU、内存和 PID 上限。

资源限制是项目容器级配置。同一项目的多个任务共同使用该容器获得的资源。

## 6. Blackboard

Blackboard 使用 SQLite 保存长期状态。核心对象：

| 对象 | 关键职责 |
|---|---|
| `Project` | 目标、范围、启动模式和生命周期状态 |
| `Fact` | 证据支持的事实及其记忆状态 |
| `Hypothesis` | 待验证判断和支持关系 |
| `Intent` | 分支目标、营养、强度、租约和失败状态 |
| `Hint` | 人工持久输入与 Reason 唤醒信号 |
| `Evidence` | 可追溯证据引用和元数据 |
| `Worker Run` | 一次 Agent 执行及模型会话信息 |
| `Completion` | 引用 Blackboard Fact 的最终结果 |
| `Event` | 状态变化和调度决策的有序记录 |

API 与 Dispatcher 不通过进程内对象共享状态。任何需要跨服务生效的事实都必须写入 Blackboard。

## 7. 三阶段任务协议

### Bootstrap

仅在 `direct` 启动模式下创建。Bootstrap 负责首次观察、建立初始 Fact，并允许简单项目直接生成 Completion。

### Explore

Explore 消费一个 Intent，执行证据收集或假设验证。多个 Explore 可以并发执行。

### Reason

Reason 读取完整项目图，合并分支结果、创建后续 Intent 或生成 Completion。一个项目同一时刻只运行一个 Reason，避免多个规划者同时改写分支图。

Reason 报告一次最多创建 `tasks.reason.max_intents` 个 Intent。这个字段只限制单次扩展宽度；并发由 Dispatcher 容量配置控制。

## 8. 一次任务的数据流

```text
1. Dispatcher 读取 runnable Intent
2. 分支策略给候选排序
3. Scheduler 选择健康且有容量的 Worker
4. Blackboard 原子 claim Intent 和租约
5. Worker 适配器渲染提示词与 graph.yaml
6. Dispatcher 通过 docker exec 启动 Agent CLI
7. Agent 在 /workspace 中执行并返回结构化报告
8. 协议层解析报告
9. 领域层校验 Fact、Hypothesis、Intent、Completion
10. Blackboard 原子写入结果并释放租约
11. 完成信号触发后续 Reason
```

执行超时或取消时，运行器会终止对应容器进程组，避免只关闭宿主 Docker 客户端而遗留任务进程。

## 9. 调度与容量

DispatcherService 管理多项目调度，每个活动项目拥有一个项目级 Dispatcher，并共享全局容量计数器。

```text
全局上限              runtime.max_workers
项目上限              runtime.max_project_workers
活动项目上限          runtime.max_running_projects
适配器上限            workers[].max_running
单次分支生成上限      tasks.reason.max_intents
```

前四项约束运行容量，最后一项约束 Reason 输出。它们相互独立。

Intent 使用短租约和心跳。只有状态、重试时间、范围、Worker 能力和容量均满足的候选才可被 claim。Dispatcher 中断后，过期租约可以由后续实例回收。

## 10. 分支策略

默认策略为 `sma_discrete`。它将文本 Intent 视为离散候选，不把任务强行映射为连续数值向量。

适应度由以下部分组成：

```text
fitness = 0.60 * nutrient
        + 0.20 * branch_nutrient
        + 0.15 * strength
        + 0.05 * novelty
        - min(0.30, failure_streak * 0.075)
```

其中营养、分支营养和强度在当前候选集中归一化。策略在前若干次选择中保留探索概率，并逐步从 `0.12` 收敛到 `0.03`。随机序列由配置种子、Project ID、选择序号和候选 ID 共同确定，因此每次选择都可以审计。

## 11. 提示词与协议

提示词不是集中写在 Python 字符串中，而是作为包资源存放在：

```text
src/slime_cairn/protocol/prompts/default/
```

`AGENTS.md` 提供项目常驻指令，阶段模板提供当前任务、图快照和输出约束。协议层负责：

- 校验提示词组及必要占位符；
- 渲染 Bootstrap、Explore、Reason 提示词；
- 解析 Agent 的结构化报告；
- 限制报告项目数和上下文规模；
- 将协议对象交给领域验证层。

模型文本只有在解析和校验成功后才会成为持久状态。

## 12. Python 包分层

```text
src/slime_cairn/
├─ server/
│  ├─ api.py                HTTP API 与静态 UI
│  └─ blackboard.py         SQLite 持久化
├─ dispatcher/
│  ├─ main.py               Dispatcher 容器入口
│  ├─ service.py            多项目生命周期
│  ├─ loop.py               项目调度循环与 Worker Pool
│  └─ scheduler.py          claim 与结果写入
├─ domain/
│  ├─ models.py             核心数据模型
│  ├─ branch_policy.py      SMA 离散分支策略
│  ├─ nutrients.py          Intent 营养评分
│  ├─ context.py            上下文构建
│  ├─ workspace.py          项目文件布局
│  └─ validation.py         领域门槛
├─ workers/
│  ├─ manager.py            项目容器生命周期
│  ├─ execution.py          Docker exec 与取消
│  ├─ native.py             Agent CLI 适配
│  ├─ health.py             模型端点健康检查
│  └─ factory.py            项目运行时装配
├─ protocol/
│  ├─ contracts.py          结构化报告协议
│  ├─ prompting.py          提示词加载与渲染
│  └─ prompts/              常驻与阶段提示词
├─ integrations/benchmark/  可选评测平台集成
└─ cli.py                   HTTP API 客户端
```

依赖方向遵循：入口层调用应用与领域能力，领域模型不依赖 Web UI 或 Compose；Worker 适配器不直接修改 Blackboard，而通过调度与验证流程提交结果。

## 13. 持久化与恢复边界

- `slime-data` 保存控制面事实，是项目状态的权威来源；
- `slime-workspaces` 保存任务文件和项目级 Agent home；
- 项目容器可以重建，不作为唯一数据来源；
- 应用镜像和 Worker 镜像可以重新拉取；
- `.env` 和 `dispatch.json` 应与两个数据卷一起纳入部署备份。

完整恢复要求 Blackboard、workspace 和配置来自同一备份时间点。
