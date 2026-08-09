from __future__ import annotations

from .models import Fact, IntentProposal


class NutrientEngine:
    """Scores growth directions and rewards evidence-producing paths."""

    def score(self, proposal: IntentProposal) -> float:
        raw = (
            proposal.expected_value * 45
            + proposal.novelty * 30
            - proposal.cost * 20
            - proposal.risk * 100
        )
        return round(max(0.0, raw), 2)

    def reward(self, facts: list[Fact], duplicate_count: int = 0) -> float:
        if not facts:
            return -0.25
        confidence = sum(fact.confidence for fact in facts) / len(facts)
        novelty_bonus = max(0, len(facts) - duplicate_count) * 0.25
        return round(max(-0.25, confidence * 0.5 + novelty_bonus), 3)

