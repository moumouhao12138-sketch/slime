from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from slime_cairn.blackboard import Blackboard
from slime_cairn.dispatcher import DispatcherConfig, WorkerPool, WorkerRuntime
from slime_cairn.service import DispatcherService, ProjectRuntimeBinding


class HealthMind:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    def healthcheck(self):
        return {
            "healthy": self.healthy,
            "status": 401 if not self.healthy else 200,
            "detail": "fixture health",
        }


class StartupHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_exits_before_project_runtime_when_required_workers_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            board = Blackboard(Path(temporary) / "board.db")
            created: list[str] = []
            try:
                pool = WorkerPool(
                    [
                        WorkerRuntime(
                            "bad-worker",
                            HealthMind(False),
                            ("bootstrap", "explore", "reason"),
                        )
                    ]
                )
                service = DispatcherService(
                    board,
                    pool,
                    lambda project: created.append(project.id) or ProjectRuntimeBinding(),
                    DispatcherConfig(worker_healthcheck="startup_only"),
                )

                with self.assertRaisesRegex(RuntimeError, "required task types unavailable"):
                    await service.serve()

                self.assertEqual(created, [])
                self.assertEqual(service.status()["state"], "failed")
                self.assertFalse(pool.get("bad-worker").healthy)
            finally:
                board.close()

    async def test_service_requires_both_explore_and_reason_after_healthcheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            board = Blackboard(Path(temporary) / "board.db")
            try:
                pool = WorkerPool([WorkerRuntime("explore-only", HealthMind(True), ("explore",))])
                service = DispatcherService(
                    board,
                    pool,
                    lambda project: ProjectRuntimeBinding(),
                    DispatcherConfig(worker_healthcheck="startup_only"),
                )

                with self.assertRaisesRegex(RuntimeError, "reason"):
                    await service.serve()
            finally:
                board.close()


if __name__ == "__main__":
    unittest.main()
