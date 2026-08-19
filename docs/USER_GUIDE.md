# 使用文档

本文面向使用 Slime 创建项目、观察执行、处理失败和读取结果的用户。部署与升级见 [DEPLOYMENT.md](DEPLOYMENT.md)，参数说明见 [CONFIGURATION.md](CONFIGURATION.md)。

## 1. 打开控制面

启动服务：

```sh
./slime up
./slime ui
```

默认 UI 地址为 <http://127.0.0.1:8000/>。页面用于创建项目、浏览图结构、查看 Fact、Intent、Evidence、Worker Run 和事件，以及添加人工 Hint。

## 2. 创建项目

```sh
./slime new \
  --name demo \
  --target https://target.example/ \
  --goal "完成目标并给出可验证结果" \
  --start-mode growth
```

参数：

| 参数 | 说明 |
|---|---|
| `--name` | 项目名称；后续 CLI 用名称定位最新同名项目 |
| `--target` | 目标地址或目标标识，同时作为默认允许范围 |
| `--goal` | 明确的完成条件和预期结果 |
| `--start-mode` | `growth` 或 `direct` |

### 启动模式

| 模式 | 行为 | 适合场景 |
|---|---|---|
| `growth` | 先运行 Reason，由 Reason 创建多个 Explore Intent | 需要并行拆解和持续探索的任务 |
| `direct` | 先运行一次 Bootstrap；必要时再进入 Reason/Explore | 目标明确、可能一次完成的任务 |
| `hybrid` | 先运行 Bootstrap 建立初始事实，再强制进入 Reason 创建 Explore 分支 | 需要先摸清入口、再多方向并行探索的任务 |

创建成功后项目状态为 `running`，Dispatcher 会自动发现并调度，无需单独启动 Worker。

## 3. 查看项目

列出项目：

```sh
./slime list
```

查看摘要：

```sh
./slime status --name demo
```

摘要包含项目状态、Fact 数量、各状态 Intent 数量、活动租约、等待重试数量和最终结果。

查看完整运行态：

```sh
./slime runtime --name demo
```

`runtime` 输出 JSON，包括 Dispatcher 状态、Worker 健康与容量、当前任务、错误和恢复信息。需要机器可读输出时，其他业务命令也可以使用 `--json`：

```sh
./slime list --json
./slime status --name demo --json
```

## 4. 理解执行过程

### Bootstrap

`direct` 模式的首次任务。它建立初始事实，并可以在证据充分时直接完成项目。

### 混合启动

`hybrid` 模式先执行一次 Bootstrap，但 Bootstrap 只负责建立初始事实，不直接结束项目；随后由 Reason 读取这些事实并创建黏菌式 Explore 分支。

### Explore

Explore 对一个明确 Intent 收集证据、验证假设并生成结构化报告。同一项目可以并发运行多个 Explore，但这些任务共享同一个项目 Worker 容器和 workspace。

### Reason

Reason 读取当前 Blackboard 图，负责：

- 合并跨分支事实和假设；
- 创建后续 Intent；
- 判断是否需要继续探索；
- 在证据充分时形成 Completion。

一次 Reason 最多创建 `tasks.reason.max_intents` 个新 Intent。该值控制分支生成数量，不是并发数。

## 5. Blackboard 对象

| 对象 | 作用 |
|---|---|
| `Project` | 目标、范围、状态和完成条件 |
| `Fact` | 已通过证据门槛的结构化事实 |
| `Hypothesis` | 尚待验证的判断及其支持证据 |
| `Intent` | 可租约、可调度、可重试的工作分支 |
| `Hint` | 人工补充的持久判断，会触发重新规划 |
| `Evidence` | 文件、命令输出或外部证据引用 |
| `Worker Run` | 一次模型执行的状态、报告和错误 |
| `Completion` | 引用有效 Fact 的最终结论 |
| `Event` | 按顺序记录的状态变化和调度决策 |

系统不会只保留最终回答。事实、分支、错误和人工 Hint 会共同构成可审计的项目记忆。

## 6. 分支与营养评分

Intent 的营养值反映预期价值、新颖度、成本、风险和已有证据。默认 `sma_discrete` 策略进一步综合：

```text
fitness = 0.60 * 当前营养
        + 0.20 * 分支历史最佳营养
        + 0.15 * 当前强度
        + 0.05 * 新颖度
        - 失败惩罚
```

调度策略只对已经满足项目状态、重试时间、租约、Worker 能力和容量条件的 Intent 排序。最终 claim 仍由 SQLite 原子操作完成。

在候选较多时，策略会以逐步收敛的概率探索低排名分支，避免一直执行同一条高分路径。每次选择都会写入事件流，包含评分、排名、选择模式和随机种子摘要。

## 7. 并发如何计算

实际并发取以下上限的最小可用值：

```text
全局 runtime.max_workers
单项目 runtime.max_project_workers
Worker 条目的 workers[].max_running
当前健康且支持该任务类型的 Worker 容量
```

默认值为：全局 `8`、单项目 `4`、同时调度项目 `3`。默认 Codex 条目的 `max_running` 为 `8`，因此单个项目通常最多同时执行 `4` 个任务。

`runtime.reason_batch_size` 表示积累多少个完成信号后优先唤醒 Reason，不是分支数，也不是并发数。

## 8. 暂停、停止与恢复

暂停项目：

```sh
./slime pause --name demo
```

`stop` 与 `pause` 等价：

```sh
./slime stop --name demo
```

暂停会将项目置为 `stopped`，取消活动任务、释放租约并停止项目 Worker 容器；Blackboard 和 workspace 保留。

恢复同一项目：

```sh
./slime resume --name demo
```

恢复后继续使用原 Project、Fact、Intent、Evidence 和 workspace。已完成项目不能通过 `resume` 直接恢复，应在 Web UI 中填写新的反馈并执行 reopen。

## 9. 处理失败节点

先读取状态和 Dispatcher 日志：

```sh
./slime status --name demo
./slime runtime --name demo
./slime logs -f dispatcher
```

常见原因：

- 模型 endpoint 暂时断开；
- API key 或模型名错误；
- 模型返回内容不符合结构化报告协议；
- Worker CLI 启动或健康检查失败；
- 任务超时；
- 同一错误连续出现并触发项目临时暂停。

系统默认允许每个 Intent 尝试 `6` 次，并在失败后使用 `30` 秒到 `1800` 秒的退避。修复上游问题后，可以立即重新排队一个 Intent：

```sh
./slime retry --name demo --intent-id intent_xxx
```

`retry` 会清除该 Intent 当前的等待时间并重新置为待调度状态，但不会删除既有失败历史。

## 10. 人工 Hint

在 Web UI 的项目详情中添加 Hint，可以补充已知事实、纠正方向或要求验证新的路径。Hint 持久写入 Blackboard，并向 Reason 发出重新规划信号。

有效 Hint 应描述可操作的新信息，例如：

- 已确认的目标属性；
- 应优先验证的假设；
- 已排除的路径；
- 对最终结果格式的补充要求。

不要用 Hint 重复已有目标；重复信息会增加上下文但不会增加有效分支。

## 11. 删除项目

```sh
./slime delete --name demo
```

删除是异步操作。项目先进入 `deleting`，Dispatcher 随后排空任务并删除：

- 项目 Worker 容器；
- `slime-workspaces` 中该项目的子目录；
- Blackboard 中该项目的数据。

删除完成后无法通过 `resume` 恢复。

## 12. CLI 参考

| 命令 | 作用 |
|---|---|
| `help` | 显示业务 CLI 参数 |
| `new` | 创建项目 |
| `list` | 列出项目 |
| `status` | 查看项目摘要和结果 |
| `runtime` | 查看完整运行态 JSON |
| `pause` | 暂停项目 |
| `stop` | 暂停项目，与 `pause` 等价 |
| `resume` | 恢复已停止项目 |
| `delete` | 异步删除项目 |
| `retry` | 立即重试指定 Intent |

通用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--name` | `ctf-test` | 项目名称 |
| `--base-url` | `http://127.0.0.1:8000` | API 地址 |
| `--timeout` | `10` | HTTP 请求超时，单位秒 |
| `--json` | 关闭 | 输出机器可读 JSON |

根启动器帮助列出部署命令：

```sh
./slime help
```

业务 CLI 的完整参数可在 API 容器内查看：

```sh
docker compose exec -T api slime help
```
