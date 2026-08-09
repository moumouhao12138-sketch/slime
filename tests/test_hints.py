from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.domain.context import ContextBuilder
from slime_cairn.dispatcher.loop import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.domain.models import Intent, Project, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.domain.workspace import IsolatedWorkspace


class HintReasonMind:
    def __init__(self) -> None:
        self.capsules = []

    def run_cairn_task(self, intent, facts, mode, capsule):
        if mode != "reason":
            raise AssertionError(f"expected reason, got {mode}")
        self.capsules.append(capsule)
        return PseudopodReport(
            pseudopod_id="hint-reason-report",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=0.0,
            stop_reason="hint_reason_complete",
        )

    def cancel_active(self):
        return {"cancelled": 0}


class FailedHintReasonMind(HintReasonMind):
    def run_cairn_task(self, intent, facts, mode, capsule):
        self.capsules.append(capsule)
        return PseudopodReport(
            pseudopod_id="failed-hint-reason-report",
            intent_id=intent.id,
            mode=mode,
            status="failed",
            candidate_facts=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=0.0,
            stop_reason="fixture_failed",
            errors=["fixture failed"],
        )


class HintBlackboardTests(unittest.TestCase):
    def test_scope_hints_import_without_mutating_scope_and_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "board.db"
            board = Blackboard(database)
            project = board.create_project(
                "hint-persistence",
                "fixture.local",
                "exercise hints",
                {
                    "targets": ["fixture.local"],
                    "hints": [{"content": "Read the marker first.", "creator": "seed"}],
                },
            )
            imported = board.ensure_scope_hints(project.id)
            self.assertEqual(len(imported), 1)
            self.assertEqual(board.get_project(project.id).scope["hints"][0]["content"], "Read the marker first.")
            board.close()

            reopened = Blackboard(database)
            try:
                hints = reopened.list_hints(project.id)
                self.assertEqual(len(hints), 1)
                self.assertEqual(hints[0].content, "Read the marker first.")
            finally:
                reopened.close()

    def test_reason_records_all_hint_ids_without_inlining_hint_bodies(self) -> None:
        project = Project(
            "project_hint_context",
            "hint-context",
            "fixture.local",
            "exercise bounded hints",
            {"targets": ["fixture.local"], "hints": [{"content": "legacy" * 1000}]},
        )
        intent = Intent(
            kind="reason",
            objective="review hints",
            target_entity="fixture.local",
            parent_fact_ids=[],
        )
        hints = [
            {
                "id": f"hint_{index}",
                "content": f"hint {index}: " + ("x" * 10_000),
                "creator": "operator",
                "created_at": float(index),
            }
            for index in range(40)
        ]
        builder = ContextBuilder()

        capsule = builder.build("reason", project, intent, [], [], hints=hints)

        self.assertEqual(capsule.hints, [])
        self.assertEqual(len(capsule.manifest.included_hint_ids), len(hints))
        self.assertEqual(capsule.manifest.omitted_hint_ids, [])
        self.assertNotIn("hints", capsule.scope)


class HintDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.project = self.board.create_project(
            "hint-dispatcher",
            "fixture.local",
            "exercise Hint-triggered Reason",
            {"targets": ["fixture.local"]},
        )
        self.workspace = IsolatedWorkspace(root / "workspace")
        self.config = DispatcherConfig(
            max_workers=1,
            max_project_workers=1,
            lease_seconds=5,
            reason_lease_seconds=5,
            heartbeat_interval=0.1,
            poll_interval=0.01,
            reason_debounce_seconds=0,
            state_heartbeat_interval=0.01,
        )

    async def asyncTearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    async def test_new_hint_wakes_reason_and_success_advances_hint_cursor(self) -> None:
        mind = HintReasonMind()
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=self.workspace)
        dispatcher = AsyncDispatcher(
            self.board,
            self.project.id,
            scheduler,
            WorkerPool([WorkerRuntime("fixture-reason", mind, ("bootstrap", "reason"))]),
            self.config,
        )
        await dispatcher.initialize()
        self.assertEqual(dispatcher._pending_reason_signals, 0)
        hint, created = self.board.add_hint(self.project.id, "Check the response marker.", "operator")
        self.assertTrue(created)

        result = await dispatcher.cycle(max_dispatch=0)
        self.assertTrue(result["reason_started"])
        self.assertIsNotNone(dispatcher._reason_task)
        await asyncio.wait_for(dispatcher._reason_task, timeout=1)
        self.assertTrue(await dispatcher._reap_reason())

        self.assertIn(hint.id, mind.capsules[-1].manifest.included_hint_ids)
        self.assertEqual(mind.capsules[-1].hints, [])
        self.assertGreaterEqual(
            self.board.get_reason_state(self.project.id)["last_hint_created_at"], hint.created_at
        )
        self.assertIn(
            "dispatcher.hint_signal",
            [event["kind"] for event in self.board.list_events(self.project.id)],
        )

    async def test_failed_reason_does_not_advance_hint_cursor(self) -> None:
        mind = FailedHintReasonMind()
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=self.workspace)
        hint, _ = self.board.add_hint(self.project.id, "Keep this pending after failure.", "operator")

        with self.assertRaisesRegex(RuntimeError, "reason worker failed"):
            await asyncio.to_thread(scheduler.reason, False, False, mind, "fixture-reason")

        state = self.board.get_reason_state(self.project.id)
        self.assertLess(state["last_hint_created_at"], hint.created_at)


if __name__ == "__main__":
    unittest.main()
