from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
import random
from typing import Any, Iterable

from .models import Intent


GROWTH_SELECTION_MODES = frozenset({"nutrient", "sma_discrete"})
DEFAULT_GROWTH_SELECTION_MODE = "sma_discrete"
DEFAULT_GROWTH_EXPLORATION_PROBABILITY = 0.12
DEFAULT_GROWTH_EXPLORATION_MIN_PROBABILITY = 0.03
DEFAULT_GROWTH_CONVERGENCE_SELECTIONS = 24
DEFAULT_GROWTH_RANDOM_SEED = "slime-sma-v1"


@dataclass(slots=True)
class BranchSelection:
    ordered: list[Intent]
    details: dict[str, Any]


class SmaBranchPolicy:
    """Discrete SMA-inspired selection for non-numeric Intent branches."""

    def __init__(
        self,
        *,
        mode: str = DEFAULT_GROWTH_SELECTION_MODE,
        exploration_probability: float = DEFAULT_GROWTH_EXPLORATION_PROBABILITY,
        exploration_min_probability: float = DEFAULT_GROWTH_EXPLORATION_MIN_PROBABILITY,
        convergence_selections: int = DEFAULT_GROWTH_CONVERGENCE_SELECTIONS,
        random_seed: str = DEFAULT_GROWTH_RANDOM_SEED,
    ) -> None:
        if mode not in GROWTH_SELECTION_MODES:
            raise ValueError(
                "growth selection mode must be one of: "
                + ", ".join(sorted(GROWTH_SELECTION_MODES))
            )
        if not 0 <= exploration_min_probability <= exploration_probability <= 1:
            raise ValueError(
                "growth exploration probabilities must satisfy 0 <= min <= initial <= 1"
            )
        if convergence_selections < 1:
            raise ValueError("growth convergence selections must be positive")
        if not str(random_seed).strip():
            raise ValueError("growth random seed must not be empty")
        self.mode = mode
        self.exploration_probability = float(exploration_probability)
        self.exploration_min_probability = float(exploration_min_probability)
        self.convergence_selections = int(convergence_selections)
        self.random_seed = str(random_seed)

    def select(
        self,
        candidates: Iterable[Intent],
        *,
        project_id: str,
        selection_index: int = 0,
        all_intents: Iterable[Intent] | None = None,
    ) -> BranchSelection:
        items = list(candidates)
        if not items:
            return BranchSelection([], self._empty_details(selection_index))

        population = list(all_intents) if all_intents is not None else list(items)
        scored = self._score_population(items, population)
        ranked = sorted(
            scored,
            key=lambda item: (
                -item["fitness"],
                -item["intent"].nutrient,
                item["intent"].created_at,
                item["intent"].id,
            ),
        )
        for rank, item in enumerate(ranked, 1):
            item["rank"] = rank

        progress = min(1.0, max(0, selection_index) / self.convergence_selections)
        exploration_probability = (
            self.exploration_probability * (1.0 - progress)
            + self.exploration_min_probability * progress
        )
        seed_material = "|".join(
            [
                self.random_seed,
                project_id,
                str(max(0, selection_index)),
                *sorted(item["intent"].id for item in ranked),
            ]
        )
        seed_digest = sha256(seed_material.encode("utf-8")).hexdigest()
        rng = random.Random(int(seed_digest[:16], 16))
        draw = rng.random()

        if self.mode == "nutrient":
            ranked = sorted(
                ranked,
                key=lambda item: (
                    -item["intent"].nutrient,
                    item["intent"].created_at,
                    item["intent"].id,
                ),
            )
            for rank, item in enumerate(ranked, 1):
                item["rank"] = rank
            selected = ranked[0]
            selection_mode = "nutrient"
        elif len(ranked) <= 2:
            # A two-item population already receives natural diversification:
            # once the leading task runs, the other task is next in line.
            selected = ranked[0]
            selection_mode = "sma_attract"
        elif draw < exploration_probability:
            lower_half = ranked[max(1, len(ranked) // 2) :]
            selected = self._weighted_choice(
                lower_half,
                [max(0.05, 1.05 - item["fitness"]) for item in lower_half],
                rng,
            )
            selection_mode = "sma_explore"
        else:
            temperature = max(0.25, 1.0 - 0.75 * progress)
            best_fitness = ranked[0]["fitness"]
            weights = [
                math.exp((item["fitness"] - best_fitness) * 3.0 / temperature)
                for item in ranked
            ]
            selected = self._weighted_choice(ranked, weights, rng)
            selection_mode = "sma_attract"

        ordered_scored = [selected, *(item for item in ranked if item is not selected)]
        details = {
            "policy": self.mode,
            "selection_mode": selection_mode,
            "selection_index": max(0, int(selection_index)),
            "selected_intent_id": selected["intent"].id,
            "selected_rank": int(selected["rank"]),
            "population_size": len(ranked),
            "exploration_probability": round(exploration_probability, 6),
            "random_draw": round(draw, 6),
            "seed_digest": seed_digest[:16],
            "candidates": [self._public_score(item) for item in ranked],
        }
        return BranchSelection(
            [item["intent"] for item in ordered_scored],
            details,
        )

    @staticmethod
    def _weighted_choice(
        items: list[dict[str, Any]],
        weights: list[float],
        rng: random.Random,
    ) -> dict[str, Any]:
        total = sum(max(0.0, float(weight)) for weight in weights)
        if total <= 0:
            return items[0]
        threshold = rng.random() * total
        cumulative = 0.0
        for item, weight in zip(items, weights):
            cumulative += max(0.0, float(weight))
            if cumulative >= threshold:
                return item
        return items[-1]

    @classmethod
    def _score_population(
        cls,
        candidates: list[Intent],
        population: list[Intent],
    ) -> list[dict[str, Any]]:
        roots = {item.id: cls._branch_root(item) for item in population}
        branch_nutrients: dict[str, float] = {}
        for item in population:
            root_id = roots[item.id]
            nutrient = cls._finite(item.nutrient)
            branch_nutrients[root_id] = max(branch_nutrients.get(root_id, nutrient), nutrient)

        candidate_nutrients = [cls._finite(item.nutrient) for item in candidates]
        candidate_strengths = [max(0.0, cls._finite(item.strength)) for item in candidates]
        candidate_branch_nutrients = [
            branch_nutrients.get(cls._branch_root(item), cls._finite(item.nutrient))
            for item in candidates
        ]
        nutrient_bounds = (min(candidate_nutrients), max(candidate_nutrients))
        strength_bounds = (min(candidate_strengths), max(candidate_strengths))
        branch_bounds = (
            min(candidate_branch_nutrients),
            max(candidate_branch_nutrients),
        )

        scored: list[dict[str, Any]] = []
        for item, nutrient, strength, branch_nutrient in zip(
            candidates,
            candidate_nutrients,
            candidate_strengths,
            candidate_branch_nutrients,
        ):
            nutrient_score = cls._normalize(nutrient, *nutrient_bounds)
            strength_score = cls._normalize(strength, *strength_bounds)
            branch_score = cls._normalize(branch_nutrient, *branch_bounds)
            novelty = min(1.0, max(0.0, cls._finite(item.novelty)))
            failure_penalty = min(0.3, max(0, item.failure_streak) * 0.075)
            fitness = min(
                1.0,
                max(
                    0.0,
                    nutrient_score * 0.6
                    + branch_score * 0.2
                    + strength_score * 0.15
                    + novelty * 0.05
                    - failure_penalty,
                ),
            )
            scored.append(
                {
                    "intent": item,
                    "fitness": round(fitness, 6),
                    "nutrient": round(nutrient, 4),
                    "strength": round(strength, 4),
                    "branch_root_id": cls._branch_root(item),
                    "branch_nutrient": round(branch_nutrient, 4),
                    "novelty": round(novelty, 4),
                    "failure_streak": int(item.failure_streak),
                }
            )
        return scored

    @staticmethod
    def _normalize(value: float, lower: float, upper: float) -> float:
        if upper - lower <= 1e-9:
            return 0.5
        return (value - lower) / (upper - lower)

    @staticmethod
    def _finite(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if math.isfinite(result) else 0.0

    @staticmethod
    def _branch_root(intent: Intent) -> str:
        meta = intent.context.get("slime_meta", {})
        if not isinstance(meta, dict):
            return intent.id
        return str(meta.get("branch_root_id") or intent.id)

    @staticmethod
    def _public_score(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent_id": item["intent"].id,
            "rank": int(item["rank"]),
            "fitness": item["fitness"],
            "nutrient": item["nutrient"],
            "strength": item["strength"],
            "branch_root_id": item["branch_root_id"],
            "branch_nutrient": item["branch_nutrient"],
            "novelty": item["novelty"],
            "failure_streak": item["failure_streak"],
        }

    def _empty_details(self, selection_index: int) -> dict[str, Any]:
        return {
            "policy": self.mode,
            "selection_mode": "empty",
            "selection_index": max(0, int(selection_index)),
            "selected_intent_id": None,
            "population_size": 0,
            "candidates": [],
        }
