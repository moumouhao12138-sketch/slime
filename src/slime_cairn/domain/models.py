from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import time
from typing import Any
from uuid import uuid4


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(*parts: Any) -> str:
    return sha256(stable_json(parts).encode("utf-8")).hexdigest()


def coerce_unit_score(value: Any, default: float) -> tuple[float, str | None]:
    """Turn model-provided scheduling scores into stable unit-interval values.

    Models occasionally use a score field for a semantic explanation. Keep that
    text available to the caller while preserving a valid scheduler value.
    """

    try:
        score = float(value)
    except (TypeError, ValueError):
        note = str(value).strip()
        return default, note or None
    if not math.isfinite(score) or score < 0 or score > 1:
        return default, str(value)
    return score, None


@dataclass(slots=True)
class Project:
    id: str
    name: str
    target: str
    goal: str
    scope: dict[str, Any]
    status: str = "running"
    created_at: float = field(default_factory=now)


@dataclass(slots=True)
class Hint:
    """A durable piece of human judgment added to a project's Blackboard."""

    id: str = field(default_factory=lambda: new_id("hint"))
    project_id: str = ""
    content: str = ""
    creator: str = "human"
    created_at: float = field(default_factory=now)


@dataclass(slots=True)
class FactCandidate:
    subject: str
    predicate: str
    object: str
    confidence: float
    evidence_refs: list[str]
    source_intent_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return fingerprint(self.subject, self.predicate, self.object)


@dataclass(slots=True)
class Fact(FactCandidate):
    id: str = field(default_factory=lambda: new_id("fact"))
    project_id: str = ""
    created_at: float = field(default_factory=now)
    memory_state: str = "active"  # active / pinned / dormant / revived
    structural_importance: float = 0.0
    reference_count: int = 0
    branch_count: int = 0
    omission_count: int = 0
    access_count: int = 0
    last_accessed_at: float = 0.0
    last_revived_at: float | None = None


@dataclass(slots=True)
class HypothesisCandidate:
    statement: str
    supporting_fact_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.5
    next_validation: str = ""
    source_intent_id: str | None = None


@dataclass(slots=True)
class Hypothesis(HypothesisCandidate):
    id: str = field(default_factory=lambda: new_id("hypothesis"))
    project_id: str = ""
    status: str = "open"
    created_at: float = field(default_factory=now)


@dataclass(slots=True)
class IntentProposal:
    kind: str
    objective: str
    target_entity: str
    parent_fact_ids: list[str]
    expected_value: float = 0.5
    novelty: float = 0.5
    cost: float = 0.2
    risk: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable semantic identity used to suppress only true duplicate work.

        Different exploration directions often share the same target and an
        empty context.  Their objective and incoming Fact path must therefore
        remain part of the key or Reason loses the branches it deliberately
        created for parallel search.
        """

        # ``slime_meta`` is derived scheduling state. Excluding it keeps the
        # Cairn Intent identity stable when nutrients, memory or audit state
        # change between Reason passes.
        semantic_context = {key: value for key, value in self.context.items() if key != "slime_meta"}
        semantic_provenance = {
            key: value for key, value in self.provenance.items() if key != "slime_meta"
        }
        return fingerprint(
            self.kind,
            self.objective.strip(),
            self.target_entity,
            sorted(set(self.parent_fact_ids)),
            semantic_context,
            semantic_provenance,
        )


@dataclass(slots=True)
class Intent(IntentProposal):
    id: str = field(default_factory=lambda: new_id("intent"))
    project_id: str = ""
    fingerprint: str = ""
    nutrient: float = 0.0
    strength: float = 1.0
    status: str = "pending"
    owner: str | None = None
    lease_expires_at: float | None = None
    last_heartbeat_at: float | None = None
    last_error: str = ""
    attempts: int = 0
    # ``attempts`` is the total number of leases.  These fields describe the
    # current failure cycle and survive a Dispatcher restart.
    failure_streak: int = 0
    retry_not_before: float = 0.0
    created_at: float = field(default_factory=now)


@dataclass(slots=True)
class WorkerTask:
    """One of the three Cairn-style modes executed by the same worker runtime."""

    mode: str  # bootstrap / explore / reason
    objective: str
    intent_id: str
    target_entity: str
    relevant_fact_ids: list[str]
    goal: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    environment_brief: str = ""
    previous_summary: str = ""
    branch_summaries: list[dict[str, Any]] = field(default_factory=list)
    context_manifest: dict[str, Any] = field(default_factory=dict)
    hints: list[dict[str, Any]] = field(default_factory=list)
    open_intents: list[dict[str, Any]] = field(default_factory=list)
    initial_budget: int = 60
    max_steps: int = 12


@dataclass(slots=True)
class CompletionProposal:
    """Evidence-backed claim that the project goal has been reached.

    Reason refers to Fact IDs already present on the Blackboard.  Bootstrap can
    complete in its first turn, before its newly discovered Facts have IDs, so
    it instead carries zero-based indexes into that report's candidate Facts.
    Scheduler resolves those indexes only after the Facts pass validation and
    have been written to the Blackboard.
    """

    fact_ids: list[str]
    description: str
    candidate_fact_indexes: list[int] = field(default_factory=list)
    # Candidate answers are emitted explicitly by Reason.  Only the external
    # Benchmark control plane consumes them; ordinary projects ignore them.
    submissions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PseudopodReport:
    pseudopod_id: str
    intent_id: str
    mode: str
    status: str
    candidate_facts: list[FactCandidate]
    evidence_refs: list[str]
    proposed_intents: list[IntentProposal]
    tool_calls: int
    progress_score: float
    stop_reason: str
    remaining_budget: int = 0
    activity_summary: list[str] = field(default_factory=list)
    context_manifest: dict[str, Any] = field(default_factory=dict)
    candidate_hypotheses: list[HypothesisCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model_usage: dict[str, int] = field(default_factory=dict)
    model_session: dict[str, Any] = field(default_factory=dict)
    completion: CompletionProposal | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
