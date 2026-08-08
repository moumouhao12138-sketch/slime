from __future__ import annotations

import unittest

from slime_cairn.branch_policy import SmaBranchPolicy
from slime_cairn.models import Intent


def intent(
    intent_id: str,
    nutrient: float,
    *,
    strength: float = 1.0,
    branch_root_id: str = "",
    failure_streak: int = 0,
    novelty: float = 0.5,
    created_at: float = 1.0,
) -> Intent:
    context = (
        {"slime_meta": {"branch_root_id": branch_root_id}}
        if branch_root_id
        else {}
    )
    return Intent(
        id=intent_id,
        project_id="project-policy",
        kind="explore",
        objective=intent_id,
        target_entity="fixture.local",
        parent_fact_ids=[],
        nutrient=nutrient,
        strength=strength,
        failure_streak=failure_streak,
        novelty=novelty,
        context=context,
        created_at=created_at,
    )


class SmaBranchPolicyTests(unittest.TestCase):
    def test_nutrient_mode_preserves_strict_descending_order(self):
        policy = SmaBranchPolicy(mode="nutrient")
        candidates = [
            intent("low", 1.0, created_at=1.0),
            intent("high-new", 9.0, created_at=3.0),
            intent("high-old", 9.0, created_at=2.0),
        ]

        selection = policy.select(candidates, project_id="project-policy")

        self.assertEqual(
            [item.id for item in selection.ordered],
            ["high-old", "high-new", "low"],
        )
        self.assertEqual(selection.details["selection_mode"], "nutrient")

    def test_small_population_attracts_to_the_best_candidate(self):
        selection = SmaBranchPolicy().select(
            [intent("low", 1.0), intent("high", 10.0)],
            project_id="project-policy",
        )

        self.assertEqual(selection.ordered[0].id, "high")
        self.assertEqual(selection.details["selection_mode"], "sma_attract")
        self.assertEqual(selection.details["selected_rank"], 1)

    def test_forced_exploration_selects_from_the_lower_ranked_half(self):
        policy = SmaBranchPolicy(
            exploration_probability=1.0,
            exploration_min_probability=1.0,
        )
        candidates = [intent(f"branch-{index}", float(index)) for index in range(1, 6)]

        selection = policy.select(candidates, project_id="project-policy")

        self.assertEqual(selection.details["selection_mode"], "sma_explore")
        self.assertGreaterEqual(selection.details["selected_rank"], 3)

    def test_seeded_selection_is_reproducible(self):
        policy = SmaBranchPolicy(random_seed="fixture-seed")
        candidates = [intent(f"branch-{index}", float(index)) for index in range(1, 7)]

        first = policy.select(
            candidates,
            project_id="project-policy",
            selection_index=4,
        )
        second = policy.select(
            list(reversed(candidates)),
            project_id="project-policy",
            selection_index=4,
        )

        self.assertEqual(first.details["seed_digest"], second.details["seed_digest"])
        self.assertEqual(first.ordered[0].id, second.ordered[0].id)
        self.assertEqual(first.details["random_draw"], second.details["random_draw"])

    def test_branch_history_reinforces_a_child_candidate(self):
        root = intent("strong-root", 100.0)
        root.status = "completed"
        child = intent("strong-child", 5.0, branch_root_id=root.id)
        isolated = intent("isolated", 6.0)
        weak = intent("weak", 1.0)

        selection = SmaBranchPolicy(
            exploration_probability=0.0,
            exploration_min_probability=0.0,
        ).select(
            [child, isolated, weak],
            project_id="project-policy",
            all_intents=[root, child, isolated, weak],
        )

        ranked_ids = [item["intent_id"] for item in selection.details["candidates"]]
        self.assertEqual(ranked_ids[0], child.id)
        child_score = selection.details["candidates"][0]
        self.assertEqual(child_score["branch_root_id"], root.id)
        self.assertEqual(child_score["branch_nutrient"], 100.0)

    def test_branch_history_does_not_invent_zero_for_negative_scores(self):
        root = intent("negative-root", -10.0)
        child = intent("negative-child", -12.0, branch_root_id=root.id)
        isolated = intent("negative-isolated", -5.0)

        selection = SmaBranchPolicy().select(
            [child, isolated],
            project_id="project-policy",
            all_intents=[root, child, isolated],
        )

        child_score = next(
            item
            for item in selection.details["candidates"]
            if item["intent_id"] == child.id
        )
        self.assertEqual(child_score["branch_nutrient"], -10.0)

    def test_exploration_probability_converges_to_configured_floor(self):
        policy = SmaBranchPolicy(
            exploration_probability=0.2,
            exploration_min_probability=0.03,
            convergence_selections=10,
        )
        candidates = [intent(f"branch-{index}", float(index)) for index in range(1, 4)]

        initial = policy.select(candidates, project_id="project-policy", selection_index=0)
        converged = policy.select(candidates, project_id="project-policy", selection_index=10)
        later = policy.select(candidates, project_id="project-policy", selection_index=100)

        self.assertEqual(initial.details["exploration_probability"], 0.2)
        self.assertEqual(converged.details["exploration_probability"], 0.03)
        self.assertEqual(later.details["exploration_probability"], 0.03)

    def test_invalid_policy_configuration_is_rejected(self):
        for kwargs in (
            {"mode": "unknown"},
            {"exploration_probability": -0.1},
            {
                "exploration_probability": 0.1,
                "exploration_min_probability": 0.2,
            },
            {"convergence_selections": 0},
            {"random_seed": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SmaBranchPolicy(**kwargs)


if __name__ == "__main__":
    unittest.main()
