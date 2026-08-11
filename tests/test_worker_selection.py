from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.dispatcher.loop import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime, load_dispatch_config
from slime_cairn.domain.models import IntentProposal, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.domain.workspace import IsolatedWorkspace


class ImmediateMind:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def run_cairn_task(self, intent, facts, mode, capsule):
        self.calls.append(intent.id)
        return PseudopodReport(
            pseudopod_id="ignored",
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


class FailingMind(ImmediateMind):
    def run_cairn_task(self, intent, facts, mode, capsule):
        self.calls.append(intent.id)
        return PseudopodReport(
            pseudopod_id="ignored",
            intent_id=intent.id,
            mode=mode,
            status="failed",
            candidate_facts=[],
            candidate_hypotheses=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=0.0,
            stop_reason="fixture_worker_failure",
            errors=["fixture Worker failure"],
        )


class ScopedCancelMind(ImmediateMind):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.cancellations: list[dict[str, str | None]] = []

    def cancel_active(
        self,
        *,
        project_id: str | None = None,
        intent_id: str | None = None,
        reason: str = "cancelled",
    ) -> dict[str, int]:
        self.cancellations.append(
            {
                "project_id": project_id,
                "intent_id": intent_id,
                "reason": reason,
            }
        )
        return {"cancelled": 1}


class BlockingParallelMind(ImmediateMind):
    def __init__(self) -> None:
        super().__init__("parallel")
        self._lock = threading.Lock()
        self.started_count = 0
        self.two_started = threading.Event()
        self.release = threading.Event()

    def run_cairn_task(self, intent, facts, mode, capsule):
        with self._lock:
            self.calls.append(intent.id)
            self.started_count += 1
            if self.started_count >= 2:
                self.two_started.set()
        self.release.wait(timeout=5)
        return PseudopodReport(
            pseudopod_id="parallel-fixture",
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


class WorkerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.project = self.board.create_project(
            "worker-selection",
            "fixture.local",
            "exercise Worker selection without model routes",
            {"targets": ["fixture.local"]},
        )
        self.workspace = IsolatedWorkspace(root / "workspace")

    async def asyncTearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def _dispatcher(self, workers: list[WorkerRuntime], max_workers: int = 2) -> AsyncDispatcher:
        scheduler = Scheduler(self.board, self.project.id, workspace=self.workspace)
        return AsyncDispatcher(
            self.board,
            self.project.id,
            scheduler,
            WorkerPool(workers),
            DispatcherConfig(
                max_workers=max_workers,
                max_project_workers=max_workers,
                lease_seconds=10,
                heartbeat_interval=1,
                poll_interval=0.01,
                state_heartbeat_interval=0.01,
            ),
        )

    def _intent(self, objective: str, nutrient: float):
        return self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective=objective,
                target_entity="fixture.local",
                parent_fact_ids=[],
                context={"fixture_objective": objective},
            ),
            nutrient,
        )[0]

    def _bootstrap_intent(self, objective: str, nutrient: float):
        return self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="bootstrap",
                objective=objective,
                target_entity="fixture.local",
                parent_fact_ids=[],
                context={"fixture_objective": objective},
            ),
            nutrient,
        )[0]

    async def test_dispatcher_selects_available_worker_before_leasing_intent(self):
        high = self._intent("high priority intent", 10.0)
        low = self._intent("low priority intent", 1.0)
        unavailable_mind = ImmediateMind("unavailable")
        available_mind = ImmediateMind("available")
        unavailable = WorkerRuntime(
            "unavailable-explore",
            unavailable_mind,
            ("explore",),
            max_running=1,
            priority=0,
            running=1,
        )
        available = WorkerRuntime(
            "available-explore",
            available_mind,
            ("explore",),
            max_running=1,
            priority=10,
        )
        dispatcher = self._dispatcher([unavailable, available], max_workers=1)

        dispatched = await dispatcher._dispatch_available(max_dispatch=1)

        self.assertEqual(dispatched, 1)
        claimed_high = self.board.get_intent(self.project.id, high.id)
        untouched_low = self.board.get_intent(self.project.id, low.id)
        self.assertEqual(claimed_high.status, "running")
        self.assertEqual(claimed_high.owner is not None, True)
        self.assertEqual(claimed_high.attempts, 1)
        self.assertEqual(untouched_low.status, "pending")
        self.assertEqual(untouched_low.attempts, 0)
        running = dispatcher.status()["running_intents"]
        self.assertEqual(running[0]["worker_name"], "available-explore")
        self.assertNotIn("route_tag", running[0])
        events = self.board.list_events(self.project.id)
        selection_event = next(
            event for event in events if event["kind"] == "growth.branch_selected"
        )
        self.assertEqual(selection_event["payload"]["intent_id"], high.id)
        self.assertEqual(selection_event["payload"]["policy"], "sma_discrete")
        self.assertEqual(selection_event["payload"]["selection_mode"], "sma_attract")
        self.assertEqual(selection_event["payload"]["selected_intent_id"], high.id)
        self.assertFalse(selection_event["payload"]["fallback"])
        self.assertEqual(len(selection_event["payload"]["candidates"]), 2)
        self.assertEqual(len(selection_event["payload"]["seed_digest"]), 16)
        started_event = next(
            event for event in events if event["kind"] == "dispatcher.intent_started"
        )
        self.assertEqual(
            started_event["payload"]["growth_selection"]["seed_digest"],
            selection_event["payload"]["seed_digest"],
        )

        await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
        await dispatcher._reap_intents()

    async def test_dispatcher_skips_unserviceable_candidate_without_spending_its_attempt(self):
        bootstrap = self._bootstrap_intent("bootstrap without a Worker", 10.0)
        explore = self._intent("explore with a Worker", 1.0)
        explore_mind = ImmediateMind("explore")
        dispatcher = self._dispatcher(
            [WorkerRuntime("explore-only", explore_mind, ("explore",))],
            max_workers=1,
        )

        dispatched = await dispatcher._dispatch_available(max_dispatch=1)

        untouched = self.board.get_intent(self.project.id, bootstrap.id)
        claimed = self.board.get_intent(self.project.id, explore.id)
        self.assertEqual(dispatched, 1)
        self.assertEqual(untouched.status, "pending")
        self.assertEqual(untouched.attempts, 0)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.attempts, 1)
        selection_event = next(
            event
            for event in self.board.list_events(self.project.id)
            if event["kind"] == "growth.branch_selected"
        )
        self.assertEqual(selection_event["payload"]["policy_selected_intent_id"], bootstrap.id)
        self.assertEqual(selection_event["payload"]["selected_intent_id"], explore.id)
        self.assertTrue(selection_event["payload"]["fallback"])

        await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
        await dispatcher._reap_intents()

    async def test_no_capacity_leaves_pending_intent_and_attempt_budget_unchanged(self):
        intent = self._intent("wait for explore capacity", 1.0)
        busy = WorkerRuntime(
            "busy-explore",
            ImmediateMind("busy"),
            ("explore",),
            max_running=1,
            running=1,
        )
        dispatcher = self._dispatcher([busy], max_workers=1)

        dispatched = await dispatcher._dispatch_available(max_dispatch=1)

        current = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(dispatched, 0)
        self.assertEqual(current.status, "pending")
        self.assertEqual(current.attempts, 0)
        self.assertEqual(dispatcher.global_capacity.snapshot()["running"], 0)

    async def test_conditional_claim_loses_race_without_incrementing_attempts(self):
        intent = self._intent("claim exactly once", 1.0)
        first = self.board.claim_intent(self.project.id, intent.id, "first-owner", 10)
        second = self.board.claim_intent(self.project.id, intent.id, "second-owner", 10)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        current = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(current.owner, "first-owner")
        self.assertEqual(current.attempts, 1)

    async def test_distinct_explore_objectives_at_the_same_target_remain_parallel(self):
        route, route_created = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="enumerate routes",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            nutrient=0.8,
        )
        parameters, parameters_created = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="test request parameters",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            nutrient=0.8,
        )
        duplicate, duplicate_created = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="enumerate routes",
                target_entity="fixture.local",
                parent_fact_ids=[],
            ),
            nutrient=0.8,
        )

        self.assertTrue(route_created)
        self.assertTrue(parameters_created)
        self.assertFalse(duplicate_created)
        self.assertNotEqual(route.id, parameters.id)
        self.assertEqual(duplicate.id, route.id)
        self.assertEqual(len(self.board.list_intents(self.project.id, "pending")), 2)

    async def test_serve_fills_parallel_slots_without_waiting_for_next_interval(self):
        self._intent("parallel branch one", 1.0)
        self._intent("parallel branch two", 1.0)
        mind = BlockingParallelMind()
        dispatcher = self._dispatcher(
            [
                WorkerRuntime(
                    "parallel-explore",
                    mind,
                    ("explore",),
                    max_running=2,
                )
            ],
            max_workers=2,
        )
        dispatcher.config.poll_interval = 5.0

        started_at = time.monotonic()
        service_task = asyncio.create_task(dispatcher.serve())
        try:
            started = await asyncio.wait_for(
                asyncio.to_thread(mind.two_started.wait, 1.0),
                timeout=1.5,
            )
            self.assertTrue(started)
            self.assertLess(time.monotonic() - started_at, 1.5)
            self.assertEqual(dispatcher.global_capacity.snapshot()["running"], 2)
        finally:
            mind.release.set()
            deadline = time.monotonic() + 2
            while dispatcher._active_count() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                await dispatcher._reap_intents()
            self.board.set_project_status(self.project.id, "stopped")
            dispatcher.request_stop()
            await asyncio.wait_for(service_task, timeout=2)

    async def test_retry_rotates_workers_before_repeating_a_failed_worker(self):
        intent = self._bootstrap_intent("try every capable Worker", 1.0)
        claude = FailingMind("claude")
        codex = FailingMind("codex")
        pi = ImmediateMind("pi")
        dispatcher = self._dispatcher(
            [
                WorkerRuntime("claude", claude, ("bootstrap",), priority=0),
                WorkerRuntime("codex", codex, ("bootstrap",), priority=1),
                WorkerRuntime("pi", pi, ("bootstrap",), priority=2),
            ],
            max_workers=1,
        )
        dispatcher.config.worker_rejected_cooldown_seconds = 0
        # This test isolates capability rotation.  Retry timing is covered by
        # the durable backoff tests, so keep every candidate immediately due.
        dispatcher.config.intent_failure_backoff_seconds = 0
        dispatcher.config.intent_failure_backoff_max_seconds = 0
        dispatcher.config.max_intent_attempts = 3

        for _ in range(3):
            self.assertEqual(await dispatcher._dispatch_available(max_dispatch=1), 1)
            await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
            await dispatcher._reap_intents()

        current = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(claude.calls, [intent.id])
        self.assertEqual(codex.calls, [intent.id])
        self.assertEqual(pi.calls, [intent.id])
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.attempts, 3)

    async def test_all_worker_failures_enter_a_rotation_cooldown(self):
        intent = self._intent("pause after every provider fails", 1.0)
        first = FailingMind("first")
        second = FailingMind("second")
        dispatcher = self._dispatcher(
            [
                WorkerRuntime("first", first, ("explore",), priority=0),
                WorkerRuntime("second", second, ("explore",), priority=1),
            ],
            max_workers=2,
        )
        dispatcher.config.worker_rejected_cooldown_seconds = 0
        dispatcher.config.intent_failure_backoff_seconds = 0
        dispatcher.config.intent_failure_backoff_max_seconds = 0
        dispatcher.config.intent_worker_cycle_cooldown_seconds = 30

        self.assertEqual(await dispatcher._dispatch_available(), 1)
        await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
        await dispatcher._reap_intents()

        self.assertEqual(await dispatcher._dispatch_available(), 1)
        await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
        await dispatcher._reap_intents()

        # Both capable Workers failed, so the next queue scan must wait rather
        # than immediately repeating the same provider calls.
        self.assertEqual(await dispatcher._dispatch_available(), 0)
        self.assertIn(intent.id, dispatcher._intent_worker_cycle_until)
        dispatcher.config.intent_worker_cycle_cooldown_seconds = 0
        dispatcher._intent_worker_cycle_until[intent.id] = 0
        self.assertEqual(await dispatcher._dispatch_available(), 1)
        await asyncio.gather(*list(dispatcher._tasks), return_exceptions=True)
        await dispatcher._reap_intents()

    async def test_reason_failure_rotates_to_an_untried_worker(self):
        first = WorkerRuntime(
            "first-reason",
            FailingMind("first"),
            ("reason",),
            priority=0,
        )
        second = WorkerRuntime(
            "second-reason",
            ImmediateMind("second"),
            ("reason",),
            priority=1,
        )
        dispatcher = self._dispatcher([first, second], max_workers=1)
        dispatcher.config.reason_failure_cooldown_seconds = 0
        dispatcher.config.reason_failure_backoff_max_seconds = 0
        dispatcher._pending_reason_signals = 1

        self.assertTrue(await dispatcher._start_reason_if_ready(force=True))
        self.assertEqual(dispatcher._reason_record.worker_name, "first-reason")
        await asyncio.gather(dispatcher._reason_task, return_exceptions=True)
        await dispatcher._reap_reason()
        self.assertEqual(dispatcher._failed_reason_workers, {"first-reason"})

        self.assertTrue(await dispatcher._start_reason_if_ready(force=True))
        self.assertEqual(dispatcher._reason_record.worker_name, "second-reason")
        await asyncio.gather(dispatcher._reason_task, return_exceptions=True)
        await dispatcher._reap_reason()
        self.assertFalse(dispatcher._failed_reason_workers)

    async def test_reason_waits_when_only_untried_worker_is_busy(self):
        first = WorkerRuntime(
            "failed-reason",
            ImmediateMind("failed"),
            ("reason",),
            priority=0,
        )
        busy = WorkerRuntime(
            "busy-reason",
            ImmediateMind("busy"),
            ("reason",),
            max_running=1,
            priority=1,
            running=1,
        )
        dispatcher = self._dispatcher([first, busy], max_workers=1)
        dispatcher._failed_reason_workers.add(first.name)

        self.assertIsNone(dispatcher._acquire_reason_worker())

    def test_provider_failure_categories_use_different_cooldowns(self):
        self.assertEqual(
            AsyncDispatcher._intent_failure_category(
                "This content was flagged for possible cybersecurity risk"
            ),
            ("model_policy_filter", 300.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category(
                'ModelInvocationError: stream disconnected before completion: Upstream request failed'
            ),
            ("provider_stream_disconnect", 180.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("503 Service temporarily unavailable"),
            ("provider_unavailable", 120.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("native worker timeout after 300s"),
            ("worker_timeout", 90.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("403 Forbidden: insufficient quota"),
            ("provider_access_denied", 3600.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("native pi-cli: no assistant event"),
            ("native_protocol_no_assistant", 900.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("FileNotFoundError: [WinError 206]"),
            ("workspace_path_limit", 900.0),
        )
        self.assertEqual(
            AsyncDispatcher._intent_failure_category("--: 4: Cannot fork"),
            ("container_resource_exhausted", 300.0),
        )

    async def test_worker_pool_uses_priority_health_and_capacity(self):
        first = WorkerRuntime(
            "first",
            ImmediateMind("first"),
            ("explore",),
            max_running=1,
            priority=0,
        )
        unavailable = WorkerRuntime(
            "unhealthy",
            ImmediateMind("unhealthy"),
            ("explore",),
            max_running=1,
            priority=0,
            healthy=False,
        )
        fallback = WorkerRuntime(
            "fallback",
            ImmediateMind("fallback"),
            ("explore",),
            max_running=1,
            priority=1,
        )
        pool = WorkerPool([fallback, unavailable, first])

        selected_first = pool.try_acquire("explore")
        selected_second = pool.try_acquire("explore")
        selected_third = pool.try_acquire("explore")

        self.assertEqual(selected_first.name, "first")
        self.assertEqual(selected_second.name, "fallback")
        self.assertIsNone(selected_third)


class WorkerConfigTests(unittest.TestCase):
    def test_worker_pool_forwards_scoped_cancellation_to_native_mind(self):
        mind = ScopedCancelMind("scoped")
        pool = WorkerPool([WorkerRuntime("scoped-worker", mind, ("explore",))])

        result = pool.cancel_active(
            "scoped-worker",
            project_id="project-a",
            intent_id="intent-a",
            reason="project_stopped",
        )

        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(
            mind.cancellations,
            [
                {
                    "project_id": "project-a",
                    "intent_id": "intent-a",
                    "reason": "project_stopped",
                }
            ],
        )

    def test_route_tags_are_rejected_and_omitted_from_runtime_snapshot(self):
        worker = WorkerRuntime("fixture", ImmediateMind("fixture"), ("explore",))
        snapshot = WorkerPool([worker]).snapshot()[0]
        self.assertNotIn("route_tags", snapshot)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch.json"
            path.write_text(
                """{
                  \"runtime\": {},
                  \"workers\": [{
                    \"name\": \"fixture\",
                    \"type\": \"codex-cli\",
                    \"execution\": \"native-agent\",
                    \"task_types\": [\"bootstrap\", \"explore\", \"reason\"],
                    \"route_tags\": [\"deep\"]
                  }]
                }
                """,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "route_tags"):
                load_dispatch_config(path, lambda project_id: object())


if __name__ == "__main__":
    unittest.main()
