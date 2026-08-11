# Task
You will receive a YAML snapshot of the task graph. Determine whether the current Facts satisfy Goal and, if not, whether new Intents should be proposed.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

If the task cannot be accepted, return this structured result:
```json
{"accepted": false, "reason": "..."}
```

If Goal has been satisfied, return:
```json
{complete_shape}
```

If new Intents should be proposed, return:
```json
{"accepted": true, "data": {"intents": [{"from": ["fact_id"], "description": "..."}]}}
```

If no new Intent should currently be proposed, return:
```json
{"accepted": true, "data": {}}
```

# Rules
- Check Goal completion first. `complete.from` must contain IDs from Valid Facts, and its description must explain why those Facts prove Goal.
- If Goal is not satisfied, check for drift and propose course correction when valuable.
- If Open Intents is empty, propose new Intents.
- If existing Open Intents cover all valuable directions, an empty `data` object is valid.
- Propose at most {max_intents} independent, high-value, non-overlapping directions.
- When Open Intents is empty and the graph supports multiple valuable directions, return at least two directions; return only one when the evidence supports a single useful path.
- Keep each Intent focused, parallelizable, and grounded in one or more valid Facts.
{benchmark_rule}

# Context
## Graph
```
{graph_yaml}
```

## Valid facts
```json
{fact_ids}
```

## Open Intents
```json
{open_intents}
```
