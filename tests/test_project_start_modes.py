from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from slime_cairn.server import api
from slime_cairn.server.blackboard import Blackboard


class ProjectStartModeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.previous_board = api.board
        api.board = self.board
        self.client = TestClient(api.app)

    def tearDown(self) -> None:
        api.board = self.previous_board
        self.board.close()
        self.temporary.cleanup()

    def test_explicit_start_modes_persist_and_seed_expected_intent(self) -> None:
        for mode, bootstrap_expected in (("growth", False), ("direct", True), ("hybrid", True)):
            with self.subTest(mode=mode):
                response = self.client.post(
                    "/projects",
                    json={
                        "name": f"fixture-{mode}",
                        "target": "fixture.local",
                        "goal": "reach the fixture goal",
                        "start_mode": mode,
                        # Explicit start_mode must win over the compatibility field.
                        "bootstrap_enabled": False,
                    },
                )
                self.assertEqual(response.status_code, 200)
                project = response.json()["project"]
                self.assertEqual(project["scope"]["start_mode"], mode)
                self.assertEqual(project["scope"]["bootstrap_enabled"], bootstrap_expected)
                intents = self.board.list_intents(project["id"])
                self.assertEqual([intent.kind for intent in intents], ["bootstrap"] if bootstrap_expected else [])

    def test_legacy_bootstrap_enabled_still_maps_to_direct(self) -> None:
        response = self.client.post(
            "/projects",
            json={
                "name": "legacy-direct",
                "target": "fixture.local",
                "goal": "reach the fixture goal",
                "bootstrap_enabled": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        project = response.json()["project"]
        self.assertEqual(project["scope"]["start_mode"], "direct")
        self.assertTrue(project["scope"]["bootstrap_enabled"])


if __name__ == "__main__":
    unittest.main()
