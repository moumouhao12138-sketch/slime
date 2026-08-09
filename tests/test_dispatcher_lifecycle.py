from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.dispatcher.loop import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.domain.models import FactCandidate, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.domain.workspace import IsolatedWorkspace


class DelayedReportMind:
    """A worker that returns only after the Dispatcher has already cancelled it."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.cancel_calls = 0
        self.cancellations: list[dict[str, str | None]] = []

    def run_cairn_task(self, intent, facts, mode, capsule):
        self.started.set()
        self.release.wait(timeout=5)
        self.returned.set()
        return PseudopodReport(
            pseudopod_id="late-report",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    self.target,
                    "late_worker_result",
                    "true",
                    0.95,
                    ["evidence://fixture/late-worker-result"],
                )
            ],
            evidence_refs=["evidence://fixture/late-worker-result"],
            proposed_intents=[],
            tool_calls=1,
            progress_score=1.0,
            stop_reason="fixture_complete",
        )

    def cancel_active(
        self,
        *,
        project_id: str | None = None,
        intent_id: str | None = None,
        reason: str = "cancelled",
    ):
        self.cancel_calls += 1
        self.cancellations.append(
            {
                "project_id": project_id,
                "intent_id": intent_id,
                "reason": reason,
            }
        )
        return {"cancelled": 1}


class DispatcherLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.target = "fixture.local"
        self.project = self.board.create_project(
            "hard-stop",
            self.target,
            "prove hard stop preserves the blackboard boundary",
            {"targets": [self.target]},
        )
        self.workspace = IsolatedWorkspace(root / "workspace")

    async def asyncTearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    async def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("timed out waiting for fixture state")
            await asyncio.sleep(0.01)

    async def test_project_stop_cancels_worker_and_fences_late_report(self):
        mind = DelayedReportMind(self.target)
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=self.workspace)
        scheduler.seed(self.target)
        pool = WorkerPool(
            [
                WorkerRuntime(
                    name="fixture-worker",
                    mind=mind,
                    task_types=("bootstrap",),
                )
            ]
        )
        dispatcher = AsyncDispatcher(
            self.board,
            self.project.id,
            scheduler,
            pool,
            DispatcherConfig(
                max_workers=1,
                max_project_workers=1,
                lease_seconds=10,
                heartbeat_interval=1,
                poll_interval=0.01,
                state_heartbeat_interval=0.01,
            ),
        )

        task = asyncio.create_task(dispatcher.serve())
        await self._wait_for(mind.started.is_set)

        self.board.set_project_status(self.project.id, "stopped")
        dispatcher.request_stop()
        result = await asyncio.wait_for(task, timeout=2)

        intent = self.board.list_intents(self.project.id)[0]
        self.assertEqual(result["project_status"], "stopped")
        self.assertGreaterEqual(mind.cancel_calls, 1)
        self.assertEqual(mind.cancellations[0]["project_id"], self.project.id)
        self.assertEqual(mind.cancellations[0]["intent_id"], intent.id)
        self.assertEqual(mind.cancellations[0]["reason"], "project_stopped")
        self.assertEqual(dispatcher.global_capacity.snapshot()["running"], 0)
        self.assertNotEqual(intent.status, "running")
        self.assertIsNone(intent.owner)
        self.assertTrue(all(run["status"] != "running" for run in self.board.list_worker_runs(self.project.id)))

        # The thread now returns a valid-looking report after its lease has
        # been revoked.  It must not revive the stopped project with a Fact.
        mind.release.set()
        await self._wait_for(mind.returned.is_set)
        await asyncio.sleep(0.05)
        self.assertFalse(
            any(
                fact.predicate == "late_worker_result"
                for fact in self.board.list_facts(self.project.id)
            )
        )


if __name__ == "__main__":
    unittest.main()
