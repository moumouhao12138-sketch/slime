from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from slime_cairn.server import api
from slime_cairn.server.blackboard import Blackboard
from slime_cairn.domain.models import IntentProposal


class IntentRetryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.previous_board = api.board
        api.board = self.board
        self.client = TestClient(api.app)
        self.project = self.board.create_project(
            "retry-api",
            "fixture.local",
            "wait for the delayed intent",
            {"targets": ["fixture.local"]},
        )
        self.intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect the delayed branch",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            nutrient=0.8,
        )
        self.retry_not_before = time.time() + 90
        self.board._connection.execute(
            """UPDATE intents SET attempts = ?, failure_streak = ?, retry_not_before = ?,
            last_error = ? WHERE id = ? AND project_id = ?""",
            (4, 2, self.retry_not_before, "fixture worker failed", self.intent.id, self.project.id),
        )
        self.board._connection.commit()

    def tearDown(self) -> None:
        api.board = self.previous_board
        self.board.close()
        self.temporary.cleanup()

    def test_view_and_runtime_expose_retry_waiting_projection(self) -> None:
        response = self.client.get(f"/projects/{self.project.id}/view")
        runtime_response = self.client.get(f"/projects/{self.project.id}/runtime")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(runtime_response.status_code, 200)
        view = response.json()
        intent = view["intents"][0]
        node = next(node for node in view["graph"]["nodes"] if node["entity_id"] == self.intent.id)
        runtime_intent = view["runtime"]["retrying_intents"][0]

        self.assertEqual(intent["failure_streak"], 2)
        self.assertEqual(intent["retry_not_before"], self.retry_not_before)
        self.assertTrue(intent["retry_waiting"])
        self.assertGreater(intent["retry_after_seconds"], 0)
        self.assertEqual(node["failure_streak"], 2)
        self.assertTrue(node["retry_waiting"])
        self.assertEqual(view["summary"]["retry_waiting_count"], 1)
        self.assertEqual(runtime_intent["intent_id"], self.intent.id)
        self.assertEqual(runtime_intent["failure_streak"], 2)
        self.assertTrue(runtime_intent["retry_waiting"])
        self.assertEqual(runtime_response.json()["retrying_intents"][0]["intent_id"], self.intent.id)

    def test_retry_endpoint_clears_delay_without_losing_history(self) -> None:
        response = self.client.post(f"/projects/{self.project.id}/intents/{self.intent.id}/retry")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["retried"])
        self.assertEqual(payload["intent"]["id"], self.intent.id)
        self.assertEqual(payload["intent"]["status"], "pending")
        self.assertEqual(payload["intent"]["attempts"], 4)
        self.assertEqual(payload["intent"]["failure_streak"], 2)
        self.assertEqual(payload["intent"]["retry_not_before"], 0.0)

        persisted = self.board.get_intent(self.project.id, self.intent.id)
        self.assertEqual(persisted.attempts, 4)
        self.assertEqual(persisted.failure_streak, 2)
        self.assertEqual(persisted.retry_not_before, 0.0)

    def test_retry_endpoint_returns_not_found_for_unknown_intent(self) -> None:
        response = self.client.post(f"/projects/{self.project.id}/intents/intent_missing/retry")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
