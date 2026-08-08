# Project Layout

```text
pyproject.toml                 Package metadata, dependencies, entry points, and test settings
.env.example                   Safe model and runtime configuration template
dispatch.cairn.native.json     Default Codex/Pi Workers with optional Claude configuration
dispatch.cairn.reason-first.example.json  Explore/Reason-only growth example
slime.cmd                      Repository-local public `slime` launcher
slime                          POSIX repository-local public `slime` launcher
bin/slime.cmd                  Windows user-PATH command shim
bin/slime                      POSIX Python command shim
scripts/check-e2e-result.py    Database status and evidence-backed completion report
scripts/finish-docker.ps1      Kali image build and smoke check
scripts/finish-docker.sh       Linux Docker/Kali image build and smoke check
scripts/native-docker-smoke.py Container command smoke test
src/slime_cairn/api.py         FastAPI, `/`, `/view`, `/events`, Hint, and project APIs
src/slime_cairn/static/        Offline Cairn-style project observability UI
src/slime_cairn/blackboard.py  Persistent Facts, Hints, Intents, Reason leases, runs, events
src/slime_cairn/branch_policy.py Discrete SMA fitness, exploration, convergence, selection trace
src/slime_cairn/cli.py         Cross-platform `slime` command and lifecycle management
src/slime_cairn/dispatcher.py  Claims, leases, hard-stop cancellation, worker selection
src/slime_cairn/scheduler.py   Context, report import, validation, Reason loop
src/slime_cairn/native_agent.py CLI execution, model preflight, final JSON parser
src/slime_cairn/prompting.py    Prompt group loading, validation, and rendering
src/slime_cairn/prompts/        Markdown phase prompts and resident Worker instructions
src/slime_cairn/model_health.py Explicit model endpoint probe and redacted diagnostics
src/slime_cairn/runtime_factory.py Persistent project Kali container factory
src/slime_cairn/worker_manager.py Container lifecycle and Agent home projection
worker/Dockerfile               Kali, CLI agents, tools, local references
tests/                           Unit, API, lifecycle, policy, and launcher regression tests
.github/workflows/tests.yml      Windows/Linux CI on Python 3.10 and 3.13
docs/                           Architecture, operation, validation notes
docs/CONFIGURATION.md           Runtime defaults and override reference
docs/CLI.md                     Public command setup and command groups
docs/DEPLOYMENT.md              Windows/Linux deployment, systemd, backup, and upgrades
docs/USER_GUIDE.md              Project creation, UI, Hints, retries, and result review
runs/                           Generated SQLite, logs, transcripts, and workspaces
```

Source and generated state stay separate. `src/`, `tests/`, `worker/`,
`scripts/`, and `docs/` are maintainable project inputs. `runs/`, `.env`,
Python caches, build output, local databases, and Agent sessions are ignored
and must not be committed. Deleting or moving `runs/` is an explicit operator
action because it contains the durable Blackboard and evidence artifacts.

## Runtime Shape

One project owns one persistent Kali container and one shared writable `/workspace`. A configured Claude, Codex, or Pi CLI runs inside that container and directly uses its Shell, files, and installed tools. The runtime has no gateway or model-facing tool relay.

The selected prompt group installs its `AGENTS.md` resource as both
`/workspace/AGENTS.md` and `/workspace/CLAUDE.md`. Phase-specific Markdown
templates remain packaged under `src/slime_cairn/prompts/<group>/`.

The FastAPI root route serves the local observability panel. It reads `GET /projects/{project_id}/view` for the graph and Inspector, then incrementally polls `GET /projects/{project_id}/events?after_id=...` for the timeline. Run:

```powershell
slime restart
slime ui
```

after a source update so the already-running API and Dispatcher load the new runtime and static page.

The application regression suite lives in `tests/` and runs through
`.github/workflows/tests.yml` on Windows and Linux with Python 3.10 and 3.13.
The suite does not call real model providers or require project Docker
containers.
