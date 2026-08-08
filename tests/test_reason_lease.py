from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from slime_cairn.blackboard import Blackboard
from slime_cairn.dispatcher import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.models import FactCandidate, PseudopodReport
from slime_cairn.scheduler import Scheduler
from slime_cairn.seeding import seed_project_context_facts
from slime_cairn.service import DispatcherService, ProjectRuntimeBinding
from slime_cairn.workspace import IsolatedWorkspace


class BlockingReasonMind:
    """Keeps a Reason task alive long enough to observe its durable lease."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.cancel_calls = 0

    def run_cairn_task(self, intent, facts, mode, capsule):
        if mode != "reason":
            raise AssertionError(f"expected reason task, got {mode}")
        self.started.set()
        self.release.wait(timeout=5)
        self.returned.set()
        return PseudopodReport(
            pseudopod_id="reason-report",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[],
            candidate_hypotheses=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=0.0,
            stop_reason="fixture_complete",
        )

    def cancel_active(self):
        self.cancel_calls += 1
        return {"cancelled": 1}


class DelayedIntentMind:
    """Returns a valid result only after Dispatcher has relinquished its intent lease."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.cancel_calls = 0

    def run_cairn_task(self, intent, facts, mode, capsule):
        self.started.set()
        self.release.wait(timeout=5)
        self.returned.set()
        return PseudopodReport(
            pseudopod_id="late-intent-report",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    self.target,
                    "late_intent_result",
                    "must_not_be_imported",
                    0.95,
                    ["evidence://fixture/late-intent-result"],
                )
            ],
            candidate_hypotheses=[],
            evidence_refs=["evidence://fixture/late-intent-result"],
            proposed_intents=[],
            tool_calls=1,
            progress_score=1.0,
            stop_reason="fixture_complete",
        )

    def cancel_active(self):
        self.cancel_calls += 1
        return {"cancelled": 1}


class ReasonLeaseBlackboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.project = self.board.create_project(
            "reason-lease",
            "fixture.local",
            "exercise durable Reason ownership",
            {"targets": ["fixture.local"]},
        )

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def _claim(self, owner: str = "reason-owner", lease_seconds: float = 30.0):
        lease = self.board.claim_reason_lease(
            self.project.id,
            owner,
            "fixture-reason",
            "fixture",
            lease_seconds,
            "reason-fixture",
        )
        self.assertIsNotNone(lease)
        return lease

    def test_single_owner_renewal_and_deterministic_expiry(self):
        claimed = self._claim()

        self.assertIsNone(
            self.board.claim_reason_lease(
                self.project.id,
                "other-owner",
                "other-reason",
                "fixture",
                30,
                "other-reason-fixture",
            )
        )
        self.assertTrue(self.board.renew_reason_lease(self.project.id, "reason-owner", 60))
        renewed = self.board.get_reason_lease(self.project.id)
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed["owner_token"], "reason-owner")
        self.assertGreaterEqual(renewed["last_heartbeat_at"], claimed["last_heartbeat_at"])
        self.assertGreater(renewed["lease_expires_at"], claimed["lease_expires_at"])

        expired = self.board.expire_reason_leases(
            self.project.id,
            as_of=renewed["lease_expires_at"] + 0.001,
        )
        self.assertEqual([lease["owner_token"] for lease in expired], ["reason-owner"])
        self.assertIsNone(self.board.get_reason_lease(self.project.id))
        self.assertFalse(self.board.renew_reason_lease(self.project.id, "reason-owner", 60))

    def test_stop_completion_and_reopen_leave_no_reason_lease(self):
        self._claim()
        stopped = self.board.set_project_status(self.project.id, "stopped")
        self.assertEqual(stopped.status, "stopped")
        self.assertIsNone(self.board.get_reason_lease(self.project.id))

        self.board.set_project_status(self.project.id, "running")
        self._claim("completion-owner")
        evidence_ref = "evidence://fixture/completion"
        self.board.register_evidence(self.project.id, evidence_ref, "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "completion_proof",
                "confirmed",
                1.0,
                [evidence_ref],
            ),
        )
        self.board.complete_project(
            self.project.id,
            [fact.id],
            "fixture completion",
            "fixture-reason",
        )
        self.assertIsNone(self.board.get_reason_lease(self.project.id))

        reopened = self.board.reopen_project(
            self.project.id,
            "fixture feedback",
            "fixture-user",
        )
        self.assertEqual(reopened["project"]["status"], "running")
        self.assertIsNone(self.board.get_reason_lease(self.project.id))


class ReasonLeaseDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.project = self.board.create_project(
            "reason-dispatcher",
            "fixture.local",
            "exercise durable Reason ownership",
            {"targets": ["fixture.local"]},
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

    def _config(self) -> DispatcherConfig:
        return DispatcherConfig(
            max_workers=1,
            max_project_workers=1,
            lease_seconds=1.0,
            reason_lease_seconds=0.2,
            heartbeat_interval=0.03,
            poll_interval=0.01,
            state_heartbeat_interval=0.01,
        )

    async def test_dispatcher_claims_heartbeats_and_releases_reason_lease(self):
        mind = BlockingReasonMind("fixture.local")
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=self.workspace)
        pool = WorkerPool([WorkerRuntime("fixture-reason", mind, ("reason",))])
        dispatcher = AsyncDispatcher(
            self.board,
            self.project.id,
            scheduler,
            pool,
            self._config(),
        )
        try:
            await dispatcher.initialize()
            self.assertTrue(await dispatcher._start_reason_if_ready(force=True, global_audit=True))
            await self._wait_for(mind.started.is_set)
            initial = self.board.get_reason_lease(self.project.id)
            self.assertIsNotNone(initial)
            self.assertEqual(initial["worker_name"], "fixture-reason")
            self.assertEqual(initial["trigger"], "global_audit")

            await asyncio.sleep(0.09)
            renewed = self.board.get_reason_lease(self.project.id)
            self.assertIsNotNone(renewed)
            self.assertEqual(renewed["owner_token"], initial["owner_token"])
            self.assertGreater(renewed["last_heartbeat_at"], initial["last_heartbeat_at"])

            mind.release.set()
            await self._wait_for(mind.returned.is_set)
            await self._wait_for(lambda: dispatcher._reason_task is not None and dispatcher._reason_task.done())
            self.assertTrue(await dispatcher._reap_reason())
            self.assertIsNone(self.board.get_reason_lease(self.project.id))
            self.assertEqual(dispatcher.global_capacity.snapshot()["running"], 0)
        finally:
            mind.release.set()
            if dispatcher._reason_task is not None:
                await self._wait_for(dispatcher._reason_task.done)
                await dispatcher._reap_reason()

    async def test_dispatcher_stop_fences_late_intent_report_while_project_stays_running(self):
        mind = DelayedIntentMind("fixture.local")
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=self.workspace)
        scheduler.seed("fixture.local")
        pool = WorkerPool([WorkerRuntime("fixture-bootstrap", mind, ("bootstrap",))])
        dispatcher = AsyncDispatcher(
            self.board,
            self.project.id,
            scheduler,
            pool,
            self._config(),
        )
        task = asyncio.create_task(dispatcher.serve())
        try:
            await self._wait_for(mind.started.is_set)
            dispatcher.request_stop()
            result = await asyncio.wait_for(task, timeout=2)

            self.assertEqual(result["project_status"], "running")
            self.assertGreaterEqual(mind.cancel_calls, 1)
            intent = self.board.list_intents(self.project.id)[0]
            self.assertEqual(intent.status, "pending")
            self.assertIsNone(intent.owner)

            mind.release.set()
            await self._wait_for(mind.returned.is_set)
            await asyncio.sleep(0.05)
            self.assertFalse(
                any(
                    fact.predicate == "late_intent_result"
                    for fact in self.board.list_facts(self.project.id)
                )
            )
            run = self.board.list_worker_runs(self.project.id)[0]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["stop_reason"], "dispatcher_stopped")
        finally:
            mind.release.set()
            if not task.done():
                dispatcher.request_stop()
                await asyncio.wait_for(task, timeout=2)

    async def test_bootstrap_disabled_uses_origin_and_goal_to_start_initial_reason(self):
        project = self.board.create_project(
            "reason-first",
            "reason-first.fixture",
            "derive the first exploration direction",
            {"targets": ["reason-first.fixture"], "bootstrap_enabled": False},
        )
        seed_project_context_facts(self.board, project)
        mind = BlockingReasonMind(project.target)
        scheduler = Scheduler(self.board, project.id, mind=mind, workspace=self.workspace)
        pool = WorkerPool([WorkerRuntime("fixture-reason", mind, ("reason",))])
        dispatcher = AsyncDispatcher(
            self.board,
            project.id,
            scheduler,
            pool,
            self._config(),
        )
        try:
            await dispatcher.initialize()
            self.assertTrue(dispatcher._pending_reason_signals)
            self.assertTrue(await dispatcher._start_reason_if_ready(force=True))
            await self._wait_for(mind.started.is_set)
        finally:
            mind.release.set()
            if dispatcher._reason_task is not None:
                await self._wait_for(dispatcher._reason_task.done)
                await dispatcher._reap_reason()

    async def test_service_seeds_bootstrap_disabled_projects_before_dispatch(self):
        project = self.board.create_project(
            "service-reason-first",
            "service.fixture",
            "derive the first exploration direction",
            {"targets": ["service.fixture"], "bootstrap_enabled": False},
        )
        mind = BlockingReasonMind(project.target)
        pool = WorkerPool([WorkerRuntime("fixture-reason", mind, ("reason",))])
        service = DispatcherService(
            self.board,
            pool,
            lambda _: ProjectRuntimeBinding(workspace=self.workspace),
            self._config(),
        )
        dispatcher = await service.register_project(project.id)
        task = service.tasks[project.id]
        try:
            await self._wait_for(mind.started.is_set)
            facts = self.board.list_facts(project.id)
            self.assertEqual({fact.attributes["role"] for fact in facts}, {"origin", "goal"})
            self.assertTrue(all(not fact.attributes["seed"] for fact in facts))
            self.assertEqual(self.board.list_intents(project.id), [])
        finally:
            mind.release.set()
            dispatcher.request_stop()
            await asyncio.wait_for(task, timeout=2)


if __name__ == "__main__":
    unittest.main()
