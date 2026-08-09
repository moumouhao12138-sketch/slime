from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import Fact, Intent, IntentProposal, fingerprint
from .nutrients import NutrientEngine


class SlimeGrowthLayer:
    """Physarum policy layered on top of Cairn's Fact/Intent contract.

    Workers speak only Cairn's compact protocol. This layer derives branch,
    memory and scheduling metadata after the model response has been parsed,
    so growth heuristics never become part of the core task contract.
    """

    SCHEMA_VERSION = 1

    def __init__(self, nutrients: NutrientEngine | None = None) -> None:
        self.nutrients = nutrients or NutrientEngine()

    @staticmethod
    def _clamp(value: float) -> float:
        return round(min(1.0, max(0.0, value)), 4)

    def decorate_intent(
        self,
        proposal: IntentProposal,
        facts: list[Fact],
        open_intents: list[Intent],
        *,
        reason_kind: str,
        revived_fact_ids: list[str],
    ) -> tuple[IntentProposal, float]:
        """Attach derived Slime metadata and return its materialized score."""

        fact_by_id = {fact.id: fact for fact in facts}
        supporting = [
            fact_id
            for fact_id in dict.fromkeys(proposal.parent_fact_ids)
            if fact_id in fact_by_id
        ]
        source_facts = [fact_by_id[fact_id] for fact_id in supporting]
        source_branches = sorted({fact.subject for fact in source_facts})
        intents_by_id = {item.id: item for item in open_intents}
        predecessor_ids = sorted(
            {
                fact.source_intent_id
                for fact in source_facts
                if fact.source_intent_id and fact.source_intent_id in intents_by_id
            }
        )
        predecessors = [intents_by_id[item_id] for item_id in predecessor_ids]
        predecessor_pairs = [
            (item, item.context.get("slime_meta", {}))
            for item in predecessors
            if isinstance(item.context.get("slime_meta", {}), dict)
        ]
        inherited_roots = {
            str(meta.get("branch_root_id") or item.id)
            for item, meta in predecessor_pairs
        }
        inherited_depths = [int(meta.get("branch_depth", 0) or 0) for _, meta in predecessor_pairs]
        confidence = (
            sum(fact.confidence for fact in source_facts) / len(source_facts)
            if source_facts
            else 0.5
        )
        structural = max(
            (fact.structural_importance for fact in source_facts),
            default=0.0,
        )
        overlapping = sum(
            1
            for intent in open_intents
            if set(intent.parent_fact_ids) & set(supporting)
            and intent.objective.strip().casefold() == proposal.objective.strip().casefold()
        )
        revived = sorted(set(revived_fact_ids) & set(supporting))

        expected_value = self._clamp(0.42 + confidence * 0.38 + structural * 0.12)
        novelty = self._clamp(0.76 - overlapping * 0.28 + min(len(source_branches), 3) * 0.04)
        cost = self._clamp(proposal.cost if proposal.cost != 0.2 else 0.18 + len(supporting) * 0.03)
        risk = self._clamp(proposal.risk)

        scored = replace(
            proposal,
            expected_value=expected_value,
            novelty=novelty,
            cost=cost,
            risk=risk,
        )
        nutrient = self.nutrients.score(scored)
        branch_root_id = sorted(inherited_roots)[0] if inherited_roots else ""
        branch_depth = max(inherited_depths, default=-1) + 1
        branch_key = fingerprint(
            branch_root_id or source_branches,
            supporting,
            scored.target_entity,
        )
        slime_meta: dict[str, Any] = {
            "schema": self.SCHEMA_VERSION,
            "role": "pseudopod",
            "branch_key": branch_key,
            "branch_root_id": branch_root_id,
            "branch_depth": branch_depth,
            "predecessor_intent_ids": predecessor_ids,
            "continuation": bool(predecessor_ids),
            "source_fact_ids": supporting,
            "source_branches": source_branches,
            "evidence_refs": sorted(
                {reference for fact in source_facts for reference in fact.evidence_refs}
            ),
            "next_validation": scored.objective,
            "revived_fact_ids": revived,
            "reason_kind": reason_kind,
            "scheduler": {
                "nutrient": nutrient,
                "expected_value": expected_value,
                "novelty": novelty,
                "cost": cost,
                "risk": risk,
                "overlapping_open_intents": overlapping,
            },
        }
        context = {**proposal.context, "slime_meta": slime_meta}
        provenance = {
            **proposal.provenance,
            "cairn": {
                "from": supporting,
                "description": proposal.objective,
            },
        }
        return replace(scored, context=context, provenance=provenance), nutrient

    @classmethod
    def fact_attributes(
        cls,
        *,
        mode: str,
        intent_id: str,
        description: str,
    ) -> dict[str, Any]:
        return {
            "cairn": {
                "contract": "fact",
                "description": description,
                "source_intent_id": intent_id,
            },
            "slime_meta": {
                "schema": cls.SCHEMA_VERSION,
                "role": "confirmed_growth",
                "phase": mode,
                "memory": "active",
            },
        }
