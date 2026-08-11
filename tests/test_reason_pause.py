from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.dispatcher.loop import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.dispatcher.scheduler import Scheduler


class ReasonPauseTests(unittest.TestCase):
    def test_failure_state_survives_blackboard_reopen_and_can_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "board.db"
            board = Blackboard(database)
            project = board.create_project("reason-pause", "fixture.local", "goal", {})
            paused_until = time.time() + 90
            board.record_reason_failure(project.id, "same provider error", paused_until)
            board.close()

            reopened = Blackboard(database)
            state = reopened.get_reason_state(project.id)
            self.assertEqual(state["failure_streak"], 1)
            self.assertEqual(state["failure_last_error"], "same provider error")
            self.assertGreater(state["failure_paused_until"], time.time())
            reopened.clear_reason_failure(project.id)
            self.assertEqual(reopened.get_reason_state(project.id)["failure_streak"], 0)
            reopened.close()

    def test_repeated_error_enters_durable_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            board = Blackboard(Path(temporary) / "board.db")
            project = board.create_project("reason-pause", "fixture.local", "goal", {})
            scheduler = Scheduler(board, project.id)
            pool = WorkerPool([WorkerRuntime("reason-worker", object(), ("reason",))])
            config = DispatcherConfig(
                max_workers=1,
                max_project_workers=1,
                lease_seconds=5,
                reason_lease_seconds=5,
                heartbeat_interval=1,
                reason_failure_cooldown_seconds=0,
                reason_failure_pause_threshold=2,
                reason_failure_pause_seconds=20,
                reason_failure_pause_max_seconds=20,
            )
            dispatcher = AsyncDispatcher(board, project.id, scheduler, pool, config)
            dispatcher._record_reason_failure_cooldown("reason-worker", "same provider error")
            dispatcher._record_reason_failure_cooldown("reason-worker", "same provider error")
            self.assertTrue(dispatcher.status()["reason_paused"])
            self.assertGreater(dispatcher.status()["reason_cooldown_remaining"], 0)
            self.assertEqual(board.get_reason_state(project.id)["failure_streak"], 2)
            board.close()


if __name__ == "__main__":
    unittest.main()
