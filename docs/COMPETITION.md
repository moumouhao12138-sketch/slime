# 比赛接入

## 1. CTF Agent API

在 `.env` 中设置 `AGENT_MATCH_BASE_URL` 与 `AGENT_MATCH_ACCESS_KEY` 后，Slime 的
本地 API 会将每道赛事题映射为一个独立 Project。AccessKey 不会进入 Worker 或
Blackboard。启动流程会自动处理环境初始化和端点就绪轮询；Agent 只接收题目下发的
附件、端点、账号信息和授权范围。

```sh
curl http://127.0.0.1:8000/agent-match/exercises
curl -X POST http://127.0.0.1:8000/agent-match/exercises/1001/start
```

候选答案由 Dispatcher 自动提交，也可以通过
`POST /agent-match/exercises/1001/submit` 手动提交。回收环境使用
`POST /agent-match/exercises/1001/recover`。

## 2. 模型网关

Worker 只应配置平台控制台生成的网关 URL，例如：

```dotenv
SLIME_CODEX_BASE_URL=https://<platform-host>/llm-gateway/proxy/e/<endpointCode>
SLIME_CODEX_API_KEY=<MODEL_API_KEY>
SLIME_CODEX_MODEL=<MODEL_NAME>
SLIME_COMPETITION_GATEWAY_ONLY=true
SLIME_CODEX_UPSTREAM_ENDPOINT=https://api.deepseek.com/responses
```

`SLIME_CODEX_UPSTREAM_ENDPOINT` 是在赛事控制台登记并由网关转发的完整上游 URL，
不是 Worker 实际连接地址。实际连接地址始终是 `SLIME_CODEX_BASE_URL`。不得把平台
AccessKey 填到任何 `*_API_KEY` 变量中。

### 协议选择

| Worker | 实际请求路径 | 可使用的白名单 URL 类型 |
| --- | --- | --- |
| Codex | `/responses` | 以 `/responses` 结尾的 URL |
| Pi（`openai-responses`） | `/responses` | 以 `/responses` 结尾的 URL |
| Pi（Chat Completions） | `/chat/completions` | 以 `/chat/completions` 结尾的 URL |
| Claude | `/v1/messages` | 以 `/v1/messages` 结尾的 URL |

默认 Codex Worker 必须选择以下完整端点之一：

```text
https://api.deepseek.com/responses
https://dashscope.aliyuncs.com/compatible-mode/v1/responses
https://qianfan.baidubce.com/v2/responses
https://ark.cn-beijing.volces.com/api/v3/responses
https://ark.cn-beijing.volces.com/api/coding/v3/responses
https://open.bigmodel.cn/api/v1/responses
https://tokenhub.tencentmaas.com/v1/responses
https://api.minimaxi.com/v1/responses
https://api.xiaomimimo.com/v1/responses
https://api.stepfun.com/v1/responses
```

网关通过相对路径转发请求。若登记的是 Qwen 的
`https://dashscope.aliyuncs.com/compatible-mode/v1/responses`，则其上游 Base URL 是
`https://dashscope.aliyuncs.com/compatible-mode/v1`；Worker 仍只填写平台下发的网关 URL。
不要在网关 URL 后自行追加 `/v1`，否则可能出现重复路径。

所有允许的 Chat Completions 与 Anthropic Messages 完整 URL 都由
`SLIME_COMPETITION_GATEWAY_ONLY` 启动校验；其清单与赛事白名单保持一致，定义于
`slime_cairn.workers.model_gateway`。
