from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slime_cairn.context import ContextCapsule, ContextManifest
from slime_cairn.execution import CommandExecution
from slime_cairn.model_health import ModelEndpoint
from slime_cairn.models import Intent, WorkerTask
from slime_cairn.native_agent import NativeAgentConfig, NativeAgentMind


class RecordingBackend:
    def __init__(self, root: Path, results: list[CommandExecution] | None = None) -> None:
        self.workspace_root = root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.results = list(results or [])
        self.calls: list[list[str]] = []

    def execute_with_environment(self, argv, cwd, timeout, environment=None):
        self.calls.append(list(argv))
        if not self.results:
            raise AssertionError("unexpected native CLI call")
        return self.results.pop(0)


def capsule() -> ContextCapsule:
    return ContextCapsule(
        mode="bootstrap",
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


def task_and_intent(worker_name: str) -> tuple[WorkerTask, Intent]:
    intent = Intent(
        kind="bootstrap",
        objective="fixture",
        target_entity="fixture.local",
        parent_fact_ids=[],
        id="intent_session_fixture",
        project_id="project_session_fixture",
    )
    return WorkerTask("bootstrap", "fixture", intent.id, intent.target_entity, []), intent


class NativeSessionRecoveryTests(unittest.TestCase):
    def test_prompt_write_falls_back_when_existing_prompt_is_locked(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig("codex-native", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("codex-native")
            mind.begin_session(task, intent)
            try:
                state = mind._state()
                mind._write_prompt_file(state, "old prompt")
                original_replace = Path.replace
                blocked = False

                def replace(source, target):
                    nonlocal blocked
                    if Path(target).name == "prompt.txt" and not blocked:
                        blocked = True
                        raise PermissionError(5, "access denied", str(target))
                    return original_replace(source, target)

                with patch.object(Path, "replace", replace):
                    container_path = mind._write_prompt_file(state, "new prompt")
            finally:
                mind.end_session("completed")

            self.assertTrue(blocked)
            self.assertRegex(container_path, r"/prompt-[0-9a-f]+\.txt$")
            fallback = state.directory / Path(container_path).name
            self.assertEqual(fallback.read_text(encoding="utf-8"), "new prompt")

    def test_failed_codex_command_releases_without_conclude(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "019f90e7-928b-71b3-95ed-e14568e459e1"
            execute_stdout = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": thread_id}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": "rm -f /workspace/tmp/fixture",
                                "status": "failed",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {"message": "tool call rejected"},
                        }
                    ),
                )
            )
            conclude_payload = {
                "accepted": True,
                "data": {"fact": {"description": "preserved confirmed evidence"}},
            }
            conclude_stdout = "\n".join(
                (
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps(conclude_payload),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 5, "output_tokens": 3},
                        }
                    ),
                )
            )
            backend = RecordingBackend(
                root,
                [
                    CommandExecution(
                        1,
                        execute_stdout,
                        "This content was flagged for possible cybersecurity risk",
                    ),
                    CommandExecution(0, conclude_stdout, ""),
                ],
            )
            mind = NativeAgentMind(
                NativeAgentConfig("codex-native", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("codex-native")

            report = mind.run_cairn_task(intent, [], "bootstrap", capsule())

            self.assertEqual(report.status, "failed")
            self.assertEqual(report.stop_reason, "native_agent_error")
            self.assertEqual(report.candidate_facts, [])
            self.assertEqual(len(backend.calls), 1)
            manifest_path = next(root.glob("pods/codex-native/*/session.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["session_id"], thread_id)
            self.assertFalse(manifest["resume_allowed"])
            self.assertFalse(manifest["fallback"].get("recovered", False))

    def test_failed_codex_execute_with_missing_rollout_skips_conclude(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "019f90e7-928b-71b3-95ed-e14568e459e2"
            backend = RecordingBackend(
                root,
                [
                    CommandExecution(
                        1,
                        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
                        f"thread/resume failed: no rollout found for thread id {thread_id}",
                    )
                ],
            )
            mind = NativeAgentMind(
                NativeAgentConfig("codex-native", "codex-cli", "codex"),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("codex-native")

            report = mind.run_cairn_task(intent, [], "bootstrap", capsule())

            self.assertEqual(report.status, "failed")
            self.assertEqual(len(backend.calls), 1)
            manifest_path = next(root.glob("pods/codex-native/*/session.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["session_id"], thread_id)
            self.assertFalse(manifest["resume_allowed"])

            mind.begin_session(task, intent)
            try:
                state = mind._state()
                self.assertFalse(state.resumed)
                self.assertFalse(state.resume_session)
                argv = mind._build_argv(state, "fresh task")
                self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
                self.assertIn("codex", argv)
                self.assertIn("exec", argv)
                self.assertNotIn("resume", argv)
                self.assertNotIn("fresh task", argv)
            finally:
                mind.end_session("failed")

    def test_explicit_missing_session_errors_start_fresh_for_all_native_clis(self):
        fixtures = (
            ("codex-cli", "codex", "thread/resume failed: no rollout found for thread id stale"),
            ("claude-code", "claude", "failed to resume: session not found"),
            ("pi-cli", "pi", "No session found matching 'stale'"),
        )
        for adapter, binary, error in fixtures:
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                backend = RecordingBackend(Path(temporary))
                worker_name = f"{adapter}-fixture"
                mind = NativeAgentMind(
                    NativeAgentConfig(worker_name, adapter, binary),
                    lambda project_id: backend,
                )
                task, intent = task_and_intent(worker_name)
                mind.begin_session(task, intent)
                stale = mind._state()
                stale.session_id = "stale-session-id"
                stale.turns = 4
                stale.resume_session = True
                stale.last_error = error
                mind.end_session("failed")

                mind.begin_session(task, intent)
                try:
                    state = mind._state()
                    self.assertFalse(state.resumed)
                    self.assertFalse(state.resume_session)
                    self.assertEqual(state.turns, 4)
                    self.assertNotEqual(state.session_id, "stale-session-id")
                    argv = mind._build_argv(state, "fresh task")
                    self.assertNotIn("--resume", argv)
                    self.assertNotIn("resume", argv)
                    if adapter == "claude-code":
                        self.assertIn("--session-id", argv)
                    elif adapter == "pi-cli":
                        self.assertNotIn("--session", argv)
                        self.assertNotIn("--session-id", argv)
                finally:
                    mind.end_session("failed")

    def test_pi_uses_a_generated_session_then_resumes_the_reported_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig("pi-native", "pi-cli", "pi"),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("pi-native")
            mind.begin_session(task, intent)
            try:
                state = mind._state()
                initial_argv = mind._build_argv(state, "initial task")
                self.assertNotIn("--session", initial_argv)
                self.assertNotIn("--session-id", initial_argv)
                self.assertNotIn("--no-context-files", initial_argv)

                session_id = "019f9115-dc4c-77a4-a0ab-826f4b324e72"
                mind._extract_attempt_metadata(
                    json.dumps({"type": "session", "id": session_id}) + "\n"
                )
                resumed_argv = mind._build_argv(state, "conclude task")
                self.assertIn("--session", resumed_argv)
                self.assertIn(session_id, resumed_argv)
            finally:
                mind.end_session("completed")

    def test_claude_reads_large_prompt_from_workspace_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig(
                    "claude-native",
                    "claude-code",
                    "claude",
                    model="claude-fixture",
                ),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("claude-native")
            mind.begin_session(task, intent)
            try:
                prompt = "reason task:" + ("x" * 40_000)
                argv = mind._build_argv(mind._state(), prompt)
            finally:
                mind.end_session("completed")

            self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
            self.assertIn('exec "$@" < "$prompt_file"', argv[2])
            self.assertIn("claude", argv)
            self.assertIn("--session-id", argv)
            self.assertNotIn(prompt, argv)
            prompt_path = next(backend.workspace_root.glob("pods/**/prompt.txt"))
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), prompt)
            self.assertLess(max(len(item) for item in argv), 4_096)

    def test_claude_reason_is_a_tool_free_cairn_planning_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig(
                    "claude-native",
                    "claude-code",
                    "claude",
                    model="claude-fixture",
                ),
                lambda project_id: backend,
            )
            intent = Intent(
                kind="reason",
                objective="plan the next graph branches",
                target_entity="fixture.local",
                parent_fact_ids=[],
                id="intent_reason_fixture",
                project_id="project_reason_fixture",
            )
            task = WorkerTask(
                "reason",
                "plan the next graph branches",
                intent.id,
                intent.target_entity,
                [],
            )
            mind.begin_session(task, intent)
            try:
                state = mind._state()
                argv = mind._build_argv(state, "reason task")
                prompt = mind._prompt(
                    {
                        "mode": "reason",
                        "context_manifest": {},
                        "scope": {},
                        "environment": {"resource_namespace": "fixture"},
                    },
                    state,
                )
            finally:
                mind.end_session("completed")

            self.assertNotIn("--tools", argv)
            self.assertIn("YAML snapshot of the task graph", prompt)
            self.assertIn("Valid facts", prompt)
            self.assertIn("Open Intents", prompt)

    def test_explicit_pi_provider_writes_models_json_inside_container_without_key_in_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingBackend(Path(temporary))
            mind = NativeAgentMind(
                NativeAgentConfig(
                    "pi-native",
                    "pi-cli",
                    "pi",
                    model="pi-fixture",
                    provider="slime_cairn",
                    environment={
                        "SLIME_PI_MODEL": "pi-fixture",
                        "SLIME_PI_BASE_URL": "https://models.example/v1",
                        "SLIME_PI_PROVIDER_API": "openai-completions",
                        "PI_API_KEY": "pi-top-secret",
                    },
                    model_endpoint=ModelEndpoint(
                        "https://models.example/v1",
                        "pi-top-secret",
                        "openai-chat-completions",
                    ),
                ),
                lambda project_id: backend,
            )
            task, intent = task_and_intent("pi-native")
            mind.begin_session(task, intent)
            try:
                prompt = "initial task:" + ("x" * 40_000)
                argv = mind._build_argv(mind._state(), prompt)
            finally:
                mind.end_session("completed")

            self.assertEqual(argv[:3], ["/bin/sh", "-lc", argv[2]])
            self.assertIn("models.json", argv[2])
            self.assertIn("PI_API_KEY", argv[2])
            self.assertIn("--provider", argv)
            self.assertIn("slime_cairn", argv)
            self.assertNotIn("pi-top-secret", " ".join(argv))
            self.assertNotIn(prompt, argv)
            prompt_args = [item for item in argv if item.startswith("@") and item.endswith("/prompt.txt")]
            self.assertEqual(len(prompt_args), 1)
            prompt_path = next(backend.workspace_root.glob("pods/**/prompt.txt"))
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), prompt)
            self.assertLess(max(len(item) for item in argv), 4_096)


if __name__ == "__main__":
    unittest.main()
