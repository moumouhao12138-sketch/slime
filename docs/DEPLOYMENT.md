# Slime Cairn 部署文档

本文面向负责安装、配置、启动、升级、备份和故障恢复的运维人员。日常创建项目和使用 UI 请阅读 [USER_GUIDE.md](USER_GUIDE.md)。

## 1. 部署模型

Slime Cairn 当前采用单机多项目模型：

```text
一台 Windows/Linux 宿主机
  +-- 一个 FastAPI 控制面
  +-- 一个 Dispatcher Service
  +-- 一个 SQLite Blackboard
  +-- 多个项目专属 Docker 容器
  +-- 多个项目专属宿主 workspace
```

重要边界：

- Slime 只支持 Docker 容器执行后端，不支持直接在宿主机执行 Worker 工具。
- 一个项目对应一个持久容器；不同项目容器相互独立。
- 同一项目内多个 Worker 共享容器、`/workspace` 和容器进程环境，不是强隔离。
- SQLite、服务日志、workspace 和 Evidence 默认位于 `runs/`。
- API 默认绑定 `127.0.0.1:8000`，没有面向公网的内置身份认证。

推荐部署规模是单机、可信用户、本地或受控内网。不要让多个 Slime 实例同时写入同一个 SQLite 文件或同一个 `runs/`。

## 2. 支持平台

| 项目 | Windows | Linux |
|---|---|---|
| 操作系统 | Windows 10/11 或 Windows Server | Ubuntu、Debian、Rocky、Alma 等现代发行版 |
| Python | 3.10+ | 3.10+ |
| Docker | Docker Desktop | Docker Engine |
| 命令入口 | `slime.cmd` + Python CLI | POSIX launcher + Python CLI |
| 后台管理 | `slime up` 状态文件 | `slime up` 或 systemd 二选一 |

持续集成覆盖 Python 3.10 和 3.13。Worker 镜像当前为 `slime-cairn-kali:0.0.21`。

## 3. 宿主机准备

### 3.1 Windows

1. 安装 Python 3.10 或更高版本，确保不是无法启动的 Windows Store 占位程序。
2. 安装并启动 Docker Desktop。
3. 确认当前 PowerShell 可以运行：

```powershell
python --version
docker version
docker info
```

4. 确认 Docker Desktop 使用 Linux Containers。

### 3.2 Linux

1. 安装 Python、venv、pip 和 Docker Engine。
2. 启动 Docker，并让部署用户可以访问 Docker socket：

```sh
sudo systemctl enable --now docker
docker version
docker info
```

将用户加入 `docker` 组意味着该用户近似拥有宿主机 root 权限。只对可信部署账户执行，并重新登录使组权限生效。

### 3.3 容量规划

默认不设置容器 memory、CPU 和 PID quota。最低资源取决于启用 Worker 数量和工具负载，建议从以下规模起步：

| 规模 | CPU | 内存 | 磁盘 |
|---|---:|---:|---:|
| 开发/单项目 | 4 核 | 8 GB | 30 GB 可用空间 |
| 多项目并发 | 8 核以上 | 16 GB 以上 | 80 GB 以上 |

磁盘主要消耗来自 Kali 镜像、Docker layer、transcript、Evidence 和 workspace。生产部署应监控 `runs/` 与 Docker data root。

## 4. 获取与安装项目

进入项目根目录后创建虚拟环境。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
```

只需要运行环境时可安装 `-e .`，不安装 `dev` 可选依赖。

## 5. 配置密钥与模型

从模板创建 `.env`：

Windows：

```powershell
Copy-Item .env.example .env
```

Linux：

```sh
cp .env.example .env
chmod 600 .env
```

### 5.1 共享配置

Codex 和 Pi 可以共用：

```dotenv
SLIME_LLM_BASE_URL=https://api.example.com/v1
SLIME_LLM_API_KEY=replace-with-a-low-privilege-key
SLIME_LLM_MODEL=replace-with-model-name
```

### 5.2 适配器专用配置

```dotenv
SLIME_CODEX_BASE_URL=https://responses.example.com/v1
SLIME_CODEX_API_KEY=replace-me
SLIME_CODEX_MODEL=replace-me
SLIME_CODEX_REASONING_EFFORT=xhigh

SLIME_PI_BASE_URL=https://chat.example.com/v1
SLIME_PI_API_KEY=replace-me
SLIME_PI_MODEL=replace-me
```

专用值优先于共享 `SLIME_LLM_*`。Codex 使用 Responses 协议，Pi 使用 Chat Completions 协议；端点协议必须和 Worker 类型匹配。
`SLIME_CODEX_REASONING_EFFORT` 通过 Dispatcher 映射为 Codex CLI 的
`model_reasoning_effort`；使用 `xhigh` 时应为启动健康检查和任务超时保留更长窗口。

### 5.3 Claude Code

默认配置保留 `claude-native`，但 `enabled` 为 `false`。启用前：

1. 在部署用户下完成 Claude Code CLI 登录。
2. 确认 `~/.claude` 存在。
3. 将 `dispatch.cairn.native.json` 中 `claude-native.enabled` 改为 `true`。
4. 运行 `slime doctor`。

未启用 Claude 时，缺少 `~/.claude` 不会阻止 Codex/Pi 启动。

### 5.4 密钥边界

- `.env` 必须保持在源码管理之外。
- 不要把真实密钥写入 dispatch JSON、README、测试、Project scope 或 Prompt。
- Codex/Pi 密钥通过短生命周期 Docker env file 进入 Worker。
- Benchmark token 只留在 API/Dispatcher 控制面，不下发给 Worker。

## 6. 配置 Dispatcher

默认配置为 `dispatch.cairn.native.json`。核心默认值：

| 配置 | 默认值 |
|---|---:|
| `runtime.max_workers` | 8 |
| `runtime.max_running_projects` | 3 |
| `runtime.max_project_workers` | 4 |
| `runtime.lease_seconds` | 15 |
| `runtime.heartbeat_interval` | 3 |
| `runtime.prompt_group` | `default` |
| `runtime.max_intent_attempts` | 6 |
| `runtime.growth_selection_mode` | `sma_discrete` |
| `runtime.worker_healthcheck` | `startup_only` |

生产部署前按宿主机容量调整全局、项目和 Worker 三层并发。单项目上限不应大于全局上限，所有已启用 Worker 的 `max_running` 总和可以高于全局上限，但实际并发仍受全局限制。

### 6.1 容器资源限制

默认不设置 quota。多项目或共享宿主机建议显式配置：

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

这些限制按项目容器生效，不是按 Worker 生效。修改限制后，容器在下次启动时重建，bind-mounted workspace 保留。

### 6.2 网络 Profile

| Profile | 网络 | capability |
|---|---|---|
| `standard` | none | 无 |
| `raw-network` | bridge | `NET_RAW` |
| `lab-network-admin` | 内部 lab 网络 | `NET_RAW`、`NET_ADMIN` |

CLI 默认使用 `raw-network`。不需要外部网络的任务应使用 `standard`；只有明确需要网络管理能力时才使用 `lab-network-admin`。

### 6.3 代理

模型端点需要代理时，将以下变量写入 dispatch JSON 的 `common_env`：

```json
{
  "common_env": {
    "HTTPS_PROXY": "http://proxy.example:8080",
    "HTTP_PROXY": "http://proxy.example:8080"
  }
}
```

Worker preflight 和实际任务使用同一代理配置。

## 7. 构建 Worker 镜像

Windows：

```powershell
.\scripts\finish-docker.ps1
```

Linux：

```sh
sh scripts/finish-docker.sh
```

构建完成后验证：

```text
docker image inspect slime-cairn-kali:0.0.21
```

构建脚本还会创建内部 lab 网络，并在只读、无网络容器中检查 `/opt/slime-cairn/tools.json` 和 Agent CLI。

## 8. 安装 `slime` 命令

Windows：

```powershell
.\slime install
```

该命令只把仓库 `bin` 加入当前用户 PATH。打开新终端后使用 `slime`。

Linux：

```sh
sh ./slime install
```

该命令创建受管理的 `~/.local/bin/slime`，必要时向 `~/.profile` 添加 PATH 块。安装器不会启动服务或修改系统级 PATH。

预览和卸载：

```text
slime install -DryRun
slime uninstall
```

Linux 标准参数形式为 `--dry-run`。

## 9. 部署前检查

```text
slime doctor
```

必须检查报告中的：

- `docker.healthy`：Docker Engine 可用。
- Worker 镜像存在。
- 容器内 Codex/Pi/Claude CLI 与启用配置一致。
- 每个启用模型端点探测成功。
- `ready.model_workers` 为真。
- `ready.service` 为真。

`slime up` 会在启动 Dispatcher 前再次执行严格检查。要只检查解析后的启动参数：

```text
slime up -DryRun
```

## 10. 启动方式

### 10.1 CLI 后台模式

适合开发机、个人工作站和普通单机部署：

```text
slime up
slime ui
slime logs -Follow
```

默认进程状态：

```text
runs/service/state.json
runs/service/server.out.log
runs/service/server.err.log
runs/service/dispatcher.out.log
runs/service/dispatcher.err.log
```

停止和重启：

```text
slime down
slime restart
```

`down` 只停止 API/Dispatcher，不删除 Blackboard、项目容器或 workspace。

### 10.2 前台模式

用于调试或交给外部进程管理器：

终端一：

```text
slime serve
```

终端二：

```text
slime dispatch
```

不要同时运行 `slime up` 和前台模式，否则可能产生重复 API/Dispatcher。

### 10.3 Linux systemd

长期 Linux 部署建议由 systemd 分别管理 API 和 Dispatcher。以下示例假设：

- 项目目录：`/opt/slime-cairn`
- 运行用户：`slime`
- 虚拟环境：`/opt/slime-cairn/.venv`
- 数据目录：`/opt/slime-cairn/runs`

创建 `/etc/slime-cairn/runtime.env`：

```dotenv
PYTHONPATH=/opt/slime-cairn/src
SLIME_CAIRN_DB=/opt/slime-cairn/runs/slime-server.db
SLIME_DISPATCH_CONFIG=/opt/slime-cairn/dispatch.cairn.native.json
SLIME_RUNTIME_MODE=docker
SLIME_RUNTIME_EXECUTION=container
SLIME_WORKSPACES_ROOT=/opt/slime-cairn/runs/slime-workspaces
SLIME_WORKER_PROFILE=raw-network
SLIME_DOCKER_BINARY=docker
SLIME_LAB_NETWORK=slime-cairn-lab
```

限制权限：

```sh
sudo install -d -o root -g slime -m 0750 /etc/slime-cairn
sudo chmod 0640 /etc/slime-cairn/runtime.env
sudo chown -R slime:slime /opt/slime-cairn/runs
```

`/etc/systemd/system/slime-api.service`：

```ini
[Unit]
Description=Slime Cairn API
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=slime
Group=slime
WorkingDirectory=/opt/slime-cairn
EnvironmentFile=/opt/slime-cairn/.env
EnvironmentFile=/etc/slime-cairn/runtime.env
ExecStart=/opt/slime-cairn/.venv/bin/python -m uvicorn slime_cairn.api:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/slime-dispatcher.service`：

```ini
[Unit]
Description=Slime Cairn Dispatcher
After=network-online.target docker.service slime-api.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=slime
Group=slime
WorkingDirectory=/opt/slime-cairn
EnvironmentFile=/opt/slime-cairn/.env
EnvironmentFile=/etc/slime-cairn/runtime.env
ExecStart=/opt/slime-cairn/.venv/bin/python -m slime_cairn.service_main
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
KillMode=control-group

[Install]
WantedBy=multi-user.target
```

加载并启动：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now slime-api slime-dispatcher
sudo systemctl status slime-api slime-dispatcher
journalctl -u slime-api -u slime-dispatcher -f
```

systemd 模式下不要再使用 `slime up/down/restart` 管理进程；使用 `systemctl`。CLI 的项目命令仍可正常使用。

### 10.4 Windows 开机启动

Windows 推荐先使用 `slime up` 验证部署。需要登录后自动启动时，可在任务计划程序中创建任务：

- 运行账户：部署用户。
- 触发器：用户登录后，并延迟到 Docker Desktop 可用。
- 工作目录：项目根目录。
- 程序：`cmd.exe`。
- 参数：`/d /c slime up`。

不要同时创建多个启动任务。Docker Desktop 尚未就绪时，`slime up` 的严格诊断会失败，应由计划任务重试或增加延迟。

## 11. 网络与反向代理

默认 API 绑定 `127.0.0.1`，这是推荐设置。UI 和 API 没有内置的公网账户认证，不应直接绑定 `0.0.0.0` 暴露到互联网。

远程访问优先使用：

1. SSH 隧道；
2. 受控 VPN；
3. 带 TLS、访问控制和请求大小限制的反向代理。

SSH 隧道示例：

```sh
ssh -L 8000:127.0.0.1:8000 user@slime-host
```

然后访问本机 `http://127.0.0.1:8000/`。

若使用 Nginx/Caddy，仍建议让 Slime 只监听 loopback，由代理负责 TLS 和身份认证。不要把模型密钥加入代理日志或 URL 参数。

## 12. 健康检查与监控

控制面健康：

```text
GET http://127.0.0.1:8000/health
```

运维检查：

```text
slime doctor
slime list
slime logs -Log all -Tail 200
```

建议监控：

- API `/health` 响应；
- systemd 或 CLI 状态文件中的两个进程；
- `runs/` 磁盘用量；
- Docker 容器数量和异常重启；
- `intent.retry_exhausted`、Worker health、Reason failure pause 事件；
- 模型端点认证、配额和延迟。

## 13. 备份与恢复

### 13.1 备份内容

至少备份：

- `runs/`；
- `.env`，单独加密保存；
- 实际使用的 dispatch JSON；
- 自定义 Worker 镜像标签或可重建 Dockerfile。

### 13.2 一致性备份

CLI 模式：

```text
slime down
```

systemd 模式：

```sh
sudo systemctl stop slime-dispatcher slime-api
```

确认进程停止后，再复制整个 `runs/`。不要只复制 SQLite 主文件而遗漏可能存在的 `-wal` 和 `-shm` 文件。

### 13.3 恢复

1. 停止 API 和 Dispatcher。
2. 将当前 `runs/` 移到保留位置。
3. 恢复完整备份目录和正确所有权。
4. 恢复匹配的 dispatch 配置和密钥。
5. 启动服务并运行 `slime doctor`。
6. 检查项目列表、事件时间线和 workspace。

首次用新版本打开旧数据库前必须备份，因为 Blackboard 可能自动执行向前 schema 升级。

## 14. 升级与回滚

推荐升级流程：

1. 停止新任务进入。
2. 停止 Dispatcher，再停止 API。
3. 备份 `runs/`、`.env` 和 dispatch JSON。
4. 更新源码。
5. 重新执行 `pip install -e .`。
6. 若 `worker/` 变化，重新构建 Worker 镜像。
7. 运行完整测试和 `slime doctor`。
8. 启动 API/Dispatcher。
9. 检查现有项目、事件和容器重建情况。

回滚时同时恢复代码、镜像和升级前数据库备份。仅回滚代码而保留已升级数据库可能不受支持。

## 15. 安全加固清单

- API 只绑定 loopback 或受保护内网。
- 远程访问通过 SSH/VPN 或带认证的 TLS 代理。
- `.env` 权限仅部署用户可读。
- 部署用户、Docker group 和系统管理员范围最小化。
- 对多项目宿主机配置 memory、CPU、PID 和磁盘监控。
- 只启用任务需要的 Worker 和网络 Profile。
- 不把 Docker socket 挂载到 Worker 容器。
- 定期轮换模型密钥和 Benchmark token。
- 对 `delete`、批量删除和备份恢复建立人工确认流程。
- 只对明确授权的目标运行网络或安全测试。

## 16. 部署验收

部署完成后逐项确认：

- [ ] Python 版本满足要求，依赖安装成功。
- [ ] Docker Engine 正常，Worker 镜像存在。
- [ ] `.env` 和 dispatch JSON 权限正确。
- [ ] `slime doctor` 返回 `ready.service: true`。
- [ ] API `/health` 可用且未暴露到非预期网络。
- [ ] Dispatcher 正常运行且无重复实例。
- [ ] 可以创建测试项目并看到 Worker Run。
- [ ] `slime pause/resume` 能保留项目 workspace。
- [ ] 日志、SQLite 和 workspace 位于计划的数据盘。
- [ ] 备份和恢复流程经过演练。
- [ ] 资源限制、磁盘监控和密钥轮换符合部署环境要求。

## 17. 故障排查

### Docker 不可用

检查 `docker info`、Docker daemon、用户组权限和 Docker Desktop 状态。Linux systemd 服务用户必须可以访问 `/var/run/docker.sock`。

### Worker preflight 失败

核对 Worker 是否启用、模型协议是否匹配、密钥是否有效、代理是否一致、镜像内 CLI 是否存在。认证和配额错误会获得较长 Worker cooldown。

### API 正常但 Dispatcher 不工作

检查：

```text
slime logs -Log dispatcher -Tail 200
```

systemd 模式使用：

```sh
journalctl -u slime-dispatcher -n 200 --no-pager
```

确认 `SLIME_DISPATCH_CONFIG`、数据库和 workspace 路径对 Dispatcher 用户可读写。

### 端口冲突

CLI 临时指定端口：

```text
slime up -Port 8010
```

所有后续 CLI 项目命令必须使用同一 `-Port 8010`。systemd 部署则修改 API unit，并确保客户端访问相同地址。

### 容器配置变化

镜像、网络、init 或 cgroup 设置变化后，项目容器在下次使用时重建。workspace 为宿主 bind mount，不会随容器删除。

### 状态文件过期

CLI 模式的状态文件是 `runs/service/state.json`。先确认其中 PID 是否仍属于 Slime，再执行 `slime down` 或 `slime up`。Linux CLI 会检查 `/proc` 命令行，拒绝停止无关 PID。

## 18. 相关文档

- [README](../README.md)
- [使用文档](USER_GUIDE.md)
- [CLI](CLI.md)
- [配置说明](CONFIGURATION.md)
- [架构说明](ARCHITECTURE.md)
- [运维说明](OPERATIONS.md)
- [验证说明](VALIDATION.md)
