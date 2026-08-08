# Slime Cairn 使用文档

本文面向使用 Slime 创建项目、观察分支、补充 Hint、处理失败和读取结果的用户。环境安装、systemd、备份和升级请阅读 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 1. 开始使用

确认管理员已经完成部署并且：

```text
slime doctor
```

报告中的 `ready.service` 为 `true`。启动并打开 UI：

```text
slime up
slime ui
```

默认地址：

- UI：`http://127.0.0.1:8000/`
- API 文档：`http://127.0.0.1:8000/docs`

如果 `slime` 还没有加入 PATH：

```text
# Windows
.\slime ui

# Linux
sh ./slime ui
```

## 2. 基本概念

### Project

一个 Project 表示一个持续任务，包含名称、目标、Goal、授权范围、状态、Blackboard 和项目 workspace。

### Fact

经过校验并进入 Blackboard 的事实。Fact 应该能够追溯到 Evidence 或明确的系统来源。

### Hypothesis

尚未完全确认的判断，包含支持 Fact 和下一步验证方向。

### Intent

一个可执行的具体分支，例如“枚举登录入口”“验证某个参数是否影响响应”或“核对候选答案”。Dispatcher 对 Intent 进行排序、claim 和分配 Worker。

### Hint

用户写入 Blackboard 的持久提示。Hint 不会覆盖项目 Goal，而是让 Reason 在下一轮重新考虑方向、约束或新信息。

### Evidence

支持 Fact 或结论的文件、命令输出、外部响应和引用。

### Worker Run

一次具体的 Bootstrap、Explore 或 Reason 执行记录，包含 Worker、状态、报告、错误和产物路径。

### Completion

Reason 基于有效 Fact 写出的最终结论。Completion 不是任意 Worker 的自然语言回答，必须经过项目完成路径写入 Blackboard。

## 3. 创建项目

### 3.1 从 UI 创建

1. 打开 UI。
2. 点击左侧项目区域中的“新建项目”。
3. 填写项目名称。
4. 填写目标 Target。
5. 填写 Goal，明确希望得到的结果和验收标准。
6. 选择启动模式。
7. 按需添加一个或多个初始 Hint。
8. 确认创建。

Target 会进入项目授权范围。后续由 Reason 创建的 Intent 必须通过 scope 校验，不能任意扩展到未授权目标。

### 3.2 从 CLI 创建

Windows 和 Linux 都支持以下参数风格：

```text
slime new -Name "analysis-001" -Target "https://target.example" -Goal "收集证据并返回可验证结论。" -StartMode growth
```

Linux/Python CLI 也支持标准长参数：

```sh
slime new --name analysis-001 \
  --target https://target.example \
  --goal "收集证据并返回可验证结论。" \
  --start-mode growth
```

### 3.3 如何写 Goal

Goal 应包含：

- 要解决的问题；
- 允许分析的目标；
- 需要的证据标准；
- 最终输出格式；
- 明确的停止或完成条件。

较好的示例：

```text
分析 https://target.example 的公开接口，记录可复现请求和响应证据，
确认实际可用的 API 路径，并在最终结论中引用对应 Fact。
```

不建议只写“看看这个网站”或“帮我解决”，因为 Reason 难以判断完成条件。

## 4. 选择启动模式

### growth

默认黏菌分支模式：

1. 创建 Origin 和 Goal 上下文；
2. Reason 先读取全图；
3. Reason 创建多个相对独立的 Explore Intent；
4. Dispatcher 并发探索；
5. 新 Fact 再触发 Reason 整合。

适合范围较大、需要多方向探索或证据交叉验证的任务。

### direct

Cairn 直推模式：

1. Bootstrap 先直接处理任务；
2. 简单任务可能直接产生足够事实；
3. 需要进一步探索时再进入 Reason/Explore 循环。

适合目标明确、路径较短、一次初始探测可能完成的任务。

启动模式只影响初始调度路径，不改变后续 Blackboard、Evidence 和校验规则。

## 5. 项目页面

### 5.1 项目列表

左侧列表显示项目名称、状态和目标。可选择项目进入详情，也可以勾选多个项目执行批量删除。

常见状态：

| 状态 | 含义 |
|---|---|
| `running` | Dispatcher 可以为项目分配工作 |
| `stopped` | 项目停止，持久数据保留 |
| `completed` | 已产生 Completion |
| `pending` | Intent 等待调度 |
| `running` Intent | Worker 已持有租约并执行 |
| `retry_wait` | 失败后等待重试时间到达 |
| `failed` | 自动重试已耗尽 |
| `dormant` | 不参与当前调度的保留记录 |

### 5.2 成果视图

项目默认打开“成果”视图，优先展示：

- 当前 Goal 或最终答案；
- Completion 引用的支持 Fact；
- 已产生有效结果的分支；
- 正在运行的分支；
- 已尝试但没有新增结果的折叠分支；
- 有效分支、活动分支和事实数量。

项目仍在运行时，顶部显示任务进行状态；完成后显示 Reason 最终结论。

### 5.3 图视图

切换到“图”后可以查看 Fact、Intent、Hypothesis 和 Completion 的因果关系。典型路径：

```text
Fact -> Intent -> Fact / Hypothesis -> Completion
```

点击节点后，右侧 Inspector 显示该记录的完整字段、来源、状态和引用。

### 5.4 Inspector

Inspector 用于检查单个节点或项目状态。对 Intent，重点关注：

- objective 和 target；
- nutrient、strength、novelty；
- status、attempts 和 failure streak；
- retry time 和 last error；
- source Fact、branch root 和 predecessor；
- Worker Run 和 Evidence。

失败或等待重试的 Intent 可以从 Inspector 执行“立即重试”。这会重开同一个 Intent，不会创建重复任务，也不会删除历史错误。

### 5.5 活动时间线

活动时间线按不可变事件 ID 增量更新，常见事件包括：

- 项目创建、停止、恢复、完成和重新打开；
- Intent 创建、claim、启动、完成和重试；
- Worker health 和失败分类；
- `growth.branch_selected` 分支选择；
- Reason 启动、完成或失败暂停；
- Hint 添加；
- Benchmark 提示、提交和验证。

点击时间线记录可查看事件 payload。事件流适合解释“为什么这个分支被运行”以及“失败后发生了什么”。

## 6. 观察运行状态

### UI

项目页面显示活动 Bootstrap、Explore 或 Reason Worker、已运行时间和最近租约心跳。心跳表示 Worker 仍持有任务，不代表 UI 会流式展示模型思维或每条工具命令。

### CLI

```text
slime list
slime status -Name analysis-001
slime runtime -Name analysis-001
slime logs -Follow
```

查看全部服务日志：

```text
slime logs -Log all -Tail 200
```

日志类型：

- `dispatcher`：Worker 分配、Reason、重试和项目状态。
- `server`：API 请求、UI 和控制面错误。
- `all`：同时查看两类日志。

## 7. 理解分支选择

默认使用离散 SMA 策略。它不会永远严格选择 nutrient 最大的 Intent，而是在较高适应度分支和少量探索之间平衡。

适应度包含：

- 当前 Intent nutrient：60%；
- 同一 branch root 的历史最佳 nutrient：20%；
- strength：15%；
- novelty：5%；
- failure streak 惩罚：每次 0.075，最多 0.30。

`growth.branch_selected` 事件中常用字段：

| 字段 | 含义 |
|---|---|
| `policy` | 当前策略，通常为 `sma_discrete` |
| `selection_mode` | `sma_attract`、`sma_explore` 或 `nutrient` |
| `selected_intent_id` | 实际被 claim 的 Intent |
| `policy_selected_intent_id` | 策略最初选择的 Intent |
| `fallback` | 是否因 Worker 能力/容量而回退 |
| `selected_rank` | 被策略选中的适应度排名 |
| `exploration_probability` | 本轮探索概率 |
| `random_draw` | 确定性随机值 |
| `seed_digest` | 随机种子摘要，不是密钥 |
| `candidates` | 候选评分组成和排名 |

探索概率在前 24 次选择中从 12% 收敛到 3%。Dispatcher 重启会重置进程内选择计数，但已经发生的选择保留在事件流中。

## 8. 添加 Hint

项目页面点击“添加 Hint”，填写策略提示或新的判断。

适合写入 Hint 的内容：

- 新发现的入口、约束或已知事实；
- 希望优先验证的方向；
- 明确排除某条重复路线；
- 外部人工确认结果；
- 对当前 Completion 的修正建议。

示例：

```text
已人工确认 /api/v1 需要登录，不要继续匿名枚举；优先检查公开的 /health 和 /docs。
```

Hint 会持久进入 Blackboard，并唤醒 Reason。它不是即时发送给某个正在运行 Worker 的聊天消息；活动 Worker 完成后，下一轮调度会读取更新后的完整图。

## 9. 处理失败与重试

Intent 默认最多自动尝试 6 次。前五次失败后等待：

```text
30s -> 60s -> 120s -> 240s -> 480s
```

第六次失败后进入 `failed`。以下内容仍会保留：

- attempts 和 failure streak；
- last error 和错误分类；
- 每次 Worker Run；
- transcript、result 和 Evidence；
- 原 Intent 的 objective 和上下文。

### 何时立即重试

适合立即重试：

- 模型端点短暂恢复；
- 已修复 API key 或代理；
- Worker 镜像已经重建；
- 人工确认错误不是任务本身造成。

不适合反复立即重试：

- 认证或配额仍然错误；
- 同一 Prompt 持续触发提供商策略拒绝；
- 任务超时但没有缩小目标；
- 容器持续出现资源耗尽。

此时应先修复配置、降低并发、增加资源、添加 Hint 或调整 Goal。

## 10. 暂停、恢复和重新打开

### 暂停/停止

UI 点击“停止”，或运行：

```text
slime pause -Name analysis-001
```

系统会：

- 将 Project 设为 `stopped`；
- 取消活动 native CLI；
- 围栏并释放活动 Intent/Reason lease；
- 记录中断 Worker Run；
- 保留数据库、容器和 workspace。

由人工停止造成的释放不会增加 Intent failure count。

### 恢复

UI 点击“继续”，或运行：

```text
slime resume -Name analysis-001
```

项目从原 Blackboard 和 workspace 继续，不重新创建项目。

### 重新打开已完成项目

完成项目的页面显示“重新打开”。填写新的反馈或目标调整后：

- Completion 历史保留；
- 反馈写入 Blackboard；
- Project 返回运行状态；
- Reason 基于完整历史重新规划。

Benchmark 托管项目的生命周期由评测平台控制，普通的继续和重新打开按钮可能不可用。

## 11. 删除项目

删除是不可逆的项目级操作。

单项目删除要求输入完整项目名确认。批量删除要求选择项目并再次确认。系统会异步：

- 停止活动 Worker；
- 删除项目容器；
- 删除项目 workspace；
- 删除项目 Blackboard 记录和相关运行数据。

删除前导出或备份需要保留的 Evidence、transcript 和结果。`pause`/`stop` 才是可恢复操作。

CLI：

```text
slime delete -Name analysis-001
```

## 12. 读取结果与证据

最终结果应同时检查：

1. Completion 描述是否回答 Goal；
2. Completion 引用的 Fact 是否存在；
3. Fact 的 Evidence 引用是否可访问；
4. Worker Run 是否完成而不是失败或 conclude fallback；
5. 是否存在相互冲突的 Hypothesis 或 rejected Fact；
6. Benchmark 项目是否得到平台验证，而不只是模型声称正确。

不要只复制某个 Explore Worker 的自然语言输出作为最终结论。成果视图和 Completion 才是 Reason 汇总后的项目结果。

项目 workspace 中常见产物：

```text
context/graph.yaml
pods/<worker>/<task>/prompt.*
pods/<worker>/<task>/transcript.*
pods/<worker>/<task>/result.*
pods/<worker>/<task>/evidence/*
```

## 13. 推荐工作流

### 13.1 多方向调查

1. 使用 `growth` 创建项目。
2. Goal 写清证据标准和完成条件。
3. 观察 Reason 创建的首批 Intent。
4. 在成果视图检查有效分支。
5. 对重复或错误方向添加 Hint。
6. 等待 Reason 汇总 Completion。
7. 人工检查引用 Fact 和 Evidence。

### 13.2 短任务直推

1. 使用 `direct` 创建项目。
2. 观察 Bootstrap 是否产生足够 Fact。
3. 若 Bootstrap 失败，检查 Worker Run 和重试状态。
4. 若任务扩展，允许 Reason 创建 Explore 分支。

### 13.3 修复模型配置后恢复

1. `slime pause -Name <项目>`。
2. 修复 `.env` 或 dispatch JSON。
3. `slime doctor`。
4. 配置或源码变化时执行 `slime restart`。
5. `slime resume -Name <项目>`。
6. 对确实需要立即运行的失败 Intent 使用 Inspector 重试。

### 13.4 修改 Goal 后继续

运行中的项目不要通过重复创建同名项目来修改方向。优先添加 Hint；已完成项目使用“重新打开”并填写反馈。

## 14. TSec Agent Benchmark

管理员配置 `BENCHMARK_BASE_URL` 和 `BENCHMARK_TOKEN` 后，顶部显示“评测平台”。

界面支持：

- 刷新 challenge；
- 启动 challenge 并创建映射项目；
- 打开已有映射项目；
- 在确认分数代价后请求 Hint；
- 手动提交候选 Flag；
- 关闭平台实例；
- 开始或停止并发自动化。

自动化默认维持最多三个未完成 challenge，完成后关闭实例并补充下一题。状态保存在 Blackboard 中，服务重启后可以恢复。

评测项目的完成条件是平台报告 `correct_flag_count == flag_count`，不是 Worker 仅返回一个候选字符串。错误候选会保存为 rejected Fact，避免后续分支重复提交。

Benchmark token 不会进入 Worker Prompt 或项目 workspace。

## 15. API 使用

FastAPI 文档：`http://127.0.0.1:8000/docs`。

读取项目：

```text
GET /projects
GET /projects/{project_id}
GET /projects/{project_id}/view
GET /projects/{project_id}/events?after_id=0&limit=100
GET /projects/{project_id}/worker-runs
GET /projects/{project_id}/runtime
```

写入 Hint：

```http
POST /projects/{project_id}/hints
Content-Type: application/json

{
  "content": "优先验证公开文档入口，停止重复扫描登录页面。",
  "creator": "human"
}
```

强制重试 Intent：

```text
POST /projects/{project_id}/intents/{intent_id}/retry
```

事件分页使用上一页最后一个事件的 ID 作为下一次 `after_id`，不需要反复下载完整时间线。

## 16. CLI 参数速查

| 参数 | 默认值 | 作用 |
|---|---|---|
| `-Name` / `--name` | `ctf-test` | 项目名 |
| `-Target` / `--target` | 空 | 创建项目的目标 |
| `-Goal` / `--goal` | 内置默认 Goal | 项目目标 |
| `-StartMode` / `--start-mode` | `growth` | `growth` 或 `direct` |
| `-Config` / `--config` | `dispatch.cairn.native.json` | dispatch 配置 |
| `-Database` / `--database` | `runs/slime-server.db` | SQLite 路径 |
| `-WorkspacesRoot` / `--workspaces-root` | `runs/slime-workspaces` | workspace 根目录 |
| `-Profile` / `--profile` | `raw-network` | 容器网络 Profile |
| `-BindHost` / `--bind-host` | `127.0.0.1` | API 监听地址 |
| `-Port` / `--port` | `8000` | API 端口 |
| `-Log` / `--log` | `dispatcher` | `dispatcher`、`server`、`all` |
| `-Tail` / `--tail` | `80` | 日志尾部行数 |
| `-Follow` / `--follow` | false | 持续跟踪日志 |
| `-NoDispatcher` / `--no-dispatcher` | false | `up` 时只启动 API |
| `-DryRun` / `--dry-run` | false | 只显示计划，不执行启动/PATH 变更 |

使用非默认端口、数据库或配置时，后续命令必须传入相同参数，否则 CLI 会连接到默认实例。

## 17. 数据安全

- `runs/` 是持久数据，不是临时缓存。
- `.env` 包含密钥，不要上传或共享。
- 删除项目前先备份需要的 Evidence 和 workspace。
- 服务重启不会删除项目；项目容器重建也不会删除 bind-mounted workspace。
- 不要同时运行两个 Dispatcher 写入同一个数据库。
- 不要在 Slime 运行时手工修改 SQLite 表或移动 workspace。

## 18. 常见问题

### 为什么项目看起来没有实时输出

Slime 展示任务状态和租约心跳，不流式展示模型隐藏推理或每条工具命令。Worker 完成一个结构化报告后，结果、Evidence 和 Worker Run 才进入 Blackboard。

### 为什么高 nutrient 分支没有先运行

默认 SMA 策略是加权选择，不是严格排序；也可能因为 Worker 不支持该任务、Worker 已满、Intent 在 retry wait 或原子 claim 失败而回退。查看 `growth.branch_selected` 事件。

### 为什么项目停止后容器还存在

容器是项目持久执行环境。`stop`/`pause` 保留容器和 workspace，以便恢复；删除项目才清理项目容器和数据。

### 为什么多个 Worker 会看到彼此文件

同一项目的 Worker 设计上共享一个容器和 `/workspace`，便于复用工具、证据和上下文。这不是任务级强隔离；需要强隔离时应拆成不同 Project。

### 是否需要配置 memory、CPU、PID 限制

不是必须。默认不限制；共享宿主机或高并发环境建议由管理员配置项目容器级 quota。

### Hint 会立即中断当前 Worker 吗

不会。Hint 持久写入 Blackboard 并唤醒下一轮 Reason；活动 Worker 仍按当前任务完成，除非用户停止项目。

### `retry_wait` 是否表示死锁

不是。它表示 durable backoff 尚未到期。Inspector 显示下一次重试时间；只有确认外部问题已修复时才使用立即重试。

## 19. 相关文档

- [README](../README.md)
- [部署文档](DEPLOYMENT.md)
- [CLI](CLI.md)
- [配置说明](CONFIGURATION.md)
- [架构说明](ARCHITECTURE.md)
- [运维说明](OPERATIONS.md)
- [验证说明](VALIDATION.md)
