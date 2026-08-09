# Task
You will receive a YAML snapshot of the task graph. Facts are confirmed objective results and Intents are exploration branches from one or more Facts. Interpret the graph and explore only the assigned Current Intent to advance Goal.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

If the task cannot be accepted, return this structured result:
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal result:
```json
{explore_shape}
```

# Rules
- Thoroughly explore this Intent, then end it if this direction cannot advance Goal.
- A newer conclude-phase instruction in the same session overrides this task immediately.
- `description` must state confirmed objective results and reference files containing long data.
- Include only the latest incremental facts. Do not repeat facts already present in the graph.
{benchmark_rule}

# Context
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```

## Branch Checkpoint
This Slime checkpoint records continuity, prior evidence references, and the next smallest validation. Use it to continue the same line rather than repeating broad discovery. It does not replace the graph or change the output schema.
```json
{branch_checkpoint}
```
