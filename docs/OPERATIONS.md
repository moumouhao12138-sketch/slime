# Operations

## Prerequisites

- Windows: Docker Desktop is running. Linux: the Docker Engine daemon is running and the current user can run `docker`.
- Linux: Python 3.10 or newer has the project installed with `python3 -m pip install -e .`.
- `.env` or the shell that starts Slime contains the variables used by every enabled Worker. Codex reads `SLIME_CODEX_*` for Responses; Pi reads `SLIME_PI_*` for Chat Completions. Both accept `SLIME_LLM_BASE_URL`, `SLIME_LLM_API_KEY`, and `SLIME_LLM_MODEL` as fallbacks.
- The default configuration file is `dispatch.cairn.native.json`.

## Build The Runtime

```text
# Windows PowerShell
.\scripts\finish-docker.ps1

# Linux shell
sh scripts/finish-docker.sh
```

The script uses `docker.1ms.run` for the base image and `https://mirrors.ustc.edu.cn/kali/` as the fixed Kali package source. It tags the completed image as `slime-cairn-kali:0.0.21`.

Reference repositories are optional and disabled by default so GitHub transfer instability does not block the Kali runtime. To include their cached snapshots in the image, use:

```text
# Windows PowerShell
.\scripts\finish-docker.ps1 -InstallReferenceAssets true

# Linux shell
sh scripts/finish-docker.sh --install-reference-assets true
```

## Start And Inspect

```powershell
slime up
slime doctor
slime list
```

`doctor` checks Docker, the configured image, CLI binaries inside the image, and a bounded real probe for each enabled explicit model Worker. Read `ready.service`; it becomes `true` only when the image and the required Explore/Reason Worker health checks pass.

When the model endpoint requires a proxy, put `HTTPS_PROXY`, `HTTP_PROXY`, or `ALL_PROXY` in `common_env`. The preflight probe uses the same proxy variables that are injected into the native CLI process.

When a stopped project still has a container from an earlier image version, its next runtime start recreates the container with the configured image. The project workspace bind mount is preserved.

Runtime startup compares both the configured image reference and its resolved
immutable image ID. Rebuilding `slime-cairn-kali:0.0.21` under the same tag
therefore recreates an existing project container instead of silently reusing
the old layers; the project workspace and Blackboard remain intact.

Like Cairn, project containers have no memory, CPU, or PID quota by default. They still use `--init` for process reaping. Optional `pids_limit`, `memory`, and `cpus` values live in the dispatch file's `container` section; adding, changing, or removing one recreates the project container on its next start while preserving the bind-mounted workspace.

Intent failures default to six attempts. The first five failures wait 30, 60, 120, 240, and 480 seconds; the sixth becomes terminal. The configured 1800-second ceiling applies when operators choose a larger or unlimited attempt count. A terminal `failed` Intent remains visible and can be reopened from the Inspector without losing its Worker Run history. `max_intent_attempts: 0` is supported for Cairn compatibility but is not the operational default.

Branch selection defaults to `sma_discrete`. Inspect `growth.branch_selected`
events to audit the chosen Intent, policy-selected Intent, Worker fallback,
fitness ranks, exploration draw, and deterministic seed digest. Use
`growth_selection_mode: "nutrient"` for strict nutrient-descending behavior
during comparison or rollback. Changing selection settings does not require a
database migration, but a running Dispatcher reads them only at process
startup.

### Benchmark Candidates And Provider Failures

For a managed Benchmark project, an Explore Worker can submit a candidate with
the explicit contract:

```json
{"accepted":true,"data":{"description":"confirmed response value","submissions":["exact value returned by target"]}}
```

The Scheduler validates and deduplicates those exact strings before calling
the Benchmark API. Submission values have no required prefix, wrapper, or
syntax. When the Worker omitted `submissions`, the Scheduler can recover values
from clearly labelled Fact text such as `flag: TOKEN-42`, `answer = UUID`, or a
legacy brace-style value. Obvious placeholders are ignored. A failed submission
leaves the discovered Fact durable and releases the Intent for retry.

The Inspector error text is accompanied by a failure category in dispatcher
events. `provider_stream_disconnect` means the upstream stream ended before a
complete response; `model_policy_filter` means the provider reported a safety
policy decision. Both use cooldowns and should not be diagnosed as a broken
Docker container. The same category is appended to the Intent `last_error` and
latest Worker Run error shown by Inspector. Check
`dispatcher.intent_worker_cycle_cooldown` events for the category and
`retry_after_seconds`.

### Windows Prompt File WinError 5

If a native Worker reports `PermissionError: [WinError 5]` while replacing a
`pods/.../prompt.txt` file, the mounted workspace file is still open by the
previous Docker/CLI process. The prompt content was written successfully, but
Windows refused to replace the existing filename. Slime automatically retries
that write under a unique `prompt-<id>.txt` name and passes the new mounted path
to the next CLI invocation. The failed Intent remains retryable; restarting the
service is only needed when an older process was started before this fallback
was deployed.

## Project Lifecycle

```powershell
slime new -Name "test-001" -Target "https://TARGET/" -Goal "Return an evidence-backed result."
slime logs -Follow
slime status -Name "test-001"
slime runtime -Name "test-001"
slime pause -Name "test-001"
slime resume -Name "test-001"
slime stop -Name "test-001"
```

`pause` and `stop` set the project state to `stopped`; `resume` puts the same project back into the Dispatcher queue. Its persistent workspace and previous Blackboard data remain available.

The project panel can append a Hint at any time. The same local API is available at `POST /projects/{project_id}/hints`; a new Hint becomes durable Blackboard context and wakes Reason on the next Dispatcher cycle.
