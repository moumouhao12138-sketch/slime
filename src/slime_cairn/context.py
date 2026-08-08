from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Fact, Intent, Project


@dataclass(slots=True)
class BranchSummary:
    entity: str
    important_fact_ids: list[str]
    active_intent_ids: list[str]
    latest_progress: str
    nutrient: float


@dataclass(slots=True)
class ContextManifest:
    """Audit metadata for Cairn's file-backed task context."""

    reason_kind: str = "checkpoint"
    budget: int = 0
    estimated_tokens: int = 0
    pinned_fact_ids: list[str] = field(default_factory=list)
    included_fact_ids: list[str] = field(default_factory=list)
    omitted_fact_ids: list[str] = field(default_factory=list)
    included_branch_entities: list[str] = field(default_factory=list)
    omitted_branch_entities: list[str] = field(default_factory=list)
    branch_reserved_fact_ids: list[str] = field(default_factory=list)
    revived_fact_ids: list[str] = field(default_factory=list)
    audit_fact_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    graph_snapshot_ref: str = ""
    graph_snapshot_sha256: str = ""
    graph_snapshot_counts: dict[str, int] = field(default_factory=dict)
    included_hint_ids: list[str] = field(default_factory=list)
    omitted_hint_ids: list[str] = field(default_factory=list)
    compacted_fact_ids: list[str] = field(default_factory=list)
    context_strategy: str = "cairn_graph_file"
    # Slime metadata lives beside Cairn's graph contract.  It gives a Worker
    # the active line of inquiry without changing graph.yaml's wire shape.
    branch_checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContextCapsule:
    mode: str
    objective: str
    goal: str
    scope: dict[str, Any]
    environment_brief: str
    facts: list[Fact]
    recent_fact_ids: list[str]
    previous_summary: str
    branches: list[BranchSummary]
    evidence_refs: list[str]
    manifest: ContextManifest
    hints: list[dict[str, Any]] = field(default_factory=list)
    open_intents: list[dict[str, Any]] = field(default_factory=list)


class ContextBuilder:
    """Build Cairn task descriptors; the complete graph lives in graph.yaml."""

    KALI_ENVIRONMENT_BRIEF = "Persistent Kali project container at /workspace."

    @staticmethod
    def _is_goal_fact(fact: Fact) -> bool:
        role = str(fact.attributes.get("role", "")).strip().lower()
        return role == "goal" or fact.predicate == "project_goal"

    @staticmethod
    def _is_anchor_fact(fact: Fact) -> bool:
        role = str(fact.attributes.get("role", "")).strip().lower()
        return role in {"origin", "goal"} or fact.predicate in {
            "project_origin",
            "project_goal",
        }

    def build(
        self,
        mode: str,
        project: Project,
        intent: Intent,
        facts: list[Fact],
        intents: list[Intent],
        reason_state: dict[str, Any] | None = None,
        hints: list[dict[str, Any]] | None = None,
    ) -> ContextCapsule:
        reason_state = reason_state or {}
        last_fact_time = float(reason_state.get("last_fact_time", 0.0))
        cursor_fact_ids = set(reason_state.get("last_fact_ids", []))
        recent_fact_ids = [
            fact.id
            for fact in facts
            if fact.created_at > last_fact_time
            or (fact.created_at == last_fact_time and fact.id not in cursor_fact_ids)
        ]

        # Cairn exposes every non-Goal Fact ID to Reason and stores complete
        # descriptions in graph.yaml. Fact bodies stay out of the CLI prompt.
        valid_fact_ids = [fact.id for fact in facts if not self._is_goal_fact(fact)]
        pinned_fact_ids = [
            fact.id
            for fact in facts
            if self._is_anchor_fact(fact) or fact.id in set(intent.parent_fact_ids)
        ]
        hint_items = [dict(item) for item in (hints or []) if isinstance(item, dict)]
        open_intents = [
            {
                "id": item.id,
                "from": list(item.parent_fact_ids),
                "description": item.objective,
                "worker": item.owner,
            }
            for item in intents
            if item.status in {"pending", "running"} and item.id != intent.id
        ]
        context_scope = dict(project.scope)
        context_scope.pop("hints", None)
        manifest = ContextManifest(
            reason_kind="checkpoint",
            pinned_fact_ids=pinned_fact_ids,
            included_fact_ids=valid_fact_ids,
            reasons={fact_id: "available_in_graph_yaml" for fact_id in valid_fact_ids},
            included_hint_ids=[str(item.get("id", "")) for item in hint_items if item.get("id")],
            branch_checkpoint=(
                dict(intent.context.get("slime_meta", {}))
                if isinstance(intent.context.get("slime_meta", {}), dict)
                else {}
            ),
        )
        return ContextCapsule(
            mode=mode,
            objective=intent.objective,
            goal=project.goal,
            scope=context_scope,
            environment_brief=self.KALI_ENVIRONMENT_BRIEF,
            facts=[],
            recent_fact_ids=recent_fact_ids,
            previous_summary="",
            branches=[],
            evidence_refs=[],
            manifest=manifest,
            hints=hint_items if mode == "bootstrap" else [],
            open_intents=open_intents if mode == "reason" else [],
        )
