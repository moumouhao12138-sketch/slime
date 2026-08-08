from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from slime_cairn.blackboard import Blackboard
from slime_cairn.models import IntentProposal


class IntentRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "board.db"
        self.board = Blackboard(self.path)
        self.project = self.board.create_project(
            "retry-fixture",
            "fixture.local",
            "exercise durable intent retries",
            {"targets": ["fixture.local"]},
        )

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def _intent(self):
        return self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect retry behavior",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            nutrient=1.0,
        )[0]

    def test_failure_uses_durable_backoff_and_completion_clears_streak(self) -> None:
        intent = self._intent()
        claimed = self.board.claim_intent(self.project.id, intent.id, "owner-one", 30)
        self.assertIsNotNone(claimed)

        status = self.board.release_intent(
            self.project.id,
            intent.id,
            "owner-one",
            "fixture failure one",
            retry_base_seconds=10,
            retry_max_seconds=60,
        )

        delayed = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(status, "pending")
        self.assertEqual(delayed.failure_streak, 1)
        self.assertGreater(delayed.retry_not_before, time.time())
        self.assertIsNone(self.board.claim_intent(self.project.id, intent.id, "too-early", 30))
        self.assertEqual(
            [item.id for item in self.board.list_runnable_intents(self.project.id, delayed.retry_not_before)],
            [intent.id],
        )

        self.board._connection.execute(
            "UPDATE intents SET retry_not_before = 0.0 WHERE id = ?", (intent.id,)
        )
        claimed_again = self.board.claim_intent(self.project.id, intent.id, "owner-two", 30)
        self.assertIsNotNone(claimed_again)
        self.board.release_intent(
            self.project.id,
            intent.id,
            "owner-two",
            "fixture failure two",
            retry_base_seconds=10,
            retry_max_seconds=60,
        )
        second_delay = self.board.get_intent(self.project.id, intent.id).retry_not_before - time.time()
        self.assertGreater(second_delay, 19)
        self.assertLessEqual(second_delay, 20.1)

        self.board._connection.execute(
            "UPDATE intents SET retry_not_before = 0.0 WHERE id = ?", (intent.id,)
        )
        final_claim = self.board.claim_intent(self.project.id, intent.id, "owner-three", 30)
        self.assertIsNotNone(final_claim)
        self.assertTrue(self.board.finish_intent(self.project.id, intent.id, owner="owner-three"))
        completed = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.failure_streak, 0)
        self.assertEqual(completed.retry_not_before, 0.0)

    def test_zero_attempt_cap_keeps_ordinary_failures_recoverable(self) -> None:
        intent = self._intent()
        for index in range(4):
            owner = f"owner-{index}"
            claimed = self.board.claim_intent(
                self.project.id,
                intent.id,
                owner,
                30,
                max_attempts=0,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(
                self.board.release_intent(
                    self.project.id,
                    intent.id,
                    owner,
                    f"fixture failure {index}",
                    max_attempts=0,
                    retry_base_seconds=0,
                    retry_max_seconds=0,
                ),
                "pending",
            )
        current = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(current.status, "pending")
        self.assertEqual(current.attempts, 4)
        self.assertEqual(current.failure_streak, 4)

    def test_manual_retry_overrides_an_explicit_automatic_cap(self) -> None:
        intent = self._intent()
        claimed = self.board.claim_intent(
            self.project.id,
            intent.id,
            "capped-owner",
            30,
            max_attempts=1,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(
            self.board.release_intent(
                self.project.id,
                intent.id,
                "capped-owner",
                "fixture capped failure",
                max_attempts=1,
            ),
            "failed",
        )

        self.board.retry_intent(self.project.id, intent.id)
        retried = self.board.claim_intent(
            self.project.id,
            intent.id,
            "manual-owner",
            30,
            max_attempts=1,
        )
        self.assertIsNotNone(retried)
        self.assertEqual(retried.attempts, 2)

    def test_open_queue_reopens_legacy_terminal_failures_without_erasing_history(self) -> None:
        intent = self._intent()
        claimed = self.board.claim_intent(
            self.project.id,
            intent.id,
            "legacy-owner",
            30,
            max_attempts=1,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(
            self.board.release_intent(
                self.project.id,
                intent.id,
                "legacy-owner",
                "legacy capped failure",
                max_attempts=1,
            ),
            "failed",
        )

        reopened = self.board.reopen_failed_intents(self.project.id)

        current = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(reopened, [intent.id])
        self.assertEqual(current.status, "pending")
        self.assertEqual(current.attempts, 1)
        self.assertEqual(current.failure_streak, 1)
        self.assertEqual(current.last_error, "legacy capped failure")
        self.assertEqual(current.retry_not_before, 0.0)

    def test_expired_lease_recovers_with_persistent_retry_time(self) -> None:
        intent = self._intent()
        claimed = self.board.claim_intent(self.project.id, intent.id, "expired-owner", 30)
        self.assertIsNotNone(claimed)
        self.board._connection.execute(
            "UPDATE intents SET lease_expires_at = ? WHERE id = ?",
            (time.time() - 1, intent.id),
        )
        self.board.close()
        self.board = Blackboard(self.path)

        result = self.board.recover_expired_intents(
            self.project.id,
            retry_base_seconds=15,
            retry_max_seconds=60,
        )

        recovered = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(result["recovered"], [intent.id])
        self.assertEqual(result["failed"], [])
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.failure_streak, 1)
        self.assertGreater(recovered.retry_not_before, time.time())
        self.assertIsNone(self.board.claim_intent(self.project.id, intent.id, "too-early", 30))


if __name__ == "__main__":
    unittest.main()
