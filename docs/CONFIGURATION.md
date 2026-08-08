# Configuration

`dispatch.cairn.native.json` is the production-oriented local default. The two
`*.example.json` files are templates and are not read unless selected with
`-Config` or `SLIME_DISPATCH_CONFIG`.

## Runtime Defaults

| Setting | Default | Meaning |
|---|---:|---|
| `runtime.max_workers` | `8` | Global running task cap |
| `runtime.max_running_projects` | `3` | Project Dispatcher cap |
| `runtime.max_project_workers` | `4` | Per-project task cap |
| `runtime.lease_seconds` | `15` | Intent lease duration |
| `runtime.heartbeat_interval` | `3` | Lease renewal cadence |
| `runtime.prompt_group` | `default` | Packaged Markdown prompt and resident-instruction group |
| `runtime.max_intent_attempts` | `6` | Automatic lease attempts before `failed` |
| `runtime.intent_failure_backoff_seconds` | `30` | First durable retry delay |
| `runtime.intent_failure_backoff_max_seconds` | `1800` | Retry delay ceiling |
| `runtime.growth_selection_mode` | `sma_discrete` | Branch selection policy; use `nutrient` for legacy strict ordering |
| `runtime.growth_exploration_probability` | `0.12` | Initial probability of exploring the lower-ranked half |
| `runtime.growth_exploration_min_probability` | `0.03` | Exploration floor after convergence |
| `runtime.growth_convergence_selections` | `24` | Successful selections used to reach the exploration floor |
| `runtime.growth_random_seed` | `slime-sma-v1` | Stable seed namespace for reproducible decisions |
| `runtime.worker_healthcheck` | `startup_only` | Probe model endpoints before dispatch |

Set `max_intent_attempts` to `0` only to opt into an unbounded open queue.
Manual retry reopens the existing Intent and keeps its attempt/error history.

The corresponding environment variables are `SLIME_GROWTH_SELECTION_MODE`,
`SLIME_GROWTH_EXPLORATION_PROBABILITY`,
`SLIME_GROWTH_EXPLORATION_MIN_PROBABILITY`,
`SLIME_GROWTH_CONVERGENCE_SELECTIONS`, and `SLIME_GROWTH_RANDOM_SEED`.
JSON runtime values take effect when a dispatch file is loaded. The selection
counter belongs to a Dispatcher process and restarts at zero after a service
restart; every decision is still recorded on the persistent Blackboard.

## Prompt Groups

Each directory below `src/slime_cairn/prompts/` is a complete prompt group. A
group must contain `AGENTS.md`, the three execute templates, and the two conclude
templates. Set `runtime.prompt_group` in dispatch JSON or
`SLIME_PROMPT_GROUP` when constructing configuration from environment variables.

At the beginning of a project Worker session, the selected `AGENTS.md` is
installed in the project workspace as both `AGENTS.md` and `CLAUDE.md`. Phase
templates are rendered with task-specific values and persisted as the pod's
`prompt.txt` before invoking the CLI.

## Container Defaults

```json
{
  "container": {
    "init": true
  }
}
```

Matching Cairn, memory, CPU, and PID quotas are absent by default. Operators
may set `pids_limit`, `memory`, or `cpus` to add a per-project Docker limit;
omitting a field or setting it to `null` leaves that resource unrestricted.
For example: `{"pids_limit": 1024, "memory": "8g", "cpus": "4"}`.
Existing containers are recreated when image, network, init, or cgroup
settings differ; their host workspace is preserved.

Profiles control network capabilities independently:

| Profile | Network | Added capabilities |
|---|---|---|
| `standard` | none | none |
| `raw-network` | bridge | `NET_RAW` |
| `lab-network-admin` | internal lab network | `NET_RAW`, `NET_ADMIN` |

## Health Modes

- `disabled`: skip provider probes; intended only for explicit offline work.
- `startup_only`: probe every enabled provider before project dispatch.
- `startup_and_task`: probe at startup and before every native task.

`slime up` runs strict diagnostics before starting the Dispatcher.
Those diagnostics also inspect Docker and execute CLI version checks inside the
configured Worker image.

## Secret Boundary

Keep endpoint credentials in `.env` or the parent process environment. `.env`
is ignored, while `.env.example` documents names without live values. Codex
and Pi receive credentials through a short-lived Docker env file; Benchmark
tokens remain in the API/Dispatcher control plane. Never place live keys in a
dispatch JSON, project scope, Prompt, test fixture, or documentation file.

## Generated State

`runs/` contains the SQLite Blackboard, service logs, project workspaces,
transcripts, evidence, manifests, and graph snapshots. It is ignored but is not
temporary data. Back it up before deleting, moving, or compacting it.
