from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from slime_cairn.server import api
from slime_cairn.server.blackboard import Blackboard
from slime_cairn.dispatcher.loop import DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.workers.execution import CommandExecution
from slime_cairn.domain.models import FactCandidate, HypothesisCandidate, IntentProposal
from slime_cairn.dispatcher.service import DispatcherService, ProjectRuntimeBinding
from slime_cairn.workers.manager import WorkerManager
from slime_cairn.domain.workspace import IsolatedWorkspace


CHILD_TABLES = (
    "project_completions",
    "hints",
    "facts",
    "intents",
    "actions",
    "evidence_records",
    "hypotheses",
    "worker_runs",
    "path_edges",
    "fact_relations",
    "events",
    "reason_state",
    "reason_leases",
    "dispatcher_states",
)


class ProjectDeletionBlackboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.project = self.board.create_project(
            "delete-fixture",
            "fixture.local",
            "remove all durable project records",
            {"targets": ["fixture.local"]},
        )

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def test_final_delete_removes_every_project_child_record(self) -> None:
        evidence = "evidence://fixture/delete"
        self.board.register_evidence(self.project.id, evidence, "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "reachable", "true", 1.0, [evidence]),
        )
        intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect the deletion fixture",
                target_entity="fixture.local",
                parent_fact_ids=[fact.id],
            ),
            nutrient=0.8,
        )
        claimed = self.board.claim_next_intent(self.project.id, "fixture-worker")
        self.assertIsNotNone(claimed)
        self.board.start_worker_run(
            "worker-delete-fixture",
            self.project.id,
            intent.id,
            "explore",
            {},
            1.0,
            worker_name="fixture-worker",
            owner_token="fixture-worker",
        )
        self.board.add_hypothesis(
            self.project.id,
            HypothesisCandidate(
                statement="the fixture is removable",
                supporting_fact_ids=[fact.id],
                evidence_refs=[evidence],
                source_intent_id=intent.id,
            ),
        )
        self.board.add_fact_relation(self.project.id, fact.id, "fact-other", "supports")
        self.board.add_hint(self.project.id, "Delete the fixture after verification.")
        self.board.begin_action(self.project.id, "fixture", {"operation": "delete"})
        self.board.save_reason_state(self.project.id, 1.0, 1, "fixture state")
        self.board.complete_project(
            self.project.id,
            [fact.id],
            "fixture completion before deletion",
            "fixture-worker",
        )

        deleting, accepted = self.board.request_project_deletion(self.project.id)

        self.assertTrue(accepted)
        self.assertEqual(deleting.status, "deleting")
        self.assertNotIn(self.project.id, {item.id for item in self.board.list_projects()})
        self.board.delete_project(self.project.id)

        with self.assertRaises(KeyError):
            self.board.get_project(self.project.id)
        for table in CHILD_TABLES:
            count = self.board._connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE project_id = ?",
                (self.project.id,),
            ).fetchone()["count"]
            self.assertEqual(count, 0, table)
        self.assertEqual(self.board._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_deleting_project_cannot_resume(self) -> None:
        self.board.request_project_deletion(self.project.id)

        with self.assertRaisesRegex(ValueError, "删除"):
            self.board.set_project_status(self.project.id, "running")


class ProjectDeletionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.previous_board = api.board
        api.board = self.board
        self.client = TestClient(api.app)
        self.project = self.board.create_project(
            "api-delete-fixture",
            "fixture.local",
            "exercise deletion endpoint",
            {"targets": ["fixture.local"]},
        )

    def tearDown(self) -> None:
        api.board = self.previous_board
        self.board.close()
        self.temporary.cleanup()

    def test_delete_is_idempotent_hides_project_and_fences_writes(self) -> None:
        first = self.client.delete(f"/projects/{self.project.id}")
        second = self.client.delete(f"/projects/{self.project.id}")

        self.assertEqual(first.status_code, 202)
        self.assertTrue(first.json()["accepted"])
        self.assertEqual(second.status_code, 202)
        self.assertFalse(second.json()["accepted"])
        self.assertEqual(self.client.get("/projects").json()["projects"], [])
        self.assertEqual(
            self.client.put(
                f"/projects/{self.project.id}/status", json={"status": "running"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/projects/{self.project.id}/hints", json={"content": "late write"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/projects/{self.project.id}/evidence",
                json={"evidence_ref": "evidence://late"},
            ).status_code,
            409,
        )

        self.board.delete_project(self.project.id)
        self.assertEqual(self.client.get(f"/projects/{self.project.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/projects/{self.project.id}/view").status_code, 404)

    def test_bulk_delete_deduplicates_and_reports_each_outcome(self) -> None:
        second = self.board.create_project(
            "api-delete-second",
            "second.fixture.local",
            "exercise bulk deletion",
            {"targets": ["second.fixture.local"]},
        )
        already_deleting = self.board.create_project(
            "api-delete-in-progress",
            "third.fixture.local",
            "exercise idempotent bulk deletion",
            {"targets": ["third.fixture.local"]},
        )
        self.board.request_project_deletion(already_deleting.id)

        response = self.client.post(
            "/projects/bulk-delete",
            json={
                "project_ids": [
                    self.project.id,
                    second.id,
                    self.project.id,
                    already_deleting.id,
                    "project-missing",
                ]
            },
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["requested"], 4)
        self.assertEqual(payload["accepted"], [self.project.id, second.id])
        self.assertEqual(payload["already_deleting"], [already_deleting.id])
        self.assertEqual(payload["not_found"], ["project-missing"])
        self.assertEqual(
            [item["status"] for item in payload["results"]],
            ["accepted", "accepted", "already_deleting", "not_found"],
        )
        self.assertEqual(self.client.get("/projects").json()["projects"], [])

    def test_bulk_delete_requires_at_least_one_project(self) -> None:
        response = self.client.post("/projects/bulk-delete", json={"project_ids": []})

        self.assertEqual(response.status_code, 422)


class PassiveMind:
    def healthcheck(self) -> dict:
        return {"healthy": True}

    def run_cairn_task(self, *args, **kwargs):
        raise AssertionError("the deletion fixture must not launch a worker task")


class RecordingRuntimeFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[str] = []
        self.cleaned: list[str] = []
        self.deleted: list[str] = []

    def __call__(self, project) -> ProjectRuntimeBinding:
        workspace = IsolatedWorkspace(self.root / project.id)
        workspace.root.mkdir(parents=True, exist_ok=True)
        self.created.append(project.id)

        def cleanup() -> None:
            self.cleaned.append(project.id)

        return ProjectRuntimeBinding(workspace=workspace, cleanup=cleanup)

    def delete_project(self, project_id: str) -> None:
        self.deleted.append(project_id)
        workspace = self.root / project_id
        if workspace.exists():
            shutil.rmtree(workspace)


class DeletingRuntimeFactory(RecordingRuntimeFactory):
    def __init__(self, root: Path, board: Blackboard) -> None:
        super().__init__(root)
        self.board = board

    def __call__(self, project) -> ProjectRuntimeBinding:
        binding = super().__call__(project)
        self.board.request_project_deletion(project.id)
        return binding


class ProjectDeletionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.factory = RecordingRuntimeFactory(root / "workspaces")
        pool = WorkerPool([WorkerRuntime("fixture-explore", PassiveMind(), ("explore",))])
        self.service = DispatcherService(
            self.board,
            pool,
            self.factory,
            DispatcherConfig(
                max_workers=1,
                max_project_workers=1,
                poll_interval=0.01,
                state_heartbeat_interval=0.01,
                # This fixture tests runtime deletion, not startup health or
                # required task-mode coverage.
                worker_healthcheck="disabled",
            ),
            discovery_interval=0.01,
        )

    async def asyncTearDown(self) -> None:
        self.service.request_stop()
        self.board.close()
        self.temporary.cleanup()

    async def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("timed out waiting for deletion fixture")
            await asyncio.sleep(0.01)

    async def test_registered_project_drains_then_removes_runtime_and_blackboard(self) -> None:
        project = self.board.create_project(
            "registered-delete",
            "fixture.local",
            "delete a running service project",
            {"targets": ["fixture.local"], "bootstrap_enabled": False},
        )
        task = asyncio.create_task(self.service.serve())
        try:
            await self._wait_for(lambda: project.id in self.factory.created)
            self.board.request_project_deletion(project.id)
            await self._wait_for(
                lambda: project.id in self.factory.deleted
                and project.id not in {item.id for item in self.board.list_projects("deleting")}
                and project.id not in self.service.dispatchers
            )

            self.assertIn(project.id, self.factory.cleaned)
            with self.assertRaises(KeyError):
                self.board.get_project(project.id)
            self.assertFalse(any(error["kind"] == "project_dispatcher_crashed" for error in self.service.errors))
        finally:
            self.service.request_stop()
            await asyncio.wait_for(task, timeout=2)

    async def test_unregistered_deletion_never_creates_a_runtime(self) -> None:
        project = self.board.create_project(
            "unregistered-delete",
            "fixture.local",
            "delete before service registration",
            {"targets": ["fixture.local"]},
        )
        self.board.request_project_deletion(project.id)

        await self.service._finalize_unregistered_deletions()

        self.assertEqual(self.factory.created, [])
        self.assertEqual(self.factory.deleted, [project.id])
        with self.assertRaises(KeyError):
            self.board.get_project(project.id)

    async def test_registration_race_cleans_runtime_without_starting_dispatcher(self) -> None:
        self.factory = DeletingRuntimeFactory(Path(self.temporary.name) / "raced-workspaces", self.board)
        self.service.runtime_factory = self.factory
        project = self.board.create_project(
            "raced-delete",
            "fixture.local",
            "delete during runtime registration",
            {"targets": ["fixture.local"]},
        )

        dispatcher = await self.service.register_project(project.id)

        self.assertIsNone(dispatcher)
        self.assertEqual(self.factory.created, [project.id])
        self.assertEqual(self.factory.cleaned, [project.id])
        self.assertNotIn(project.id, self.service.dispatchers)
        await self.service._finalize_unregistered_deletions()
        with self.assertRaises(KeyError):
            self.board.get_project(project.id)


class ProjectDeletionRuntimeTests(unittest.TestCase):
    def test_worker_manager_removes_container_workspace_and_cached_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspaces"
            manager = WorkerManager(root)
            project_id = "project-delete-runtime"
            workspace = manager.project_workspace_root(project_id)
            workspace.mkdir(parents=True)
            (workspace / "artifact.txt").write_text("fixture", encoding="utf-8")
            calls: list[list[str]] = []

            def executor(argv: list[str]) -> CommandExecution:
                calls.append(argv)
                return CommandExecution(0, "", "")

            manager.remove_project(project_id, executor)

            self.assertFalse(workspace.exists())
            self.assertEqual(calls, [["docker", "rm", "--force", "slime-project-delete-runtime"]])
            self.assertNotIn(project_id, manager.backends)
            self.assertNotIn(project_id, manager.profiles)
            self.assertNotIn(project_id, manager.extra_hosts)


if __name__ == "__main__":
    unittest.main()
