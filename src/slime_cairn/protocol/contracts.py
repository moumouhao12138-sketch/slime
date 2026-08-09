"""Cairn-compatible structured-result contracts.

This module is deliberately independent from the Slime scheduler and native
agent implementation.  It provides a small, testable compatibility boundary
for the three Cairn worker task types:

* bootstrap (execute and conclude phases),
* explore (execute and conclude phases), and
* reason.

The functions accept Cairn's preferred ``{\"accepted\": ..., \"data\": ...}``
envelope and its legacy bare payload shapes.  They return normalized outcome
kinds but never perform Blackboard or network writes.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeAlias


JsonObject: TypeAlias = dict[str, Any]
ReasonData: TypeAlias = JsonObject | list[JsonObject] | None

_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_DESCRIPTION_PLACEHOLDERS = frozenset(
    {
        "...",
        "confirmed key objective results",
        "why those results prove goal",
        "confirmed objective facts so far",
        "latest confirmed incremental facts",
        "independent high-value exploration direction",
    }
)
_BRACED_FLAG_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])flag\{([^{}\r\n]{1,4094})\}"
)
_LABELLED_SUBMISSION_RE = re.compile(
    r"""(?ix)
    \b(?:flag|answer|candidate|答案|候选答案|候选值)
    (?:\s+value)?\s*(?:[:=：]|\bis\b|是|为)\s*
    (?:
        `(?P<backtick>[^`\r\n]{1,4094})`
        |"(?P<double>[^"\r\n]{1,4094})"
        |'(?P<single>[^'\r\n]{1,4094})'
        |(?P<bare>[^\s,;，；]{1,4094})
    )
    """
)
_SUBMISSION_PLACEHOLDERS = frozenset(
    {
        "...",
        "<...>",
        "exact candidate",
        "your_flag",
        "your-flag",
        "placeholder",
        "redacted",
        "unknown",
        "value",
    }
)


def _is_submission_placeholder(candidate: str) -> bool:
    value = candidate.strip()
    folded = value.casefold()
    if not value or folded in _SUBMISSION_PLACEHOLDERS:
        return True
    if "..." in value or "<" in value or ">" in value:
        return True
    if folded.startswith("flag{") and value.endswith("}"):
        body = value[5:-1].strip().casefold()
        return not body or body in _SUBMISSION_PLACEHOLDERS
    return False


def extract_submission_candidates(value: Any) -> list[str]:
    """Recover explicitly labelled Benchmark candidates without assuming a format."""

    if not isinstance(value, str):
        return []
    candidates: list[str] = []
    for match in _BRACED_FLAG_CANDIDATE_RE.finditer(value):
        candidate = match.group(0)
        if _is_submission_placeholder(candidate):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    for match in _LABELLED_SUBMISSION_RE.finditer(value):
        candidate = next(
            group.strip()
            for group in (
                match.group("backtick"),
                match.group("double"),
                match.group("single"),
                match.group("bare"),
            )
            if group is not None
        )
        if match.group("bare") is not None:
            candidate = candidate.rstrip(".,:;!?。，：；！？")
        if _is_submission_placeholder(candidate):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def extract_flag_candidates(value: Any) -> list[str]:
    """Compatibility alias for callers predating format-neutral submissions."""

    return extract_submission_candidates(value)


def _required_description(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    description = value.strip()
    if description.casefold() in _DESCRIPTION_PLACEHOLDERS:
        raise ValueError(f"{label} must replace the example placeholder with concrete results")
    return description


def parse_json_output(text: str) -> JsonObject:
    """Extract the first JSON object from raw CLI/model output.

    Cairn prompts request one raw JSON object, but its parser deliberately
    tolerates prose and fenced JSON for compatibility with real CLIs.
    """

    decoder = json.JSONDecoder()
    seen: set[str] = set()
    candidates = [text.strip()]
    candidates.extend(match.group(1).strip() for match in _FENCED_BLOCK_RE.finditer(text))

    for candidate in candidates:
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

        for offset, char in enumerate(segment):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(segment[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def extract_json_object(text: str) -> JsonObject:
    """Compatibility alias matching Cairn's output-parser terminology."""

    return parse_json_output(text)


def _unwrap_wrapped_payload(payload: JsonObject) -> tuple[bool | None, JsonObject | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _looks_like_reason_data(payload: JsonObject) -> bool:
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: JsonObject) -> bool:
    return set(payload) == {"fact", "complete"} and isinstance(payload.get("fact"), dict) and isinstance(
        payload.get("complete"), dict
    )


def _looks_like_bootstrap_conclude_data(payload: JsonObject) -> bool:
    return set(payload) in ({"fact"}, {"fact", "complete"}) and isinstance(payload.get("fact"), dict)


def _looks_like_explore_data(payload: JsonObject) -> bool:
    keys = set(payload)
    return "description" in keys and keys <= {"description", "submissions"}


def validate_reason_payload(
    payload: JsonObject,
    *,
    open_intents_empty: bool,
    max_intents: int,
    known_fact_ids: set[str] | None = None,
) -> tuple[str, ReasonData]:
    """Validate a Cairn Reason result.

    Return kinds are ``rejected``, ``complete``, ``intents`` and ``noop``.
    ``noop`` is valid only when at least one intent is already open.
    """

    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    complete = data.get("complete")
    intents = data.get("intents")
    # Cairn accepts the historical singular form from imperfect model output.
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]

    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        normalized_complete = dict(complete)
        _validate_reason_sources(complete.get("from"), known_fact_ids, "complete")
        normalized_complete["description"] = _required_description(
            complete.get("description"), "complete.description"
        )
        submissions = complete.get("submissions", [])
        if not isinstance(submissions, list):
            raise ValueError("complete.submissions must be an array")
        normalized_submissions: list[str] = []
        for index, submission in enumerate(submissions):
            if not isinstance(submission, str) or not submission.strip():
                raise ValueError(f"complete.submissions[{index}] must be a non-empty string")
            candidate = submission.strip()
            if len(candidate) > 4096:
                raise ValueError(f"complete.submissions[{index}] exceeds 4096 characters")
            if candidate not in normalized_submissions:
                normalized_submissions.append(candidate)
        normalized_complete["submissions"] = normalized_submissions
        return "complete", normalized_complete

    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        normalized_intents: list[JsonObject] = []
        for index, intent in enumerate(intents):
            if isinstance(intent, dict) and "description" not in intent and "objective" in intent:
                intent = {**intent, "description": intent["objective"]}
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {index}")
            normalized_intent = dict(intent)
            _validate_reason_sources(intent.get("from"), known_fact_ids, f"intent[{index}]")
            normalized_intent["description"] = _required_description(
                intent.get("description"), f"intent[{index}].description"
            )
            normalized_intents.append(normalized_intent)
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        limited = normalized_intents[:max_intents]
        if not limited:
            return "noop", None
        return "intents", limited

    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def _validate_reason_sources(
    value: Any,
    known_fact_ids: set[str] | None,
    label: str,
) -> None:
    """Reject Hint IDs and stale graph references before scheduling work."""

    if known_fact_ids is None:
        return
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label}.from must contain Fact IDs")
    sources = {str(item).strip() for item in value if str(item).strip()}
    unknown = sorted(sources - {str(item) for item in known_fact_ids})
    if unknown:
        raise ValueError(f"{label}.from contains unknown Fact IDs: {unknown}")


def validate_bootstrap_execute_payload(payload: JsonObject) -> tuple[str, dict[str, str] | None]:
    """Validate Bootstrap's direct-solve phase.

    A successful direct Bootstrap result must contain both a confirmed Fact and
    a completion explanation.  Partial progress belongs in conclude phase.
    """

    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = _required_description(fact.get("description"), "fact.description")

    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = _required_description(
        complete.get("description"), "complete.description"
    )

    return "complete", {
        "fact_description": fact_description,
        "complete_description": complete_description,
    }


def validate_bootstrap_conclude_payload(payload: JsonObject) -> tuple[str, str | None]:
    """Validate Bootstrap's bounded conclusion phase.

    ``complete`` is tolerated for wire compatibility but intentionally not
    returned: a conclude phase persists only a Fact.
    """

    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    if set(data) - {"fact", "complete"}:
        raise ValueError("unexpected keys in conclude payload")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    description = _required_description(fact.get("description"), "fact.description")
    return "fact", description


def validate_explore_payload(payload: JsonObject) -> tuple[str, str | None]:
    """Validate Explore execute/conclude output."""

    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    description = _required_description(data.get("description"), "description")
    return "fact", description


def extract_explore_submissions(payload: JsonObject) -> list[str]:
    """Validate and normalize explicit Benchmark candidates from Explore."""

    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return []
    source = data if accepted is True else payload
    if not isinstance(source, dict):
        return []
    raw = source.get("submissions", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Explore submissions must be an array")
    normalized: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Explore submissions[{index}] must be a non-empty string")
        candidate = value.strip()
        if len(candidate) > 4096:
            raise ValueError(f"Explore submissions[{index}] exceeds 4096 characters")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized
