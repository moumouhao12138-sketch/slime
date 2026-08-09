# Task
This is the conclude phase for an Explore task. The earlier execute pass ended without a usable structured result.

Trigger: `{trigger}`

Initial error:
```
{initial_error}
```

This newer instruction overrides the earlier task. Stop immediately: do not run commands, make tool calls, inspect more files, wait for unfinished work, or plan further actions.

# Output Requirements
Return exactly one raw JSON object and no Markdown or surrounding text.

Normal result:
```json
{"accepted": true, "data": {"description": "..."}}
```

Rejected result:
```json
{"accepted": false, "reason": "..."}
```

# Rules
- Use only objective information confirmed before this conclude prompt.
- Return only the latest confirmed incremental facts for this Intent.
- Replace `...` with a concrete result; the placeholder itself is invalid.
- Do not include Slime metadata, scores, hypotheses, plans, or Intent proposals.

# Task Context
```json
{context_json}
```
