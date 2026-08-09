from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.dispatcher.loop import AsyncDispatcher, DispatcherConfig, WorkerPool, WorkerRuntime, load_dispatch_config
from slime_cairn.domain.models import IntentProposal, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.domain.seeding import seed_project_context_facts
from slime_cairn.dispatcher.service import DispatcherService, ProjectRuntimeBinding
from slime_cairn.domain.workspace import IsolatedWorkspace


class ImmediateReasonMind:
    def run_cairn_task(self, intent, facts, mode, capsule):
        if mode != "reason":
            raise AssertionError(f"expected Reason, got {mode}")
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


class BlockingReasonMind(ImmediateReasonMind):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run_cairn_task(self, intent, facts, mode, capsule):
        if mode != "reason":
            raise AssertionError(f"expected Reason, got {mode}")
        self.started.set()
        self.release.wait(timeout=5)
        return super().run_cairn_task(intent, facts, mode, capsule)

    def cancel_active(self):
        self.release.set()
        return {"cancelled": 1}


class OptionalBootstrapRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
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

    @staticmethod
    def _config() -> DispatcherConfig:
        return DispatcherConfig(
            max_workers=1,
            max_project_workers=1,
            lease_seconds=1.0,
            reason_lease_seconds=1.0,
            heartbeat_interval=0.05,
            poll_interval=0.01,
            state_heartbeat_interval=0.01,
        )

    def _project(self, bootstrap_enabled: bool = True):
        return self.board.create_project(
            "optional-bootstrap",
            "fixture.local",
            "derive an initial exploration direction",
            {
                "targets": ["fixture.local"],
                "origin": "https://fixture.local/",
                "bootstrap_enabled": bootstrap_enabled,
            },
        )

    def _legacy_bootstrap(self, project_id: str):
        intent, created = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="bootstrap",
                objective="collect first evidence",
                target_entity="fixture.local",
                parent_fact_ids=[],
                context={"phase": "bootstrap"},
            ),
            1.0,
        )
        self.assertTrue(created)
        return intent

    async def test_service_archives_legacy_bootstrap_without_worker_and_starts_reason(self):
        project = self._project(bootstrap_enabled=True)
        seed_project_context_facts(self.board, project)
        bootstrap = self._legacy_bootstrap(project.id)
        mind = BlockingReasonMind()
        service = DispatcherService(
            self.board,
            WorkerPool([WorkerRuntime("reason-only", mind, ("reason",))]),
            lambda _: ProjectRuntimeBinding(workspace=self.workspace),
            self._config(),
        )
        dispatcher = await service.register_project(project.id)
        task = service.tasks[project.id]
        try:
            await self._wait_for(mind.started.is_set)
            archived = self.board.get_intent(project.id, bootstrap.id)
            self.assertEqual(archived.status, "dormant")
            self.assertEqual(archived.attempts, 0)
            self.assertEqual(archived.last_error, "bootstrap_worker_unavailable")
            self.assertEqual(self.board.list_intents(project.id, "pending"), [])
            self.assertIsNotNone(dispatcher.status()["reason_running"])
            events = self.board.list_events(project.id)
            archived_events = [event for event in events if event["kind"] == "intent.bootstrap_archived"]
            self.assertEqual(len(archived_events), 1)
            self.assertEqual(archived_events[0]["payload"]["intent_ids"], [bootstrap.id])
        finally:
            mind.release.set()
            dispatcher.request_stop()
            await asyncio.wait_for(task, timeout=2)

    async def test_reason_first_no_bootstrap_worker_reaches_idle_without_blocking(self):
        project = self._project(bootstrap_enabled=True)
        seed_project_context_facts(self.board, project)
        bootstrap = self._legacy_bootstrap(project.id)
        mind = ImmediateReasonMind()
        dispatcher = AsyncDispatcher(
            self.board,
            project.id,
            Scheduler(self.board, project.id, mind=mind, workspace=self.workspace),
            WorkerPool([WorkerRuntime("reason-only", mind, ("reason",))]),
            self._config(),
        )

        result = await dispatcher.run_until_idle(max_cycles=100)

        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["completed_reason_runs"], 1)
        self.assertEqual(self.board.get_intent(project.id, bootstrap.id).status, "dormant")
        self.assertEqual([run["mode"] for run in self.board.list_worker_runs(project.id)], ["reason"])

    async def test_disabled_scope_archives_legacy_bootstrap_even_with_worker(self):
        project = self._project(bootstrap_enabled=False)
        seed_project_context_facts(self.board, project)
        bootstrap = self._legacy_bootstrap(project.id)
        reason_mind = ImmediateReasonMind()
        bootstrap_mind = ImmediateReasonMind()
        dispatcher = AsyncDispatcher(
            self.board,
            project.id,
            Scheduler(self.board, project.id, mind=reason_mind, workspace=self.workspace),
            WorkerPool(
                [
                    WorkerRuntime("bootstrap", bootstrap_mind, ("bootstrap",)),
                    WorkerRuntime("reason", reason_mind, ("reason",)),
                ]
            ),
            self._config(),
        )

        await dispatcher.initialize()

        archived = self.board.get_intent(project.id, bootstrap.id)
        self.assertEqual(archived.status, "dormant")
        self.assertEqual(archived.last_error, "bootstrap_disabled")
        self.assertTrue(dispatcher.status()["pending_reason_signals"])


class OptionalBootstrapConfigTests(unittest.TestCase):
    def _write_config(self, root: Path, workers: list[dict]) -> Path:
        path = root / "dispatch.json"
        import json

        path.write_text(json.dumps({"runtime": {}, "workers": workers}), encoding="utf-8")
        return path

    def test_config_allows_explore_and_reason_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_config(
                Path(temporary),
                [
                    {
                        "name": "explore",
                        "type": "codex-cli",
                        "execution": "native-agent",
                        "task_types": ["explore"],
                    },
                    {
                        "name": "reason",
                        "type": "pi-cli",
                        "execution": "native-agent",
                        "task_types": ["reason"],
                    },
                ],
            )

            _, pool = load_dispatch_config(path, lambda project_id: object())

            self.assertEqual(pool.configured_task_types(), {"explore", "reason"})

    def test_config_still_requires_explore_and_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_config(
                Path(temporary),
                [
                    {
                        "name": "reason",
                        "type": "pi-cli",
                        "execution": "native-agent",
                        "task_types": ["reason"],
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "explore"):
                load_dispatch_config(path, lambda project_id: object())


if __name__ == "__main__":
    unittest.main()
