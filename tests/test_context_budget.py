from __future__ import annotations

import unittest

from slime_cairn.domain.context import ContextBuilder
from slime_cairn.domain.models import Fact, Intent, Project


class CairnFileContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project(
            "project-context-budget",
            "context-budget",
            "fixture.local",
            "reach the goal",
            {"targets": ["fixture.local"]},
        )

    def test_fact_bodies_stay_out_of_the_task_capsule(self) -> None:
        facts = [
            Fact(
                subject="fixture.local",
                predicate="observation",
                object=f"old-{index}" + (" x" * 500),
                confidence=0.8,
                evidence_refs=[f"evidence-{index}"],
                memory_state="revived",
                created_at=float(index),
                attributes={"large": "metadata" * 100},
            )
            for index in range(80)
        ]
        intent = Intent(
            kind="explore",
            objective="inspect the current branch",
            target_entity="fixture.local",
            parent_fact_ids=[facts[0].id],
        )

        capsule = ContextBuilder().build("explore", self.project, intent, facts, [])

        self.assertIn(facts[0].id, capsule.manifest.included_fact_ids)
        self.assertIn(facts[-1].id, capsule.manifest.included_fact_ids)
        self.assertEqual(capsule.manifest.omitted_fact_ids, [])
        self.assertEqual(capsule.facts, [])
        self.assertEqual(capsule.manifest.budget, 0)
        self.assertEqual(capsule.manifest.context_strategy, "cairn_graph_file")

    def test_goal_is_an_anchor_but_not_a_valid_reason_source(self) -> None:
        origin = Fact(
            subject="fixture.local",
            predicate="project_origin",
            object="fixture.local",
            confidence=1.0,
            evidence_refs=[],
            attributes={"role": "origin"},
        )
        goal = Fact(
            subject="fixture.local",
            predicate="project_goal",
            object="reach the goal",
            confidence=1.0,
            evidence_refs=[],
            attributes={"role": "goal"},
        )
        intent = Intent(
            kind="reason",
            objective="plan next direction",
            target_entity="fixture.local",
            parent_fact_ids=[],
        )

        capsule = ContextBuilder().build("reason", self.project, intent, [origin, goal], [])

        self.assertIn(origin.id, capsule.manifest.included_fact_ids)
        self.assertNotIn(goal.id, capsule.manifest.included_fact_ids)
        self.assertIn(origin.id, capsule.manifest.pinned_fact_ids)
        self.assertIn(goal.id, capsule.manifest.pinned_fact_ids)


if __name__ == "__main__":
    unittest.main()
