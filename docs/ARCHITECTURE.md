# Architecture

## Cairn Reference

The reference project has two execution backends: `container` and `local`. Both start the selected CLI process directly through an execution backend. Its scheduler hands a complete task prompt to the CLI, parses one final result, and writes the result to its server protocol.

Slime uses the same container shape and intentionally supports only the container backend:

```text
Project record
  -> Dispatcher selects a Native Worker
  -> PersistentDockerBackend execs the worker CLI in the project container
  -> NativeAgentMind stores transcript, result JSON, and evidence references
  -> Scheduler validates candidates and updates the Blackboard
```

The public `slime` command is implemented in Python so the same lifecycle
semantics work on Linux and Windows. Windows keeps the existing PowerShell
implementation behind `slime.cmd` for compatibility; Linux uses POSIX process
groups and does not require PowerShell or Docker Desktop.

## Project Container

One project owns one persistent Kali container and one shared `/workspace`.

- `/workspace/pods/<worker>/<task>`: context JSON, transcript, result, evidence, and resume manifest for one task.
- `/workspace/context`: complete Blackboard graph snapshots written before each task.
- `/workspace/AGENTS.md` and `/workspace/CLAUDE.md`: resident instructions from the selected prompt group.

The default configuration uses Cairn's container credential model. Codex owns the Responses protocol and Pi owns Chat Completions, with both advertising Bootstrap, Explore, and Reason. The Dispatcher probes each exact model/protocol pair and selects from healthy capacity. Credentials pass through a short-lived Docker env file; host Agent login directories are outside this execution path.

The default `worker_healthcheck: startup_only` sends a small protocol-correct request to every enabled endpoint before project dispatch. `startup_and_task` repeats the probe before each native CLI task. `slime up` and `slime doctor` separately verify the CLI binaries inside the Worker image; per-task container preflight remains an explicit Worker option for custom-image diagnostics and resource recovery.

## Blackboard Loop

1. Bootstrap creates initial Facts.
2. Explore workers run independent intents concurrently.
3. Every finished worker returns a bounded JSON report.
4. Scheduler stores artifacts, validates Fact and Hypothesis candidates, and records the worker run.
5. A persistent Hint can wake Reason without replacing the project's declared scope.
6. Reason receives a new task with the full graph snapshot path, all valid Fact IDs, and the current open Intents. It is the only task mode that creates new intents or completes the project with cited Facts.

For a managed Benchmark project, Explore may additionally return an explicit
`data.submissions` array. Its values are format-neutral exact strings; Slime
does not require a `flag{...}` wrapper or any other prefix. When a Worker omits
that optional field, the Scheduler also recovers clearly labelled candidates
from newly accepted Fact descriptions, while retaining brace-style extraction
for compatibility. Explicit values are submitted first, recovered values
second; both paths go through the same deduplication and Benchmark control-plane
verification, which records the platform response as a Fact. Obvious schema
placeholders are ignored.

Native provider failures are categorized separately from container failures.
For example, an upstream `stream disconnected before completion` or
`Upstream request failed` response is recorded as `provider_stream_disconnect`
and receives a bounded cooldown. Explicit safety-policy refusals are recorded
as `model_policy_filter`; neither category is treated as a Docker health fault.

Before every Explore or Reason task, the Scheduler exports the complete Blackboard into a Cairn-shaped `graph.yaml`. Fact bodies, Hints, and Intent history are not ranked, omitted, or token-trimmed. The CLI prompt contains only the graph file path and the small task-specific fields Cairn requires; the Worker reads the complete YAML file from the persistent project container.

Phase prompts are packaged Markdown resources under
`slime_cairn/prompts/<runtime.prompt_group>/`, not Python string literals.
Bootstrap, Explore, Reason, and their conclude phases are independently editable
while sharing one validated structured-result contract.

## Discrete SMA Branch Selection

The Dispatcher asks the Blackboard only for pending Intents whose retry time
has opened, then `SmaBranchPolicy` ranks that runnable population:

```text
runnable Intents + all branch history
  -> normalize nutrient, branch nutrient, strength, and novelty
  -> subtract bounded failure-streak penalty
  -> seeded weighted attraction or lower-half exploration
  -> Worker capability/capacity check
  -> atomic claim by exact Intent ID
```

The fitness weights are `0.60` current nutrient, `0.20` maximum historical
nutrient under the same `branch_root_id`, `0.15` strength, and `0.05` novelty.
The existing result reward updates nutrient and strength, so an
evidence-producing path affects both its current Intent and runnable
descendants. Failure streak subtracts `0.075` per failure up to `0.30`.

For three or more candidates, exploitation samples all candidates with an
exponential fitness weight. Exploration samples only the lower-ranked half,
with inverse-fitness weighting. Its probability decreases linearly from
`0.12` to `0.03` over 24 successful selections. One- and two-candidate
populations choose the best candidate directly because dispatching it already
leaves the other candidate next in line.

Randomness is derived from a SHA-256 digest of the configured seed, project
ID, process-local selection index, and sorted candidate IDs. The event stream
stores the digest prefix, random draw, effective probability, ranks, and score
components. It does not replace authorization, retry, lease, capacity, or
atomic-claim rules; it only changes ordering inside the runnable set.
