from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.domain.context import ContextBuilder
from slime_cairn.domain.models import IntentProposal
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.domain.seeding import seed_project_context_facts
from slime_cairn.domain.workspace import IsolatedWorkspace


class CairnGraphSnapshotTests(unittest.TestCase):
    def test_snapshot_matches_cairn_graph_shape_and_excludes_runtime_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = Blackboard(root / "board.db")
            project = board.create_project(
                "graph-fixture",
                "fixture.local",
                "reach the fixture goal",
                {"origin": "fixture.local", "targets": ["fixture.local"]},
            )
            seed_project_context_facts(board, project)
            intent, _ = board.add_intent(
                project.id,
                IntentProposal(
                    kind="explore",
                    objective="inspect the fixture route",
                    target_entity="fixture.local",
                    parent_fact_ids=[],
                ),
                nutrient=1.0,
            )
            facts = board.list_facts(project.id)
            capsule = ContextBuilder().build(
                "explore",
                project,
                intent,
                facts,
                board.list_intents(project.id),
            )
            workspace = IsolatedWorkspace(root / "workspace")
            scheduler = Scheduler(board, project.id, workspace=workspace)

            scheduler._attach_graph_snapshot(capsule, intent, "explore")

            self.assertTrue(capsule.manifest.graph_snapshot_ref.endswith("/graph.yaml"))
            graph = yaml.safe_load(workspace.read_text(capsule.manifest.graph_snapshot_ref, 100_000))
            self.assertEqual(set(graph), {"project", "facts", "intents"})
            self.assertEqual(graph["project"]["goal"], project.goal)
            self.assertEqual(
                {item["id"] for item in graph["facts"]},
                {fact.id for fact in facts},
            )
            self.assertNotIn("worker_runs", graph)
            self.assertNotIn("reason_state", graph)
            self.assertNotIn("evidence", graph)
            board.close()


if __name__ == "__main__":
    unittest.main()
