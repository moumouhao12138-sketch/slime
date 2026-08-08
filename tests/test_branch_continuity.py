from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from slime_cairn.blackboard import Blackboard
from slime_cairn.context import ContextBuilder
from slime_cairn.models import FactCandidate, IntentProposal
from slime_cairn.scheduler import Scheduler
from slime_cairn.seeding import seed_project_context_facts
from slime_cairn.slime_layer import SlimeGrowthLayer


class BranchContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = Blackboard(self.root / "board.db")
        self.project = self.board.create_project(
            "branch-fixture",
            "fixture.local",
            "solve the fixture",
            {"origin": "fixture.local", "targets": ["fixture.local"]},
        )
        seed_project_context_facts(self.board, self.project)

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def test_child_direction_inherits_a_parent_branch_and_evidence(self) -> None:
        self.board.register_evidence(self.project.id, "evidence/root.txt", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                subject="fixture.local",
                predicate="observed",
                object="root clue",
                confidence=0.9,
                evidence_refs=["evidence/root.txt"],
            ),
        )
        root, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="test the root clue",
                target_entity="fixture.local",
                parent_fact_ids=[],
                context={"slime_meta": {"branch_root_id": "root-branch", "branch_depth": 0}},
            ),
            nutrient=1.0,
        )
        # The Fact was produced by the root path in the real runtime.
        self.board._connection.execute(
            "UPDATE facts SET source_intent_id = ? WHERE id = ?", (root.id, fact.id)
        )

        proposal, _ = SlimeGrowthLayer().decorate_intent(
            IntentProposal(
                kind="explore",
                objective="perform the smallest follow-up validation",
                target_entity="fixture.local",
                parent_fact_ids=[fact.id],
            ),
            self.board.list_facts(self.project.id),
            self.board.list_intents(self.project.id),
            reason_kind="incremental",
            revived_fact_ids=[],
        )

        checkpoint = proposal.context["slime_meta"]
        self.assertTrue(checkpoint["continuation"])
        self.assertEqual(checkpoint["branch_root_id"], "root-branch")
        self.assertEqual(checkpoint["branch_depth"], 1)
        self.assertEqual(checkpoint["predecessor_intent_ids"], [root.id])
        self.assertEqual(checkpoint["evidence_refs"], ["evidence/root.txt"])

    def test_reason_direction_has_a_durable_hypothesis_and_context_checkpoint(self) -> None:
        proposal, nutrient = SlimeGrowthLayer().decorate_intent(
            IntentProposal(
                kind="explore",
                objective="validate the only remaining route",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            self.board.list_facts(self.project.id),
            self.board.list_intents(self.project.id),
            reason_kind="incremental",
            revived_fact_ids=[],
        )
        intent, _ = self.board.add_intent(self.project.id, proposal, nutrient)
        scheduler = Scheduler(self.board, self.project.id)
        scheduler._materialize_branch_hypothesis(intent)

        hypotheses = self.board.list_hypotheses_for_intent(self.project.id, intent.id)
        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(hypotheses[0].status, "open")
        capsule = ContextBuilder().build(
            "explore",
            self.project,
            intent,
            self.board.list_facts(self.project.id),
            self.board.list_intents(self.project.id),
        )
        self.assertEqual(
            capsule.manifest.branch_checkpoint["next_validation"],
            "validate the only remaining route",
        )

        scheduler._set_branch_state(intent.id, "supported", next_validation="continue only if needed")
        updated = self.board.list_hypotheses_for_intent(self.project.id, intent.id)[0]
        self.assertEqual(updated.status, "supported")
        self.assertEqual(updated.next_validation, "continue only if needed")


if __name__ == "__main__":
    unittest.main()
