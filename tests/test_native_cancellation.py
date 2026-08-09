from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.domain.context import ContextCapsule, ContextManifest
from slime_cairn.workers.errors import ModelInvocationCancelled
from slime_cairn.workers.execution import CommandExecution, DockerExecProcess, PersistentDockerBackend, PersistentDockerConfig
from slime_cairn.domain.models import Intent, WorkerTask
from slime_cairn.workers.native import NativeAgentConfig, NativeAgentMind
from slime_cairn.workers.factory import DockerCairnRuntimeFactory


class FakePopen:
    def __init__(self, *, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.terminated = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.terminated.set()

    def communicate(self, timeout=None):
        if self.returncode is None:
            self.terminated.wait(timeout)
        return "stdout", "stderr"


class BlockingCommand:
    def __init__(self) -> None:
        self.cancelled = False
        self.reasons: list[str] = []

    def cancel(self, reason: str = "cancelled") -> bool:
        if self.cancelled:
            return False
        self.cancelled = True
        self.reasons.append(reason)
        return True

    def communicate(self, timeout=None) -> CommandExecution:
        return CommandExecution(130, "", "cancelled", cancelled=True, cancel_reason="stopped")


class StaticCommand:
    def __init__(self, result: CommandExecution) -> None:
        self.result = result
        self.cancel_reasons: list[str] = []

    def cancel(self, reason: str = "cancelled") -> bool:
        self.cancel_reasons.append(reason)
        return len(self.cancel_reasons) == 1

    def communicate(self, timeout=None) -> CommandExecution:
        return self.result


class FakeBackend:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root
        root.mkdir(parents=True, exist_ok=True)

    def execute_with_environment(self, argv, cwd, timeout, environment=None):
        raise AssertionError("test should use the interruptible command path")


class HealthBackend(FakeBackend):
    def __init__(self, root: Path, results: list[CommandExecution]) -> None:
        super().__init__(root)
        self.results = list(results)
        self.calls: list[dict] = []

    def start_with_environment(self, argv, cwd, environment=None):
        self.calls.append(
            {"argv": list(argv), "cwd": Path(cwd), "environment": dict(environment or {})}
        )
        if not self.results:
            raise AssertionError("health backend output queue exhausted")
        return StaticCommand(self.results.pop(0))


class RecoveringHealthBackend(HealthBackend):
    def __init__(self, root: Path, results: list[CommandExecution]) -> None:
        super().__init__(root, results)
        self.recoveries: list[str] = []

    def recover_resource_exhaustion(self, detail: str = "") -> dict[str, object]:
        self.recoveries.append(detail)
        return {"recovered": True, "action": "container_restarted"}


def fixture_capsule() -> ContextCapsule:
    return ContextCapsule(
        mode="explore",
        objective="fixture",
        goal="finish fixture",
        scope={"targets": ["fixture.local"]},
        environment_brief="fixture",
        facts=[],
        recent_fact_ids=[],
        previous_summary="",
        branches=[],
        evidence_refs=[],
        manifest=ContextManifest(
            reason_kind="incremental",
            budget=100,
            estimated_tokens=0,
            pinned_fact_ids=[],
            included_fact_ids=[],
            omitted_fact_ids=[],
            included_branch_entities=[],
            omitted_branch_entities=[],
            branch_reserved_fact_ids=[],
            revived_fact_ids=[],
            audit_fact_ids=[],
            reasons={},
        ),
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandExecution:
        self.calls.append(list(argv))
        if "inspect" in argv:
            return CommandExecution(0, "true\n", "")
        return CommandExecution(0, "", "")


class NativeCancellationTests(unittest.TestCase):
    def test_docker_exec_cancel_signals_workspace_pid_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "task.pid"
            pid_path.write_text("4242\n", encoding="ascii")
            fake = FakePopen(returncode=None)
            calls: list[list[str]] = []

            def control_run(argv, **kwargs):
                calls.append(list(argv))
                fake.returncode = -15
                fake.terminated.set()
                return subprocess.CompletedProcess(argv, 0, "", "")

            handle = DockerExecProcess(
                docker_binary="docker",
                container_name="slime-project",
                process=fake,
                env_path=None,
                pid_path=pid_path,
            )
            with patch("slime_cairn.workers.execution.subprocess.run", side_effect=control_run):
                self.assertTrue(handle.cancel("project_stopped"))
                deadline = time.monotonic() + 2
                while not calls and time.monotonic() < deadline:
                    time.sleep(0.01)
                result = handle.communicate(timeout=1)

            self.assertTrue(calls)
            self.assertEqual(calls[0][:4], ["docker", "exec", "slime-project", "/bin/sh"])
            self.assertIn("4242", calls[0])
            control_script = "\n".join(calls[0])
            self.assertIn("TERM", control_script)
            self.assertIn("KILL", control_script)
            self.assertIn("/proc/$current/task/$current/children", control_script)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.cancel_reason, "project_stopped")
            self.assertFalse(pid_path.exists())

    def test_backend_wraps_native_cli_and_cleans_temporary_env_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = PersistentDockerBackend(
                root,
                PersistentDockerConfig(container_name="slime-fixture"),
            )
            fake = FakePopen(returncode=0)
            seen: dict[str, list[str]] = {}

            def fake_popen(argv, **kwargs):
                seen["argv"] = list(argv)
                return fake

            with patch("slime_cairn.workers.execution.subprocess.Popen", side_effect=fake_popen):
                handle = backend.start_with_environment(
                    ["codex", "exec", "task"],
                    root,
                    {"CODEX_HOME": "/workspace/shared/agent-homes/codex"},
                )
                env_file = Path(seen["argv"][seen["argv"].index("--env-file") + 1])
                self.assertTrue(env_file.is_file())
                wrapper = seen["argv"][seen["argv"].index("-lc") + 1]
                self.assertIn("setsid --wait", wrapper)
                result = handle.communicate(timeout=1)

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(env_file.exists())

    def test_jsonl_without_assistant_result_preserves_protocol_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig("claude-bootstrap", "claude-code", "claude"),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="bootstrap",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_jsonl",
                project_id="project_jsonl",
            )
            task = WorkerTask("bootstrap", "fixture", intent.id, "fixture.local", [])
            mind.begin_session(task, intent)
            try:
                output = "\n".join(
                    [
                        json.dumps({"type": "system", "subtype": "init"}),
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "api_retry",
                                "error": "unknown",
                            }
                        ),
                    ]
                )
                with self.assertRaisesRegex(RuntimeError, r"api_retry: unknown"):
                    mind._extract_response(output)
            finally:
                mind.end_session("failed")

    def test_native_mind_cancels_matching_active_session_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeBackend(root)
            mind = NativeAgentMind(
                NativeAgentConfig("codex-explore", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="explore",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_fixture",
                project_id="project_fixture",
            )
            task = WorkerTask(
                mode="explore",
                objective="fixture",
                intent_id=intent.id,
                target_entity="fixture.local",
                relevant_fact_ids=[],
            )
            mind.begin_session(task, intent)
            state = mind._state()
            command = BlockingCommand()
            mind._register_active_command(state, command)
            try:
                first = mind.cancel_active(
                    project_id="project_fixture",
                    intent_id="intent_fixture",
                    reason="stopped",
                )
                second = mind.cancel_active(project_id="project_fixture", intent_id="intent_fixture")
            finally:
                mind._unregister_active_command(state)
                mind.end_session("interrupted")

            self.assertEqual(first["cancelled"], 1)
            self.assertEqual(second["cancelled"], 0)
            self.assertEqual(command.reasons, ["stopped"])

    def test_pending_session_cancel_prevents_cli_launch_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig("codex-explore", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="explore",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_pending",
                project_id="project_pending",
            )
            task = WorkerTask(
                mode="explore",
                objective="fixture",
                intent_id=intent.id,
                target_entity="fixture.local",
                relevant_fact_ids=[],
            )
            mind.begin_session(task, intent)
            state = mind._state()
            try:
                result = mind.cancel_active(
                    project_id="project_pending",
                    intent_id="intent_pending",
                    reason="stopped",
                )
                with self.assertRaises(ModelInvocationCancelled):
                    mind._execute_native_command(state, ["codex", "exec", "fixture"])
            finally:
                mind.end_session("interrupted")

            self.assertEqual(result["cancelled"], 1)

    def test_project_bound_healthcheck_is_cached_after_binary_and_config_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = HealthBackend(
                Path(temporary),
                [CommandExecution(0, "binary=codex\nversion=codex 1.0\nconfig_directory=readable\n", "")],
            )
            mind = NativeAgentMind(
                NativeAgentConfig(
                    "codex-explore",
                    "codex-cli",
                    "codex",
                    environment={"CODEX_HOME": "/workspace/shared/agent-homes/codex"},
                ),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="explore",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_health",
                project_id="project_health",
            )
            task = WorkerTask("explore", "fixture", intent.id, "fixture.local", [])
            mind.begin_session(task, intent)
            state = mind._state()
            try:
                mind._ensure_task_health(state)
                first = dict(state.health)
                mind._ensure_task_health(state)
                second = dict(state.health)
            finally:
                mind.end_session("completed")

            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(backend.calls[0]["argv"][:3], ["/bin/sh", "-lc", backend.calls[0]["argv"][2]])
            self.assertIn("codex", backend.calls[0]["argv"])
            self.assertTrue(first["healthy"])
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])

    def test_preflight_failure_returns_diagnostic_report_before_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = HealthBackend(
                Path(temporary),
                [
                    CommandExecution(
                        43,
                        "health_error=config_directory_missing:/workspace/shared/agent-homes/codex\n",
                        "",
                    )
                ],
            )
            mind = NativeAgentMind(
                NativeAgentConfig(
                    "codex-explore",
                    "codex-cli",
                    "codex",
                    environment={"CODEX_HOME": "/workspace/shared/agent-homes/codex"},
                ),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="explore",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_preflight_failure",
                project_id="project_preflight_failure",
            )

            report = mind.run_cairn_task(intent, [], "explore", fixture_capsule())

            self.assertEqual(report.status, "failed")
            self.assertEqual(len(backend.calls), 1)
            self.assertIn("preflight failed", report.errors[0])
            self.assertFalse(report.model_session["health"]["healthy"])
            self.assertIn("config_directory_missing", report.model_session["health"]["detail"])

    def test_preflight_resource_exhaustion_recovers_container_and_reprobes(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecoveringHealthBackend(
                Path(temporary),
                [
                    CommandExecution(2, "", "--: 4: Cannot fork"),
                    CommandExecution(0, "binary=codex\nversion=codex-cli 1.0\n", ""),
                ],
            )
            mind = NativeAgentMind(
                NativeAgentConfig("codex-recover", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="explore",
                objective="fixture",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_resource_recovery",
                project_id="project_resource_recovery",
            )
            task = WorkerTask("explore", "fixture", intent.id, "fixture.local", [])

            mind.begin_session(task, intent)
            try:
                state = mind._state()
                mind._ensure_task_health(state)
                health = dict(state.health)
            finally:
                mind.end_session("completed")

            self.assertEqual(len(backend.calls), 2)
            self.assertEqual(backend.recoveries, ["--: 4: Cannot fork"])
            self.assertTrue(health["healthy"])
            self.assertFalse(health["cached"])
            self.assertEqual(
                health["runtime_recovery"]["action"],
                "container_restarted",
            )

    def test_terminal_project_cleanup_stops_its_container_and_keeps_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = Blackboard(root / "board.db")
            executor = RecordingExecutor()
            try:
                project = board.create_project(
                    "fixture",
                    "127.0.0.1",
                    "finish fixture",
                    {"targets": ["127.0.0.1"]},
                )
                factory = DockerCairnRuntimeFactory(
                    board,
                    root / "workspaces",
                    executor=executor,
                )
                binding = factory(project)
                board.set_project_status(project.id, "stopped")
                assert binding.cleanup is not None
                binding.cleanup()

                backend = factory.manager.backend_for(project.id, factory.profile)
                self.assertTrue(backend.workspace_root.is_dir())
                self.assertIn(backend.stop_command(), executor.calls)
            finally:
                board.close()


if __name__ == "__main__":
    unittest.main()
