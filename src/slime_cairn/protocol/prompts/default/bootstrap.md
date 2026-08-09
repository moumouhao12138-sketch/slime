# Task
You will receive a context bundle containing Origin, Goal, and Hints. Understand the starting point and the information already available, then steadily drive the task forward until Goal is achieved.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

If the task cannot be accepted, return this structured result:
```json
{"accepted": false, "reason": "policy_refusal"}
```

Only return the following after confirming that Goal has been satisfied:
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

# Rules
- If the problem is not yet solved, keep working until a newer conclude-phase instruction replaces this task.
- A conclude-phase instruction in the same session overrides this keep-working rule immediately.
- Output `complete` only if Goal has definitively been achieved in this session.
- `fact.description` must state confirmed objective results, not plans or guesses.
- `complete.description` must explain why those results prove Goal is achieved.
- Store long data in a file and reference it instead of putting it in `description`.

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```json
{hints}
```
