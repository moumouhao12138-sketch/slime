from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from slime_cairn.blackboard import Blackboard
from slime_cairn.execution import CommandExecution
from slime_cairn.models import CompletionProposal, FactCandidate, IntentProposal, PseudopodReport, WorkerTask
from slime_cairn.native_agent import NativeAgentConfig, NativeAgentMind
from slime_cairn.scheduler import Scheduler
from slime_cairn.service import ProjectRuntimeBinding
from slime_cairn.workspace import IsolatedWorkspace


class FakeNativeBackend:
    def __init__(self, workspace_root: Path, outputs: list[CommandExecution]) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def execute_with_environment(self, argv, cwd, timeout, environment=None):
        cwd = Path(cwd)
        pod_evidence = list(cwd.glob("pods/**/evidence"))
        if not pod_evidence:
            raise AssertionError("native task did not create a pod evidence directory")
        for evidence_directory in pod_evidence:
            (evidence_directory / "scan.txt").write_text(
                "confirmed native output",
                encoding="utf-8",
            )
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "timeout": timeout,
                "environment": dict(environment or {}),
            }
        )
        if not self.outputs:
            raise AssertionError("fake output queue exhausted")
        return self.outputs.pop(0)


class ReportMind:
    def __init__(self, report: PseudopodReport) -> None:
        self.report = report
        self.calls: list[tuple] = []

    def run_cairn_task(self, intent, facts, mode, capsule):
        self.calls.append((intent, facts, mode, capsule))
        return self.report


class CairnRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.target = "fixture.local"
        self.project = self.board.create_project(
            "native-test",
            self.target,
            "return an evidence-backed result",
            {"targets": [self.target]},
        )

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def test_native_cli_task_runs_at_project_root_and_imports_fact(self):
        result = {
            "summary": "native task confirmed the fixture",
            "facts": [
                {
                    "subject": self.target,
                    "predicate": "native_reachable",
                    "object": "true",
                    "confidence": 0.96,
                    "evidence_files": ["evidence/scan.txt"],
                }
            ],
            "hypotheses": [],
            "proposed_intents": [],
            "completion": None,
        }
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "native-thread"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "inspect fixture.local",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps(result),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 20, "output_tokens": 10},
                    }
                ),
            ]
        )
        root = Path(self.temporary.name) / "workspace"
        backend = FakeNativeBackend(root, [CommandExecution(0, stdout, "")])
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig(
                "codex-explore",
                "codex-cli",
                "codex",
                environment={"OPENAI_API_KEY": "test-secret"},
            ),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)
        scheduler.seed(self.target)
        intent = self.board.claim_next_intent(self.project.id, "native-owner")
        self.assertIsNotNone(intent)
        outcome = scheduler.process_claimed_intent(
            intent,
            mind=mind,
            worker_name="codex-explore",
            owner_token="native-owner",
        )

        self.assertEqual(outcome["accepted_facts"][0].predicate, "native_reachable")
        self.assertEqual(backend.calls[0]["cwd"], root.resolve())
        self.assertEqual(backend.calls[0]["environment"]["OPENAI_API_KEY"], "test-secret")
        self.assertEqual(len(backend.calls), 1)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", backend.calls[0]["argv"])
        self.assertIn("features.plugins=false", backend.calls[0]["argv"])
        self.assertIn("features.remote_plugin=false", backend.calls[0]["argv"])
        self.assertIn("features.plugin_sharing=false", backend.calls[0]["argv"])
        self.assertEqual(
            backend.calls[0]["argv"][backend.calls[0]["argv"].index("-C") + 1],
            "/workspace",
        )
        self.assertTrue(list((root / "context").glob("bootstrap-*/graph.yaml")))
        run = self.board.list_worker_runs(self.project.id)[0]
        self.assertEqual(run["tool_calls"], 0)
        self.assertEqual(run["model_session"]["execution"], "native-agent")
        self.assertTrue(Path(run["model_session"]["manifest"]).is_file())

    def test_native_bootstrap_can_complete_from_its_new_fact(self):
        result = {
            "summary": "bootstrap reached the fixture goal",
            "facts": [
                {
                    "subject": self.target,
                    "predicate": "goal_proof",
                    "object": "confirmed",
                    "confidence": 1.0,
                    "evidence_files": ["evidence/scan.txt"],
                }
            ],
            "hypotheses": [],
            "proposed_intents": [],
            # The one-Fact form mirrors Cairn's Bootstrap contract. The native
            # parser resolves it to candidate index zero before Scheduler maps
            # that candidate to its durable Blackboard Fact ID.
            "completion": {"description": "the confirmed proof satisfies the fixture goal"},
        }
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "bootstrap-thread"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": json.dumps(result)},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}}),
            ]
        )
        root = Path(self.temporary.name) / "bootstrap-workspace"
        backend = FakeNativeBackend(root, [CommandExecution(0, stdout, "")])
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig("claude-bootstrap", "codex-cli", "codex"),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)
        scheduler.seed(self.target)
        intent = self.board.claim_next_intent(self.project.id, "bootstrap-owner")
        self.assertIsNotNone(intent)

        outcome = scheduler.process_claimed_intent(
            intent,
            mind=mind,
            worker_name="claude-bootstrap",
            owner_token="bootstrap-owner",
        )

        proof = outcome["accepted_facts"][0]
        self.assertEqual(self.board.get_project(self.project.id).status, "completed")
        self.assertEqual(outcome["completion"]["fact_ids"], [proof.id])
        self.assertEqual(self.board.get_active_completion(self.project.id)["fact_ids"], [proof.id])
        run = self.board.list_worker_runs(self.project.id)[0]
        self.assertEqual(run["validation"]["direct_completion"]["fact_ids"], [proof.id])

    def test_invalid_execute_uses_same_session_for_bootstrap_conclude(self):
        first_stdout = "\n".join(
            [
                json.dumps({"type": "session", "id": "resume-session"}),
                "the first execute pass did not return the report contract",
            ]
        )
        conclude_result = {
            "summary": "the existing session confirmed the fixture goal",
            "fact": {
                "subject": self.target,
                "predicate": "conclude_goal_proof",
                "object": "confirmed",
                "confidence": 1.0,
                "evidence_files": ["evidence/scan.txt"],
            },
            "completion": {"description": "the preserved evidence satisfies the fixture goal"},
        }
        conclude_stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(conclude_result)},
            }
        )
        root = Path(self.temporary.name) / "conclude-workspace"
        backend = FakeNativeBackend(
            root,
            [CommandExecution(0, first_stdout, ""), CommandExecution(0, conclude_stdout, "")],
        )
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig(
                "codex-bootstrap",
                "codex-cli",
                "codex",
                bootstrap_timeout=101,
                bootstrap_conclude_timeout=17,
            ),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)
        scheduler.seed(self.target)
        intent = self.board.claim_next_intent(self.project.id, "conclude-owner")
        self.assertIsNotNone(intent)

        outcome = scheduler.process_claimed_intent(
            intent,
            mind=mind,
            worker_name="codex-bootstrap",
            owner_token="conclude-owner",
        )

        report = outcome["report"]
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual([call["timeout"] for call in backend.calls], [101, 17])
        self.assertIn("resume", backend.calls[1]["argv"])
        self.assertIn("resume-session", backend.calls[1]["argv"])
        self.assertEqual(report.stop_reason, "native_agent_conclude_fallback")
        self.assertEqual(report.model_usage["model_calls"], 2)
        self.assertTrue(report.model_session["fallback"]["recovered"])
        self.assertEqual(self.board.get_project(self.project.id).status, "completed")
        self.assertEqual(outcome["completion"]["fact_ids"], [outcome["accepted_facts"][0].id])
        transcripts = report.model_session["transcript_refs"]
        self.assertEqual(len(transcripts), 2)
        phases = [json.loads((root / ref).read_text(encoding="utf-8"))["phase"] for ref in transcripts]
        self.assertEqual(phases, ["execute", "conclude"])

    def test_nonzero_command_failure_releases_without_conclude(self):
        first_stdout = json.dumps({"type": "session", "id": "failed-resume-session"})
        root = Path(self.temporary.name) / "conclude-failure-workspace"
        backend = FakeNativeBackend(
            root,
            [
                CommandExecution(1, first_stdout, "execute failed"),
                CommandExecution(2, "", "conclude failed"),
            ],
        )
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig("codex-bootstrap", "codex-cli", "codex"),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)
        scheduler.seed(self.target)
        intent = self.board.claim_next_intent(self.project.id, "conclude-failure-owner")
        self.assertIsNotNone(intent)

        outcome = scheduler.process_claimed_intent(
            intent,
            mind=mind,
            worker_name="codex-bootstrap",
            owner_token="conclude-failure-owner",
        )

        report = outcome["report"]
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(report.status, "failed")
        self.assertIn("ModelInvocationError", report.errors[0])
        self.assertFalse(report.model_session["fallback"].get("recovered", False))
        self.assertEqual(len(report.model_session["transcript_refs"]), 1)
        self.assertEqual(len(report.evidence_refs), 1)
        self.assertEqual(self.board.get_project(self.project.id).status, "running")

    def test_explore_timeout_conclude_writes_one_fact_without_completion(self):
        intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect the fixture route",
                target_entity=self.target,
                parent_fact_ids=[],
            ),
            nutrient=1.0,
        )
        claimed = self.board.claim_next_intent(self.project.id, "explore-conclude-owner")
        self.assertIsNotNone(claimed)
        conclude_result = {
            "summary": "the completed pre-timeout work confirmed the route",
            "fact": {
                "subject": self.target,
                "predicate": "route_confirmed_by_conclude",
                "object": "true",
                "confidence": 0.9,
                "evidence_files": ["evidence/scan.txt"],
            },
            "completion": None,
        }
        conclude_stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(conclude_result)},
            }
        )
        root = Path(self.temporary.name) / "explore-conclude-workspace"
        backend = FakeNativeBackend(
            root,
            [
                CommandExecution(
                    124,
                    json.dumps({"type": "session", "id": "explore-resume-session"}),
                    "",
                    timed_out=True,
                ),
                CommandExecution(0, conclude_stdout, ""),
            ],
        )
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig(
                "codex-explore",
                "codex-cli",
                "codex",
                explore_timeout=131,
                explore_conclude_timeout=19,
            ),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)

        outcome = scheduler.process_claimed_intent(
            claimed,
            mind=mind,
            worker_name="codex-explore",
            owner_token="explore-conclude-owner",
        )

        report = outcome["report"]
        self.assertEqual(intent.id, claimed.id)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual([call["timeout"] for call in backend.calls], [131, 19])
        self.assertIn("explore-resume-session", backend.calls[1]["argv"])
        self.assertEqual(report.stop_reason, "native_agent_conclude_fallback")
        self.assertEqual(len(outcome["accepted_facts"]), 1)
        self.assertIsNone(outcome["completion"])
        self.assertEqual(self.board.get_project(self.project.id).status, "running")

    def test_reason_uses_its_own_timeout_and_never_enters_conclude(self):
        root = Path(self.temporary.name) / "reason-timeout-workspace"
        backend = FakeNativeBackend(
            root,
            [CommandExecution(0, json.dumps({"type": "session", "id": "reason-session"}), "")],
        )
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig(
                "codex-reason",
                "codex-cli",
                "codex",
                reason_timeout=79,
            ),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)

        with self.assertRaisesRegex(RuntimeError, "reason worker failed"):
            scheduler.reason(mind=mind, worker_name="codex-reason")

        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0]["timeout"], 79)

    def test_reason_caps_new_intents_to_its_cairn_task_limit(self):
        result = {
            "summary": "three possible directions were ranked",
            "proposed_intents": [
                {"kind": "explore", "objective": f"inspect direction {index}"}
                for index in range(3)
            ],
            "completion": None,
        }
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(result)},
            }
        )
        root = Path(self.temporary.name) / "reason-intent-limit-workspace"
        backend = FakeNativeBackend(root, [CommandExecution(0, stdout, "")])
        workspace = IsolatedWorkspace(root)
        mind = NativeAgentMind(
            NativeAgentConfig("codex-reason", "codex-cli", "codex", reason_max_intents=2),
            lambda project_id: backend,
        )
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)

        created = scheduler.reason(mind=mind, worker_name="codex-reason")

        self.assertEqual(created, 2)
        self.assertEqual(len(scheduler.last_reason_report.proposed_intents), 2)
        fanout_events = [
            event
            for event in self.board.list_events(self.project.id)
            if event["kind"] == "reason.intent_batch_created"
        ]
        self.assertEqual(len(fanout_events), 1)
        self.assertTrue(fanout_events[0]["payload"]["parallel"])
        self.assertEqual(fanout_events[0]["payload"]["fanout_count"], 2)
        self.assertEqual(len(fanout_events[0]["payload"]["intent_ids"]), 2)

    def test_only_reason_can_emit_native_intents(self):
        intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect the fixture route",
                target_entity=self.target,
                parent_fact_ids=[],
            ),
            nutrient=1.0,
        )
        root = Path(self.temporary.name) / "intent-contract-workspace"
        backend = FakeNativeBackend(root, [])
        mind = NativeAgentMind(
            NativeAgentConfig("codex-explore", "codex-cli", "codex"),
            lambda project_id: backend,
        )
        task = WorkerTask(
            mode="explore",
            objective=intent.objective,
            intent_id=intent.id,
            target_entity=intent.target_entity,
            relevant_fact_ids=[],
        )
        mind.begin_session(task, intent)
        try:
            state = mind._state()
            context = mind._context(task, intent, [], state)
            self.assertNotIn('"proposed_intents": [', mind._prompt(context, state))
            (state.directory / "evidence" / "scan.txt").write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "只有 Reason 能创建 Intent"):
                mind._normalize_report(
                    {
                        "summary": "fixture result",
                        "facts": [],
                        "hypotheses": [],
                        "proposed_intents": [
                            {
                                "kind": "explore",
                                "objective": "silently discarded before this contract",
                            }
                        ],
                        "completion": None,
                    },
                    task,
                    state,
                )
        finally:
            mind.end_session()

    def test_reason_completes_only_with_existing_blackboard_fact(self):
        evidence_ref = "evidence://fixture/proof"
        self.board.register_evidence(self.project.id, evidence_ref, "fixture")
        proof, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(self.target, "goal_proof", "confirmed", 1.0, [evidence_ref]),
        )
        completion = CompletionProposal([proof.id], "the confirmed Fact satisfies the goal")
        report = PseudopodReport(
            pseudopod_id="ignored",
            intent_id="ignored",
            mode="reason",
            status="completed",
            candidate_facts=[],
            candidate_hypotheses=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=10.0,
            stop_reason="fixture",
            completion=completion,
        )
        mind = ReportMind(report)
        workspace = IsolatedWorkspace(Path(self.temporary.name) / "reason-workspace")
        scheduler = Scheduler(self.board, self.project.id, mind=mind, workspace=workspace)

        created = scheduler.reason(mind=mind, worker_name="pi-reason")

        self.assertEqual(created, 0)
        self.assertEqual(self.board.get_project(self.project.id).status, "completed")
        self.assertEqual(self.board.get_active_completion(self.project.id)["fact_ids"], [proof.id])
        self.assertEqual(mind.calls[0][2], "reason")

    def test_scheduler_requires_a_direct_task_runner(self):
        workspace = IsolatedWorkspace(Path(self.temporary.name) / "missing-runner")
        scheduler = Scheduler(self.board, self.project.id, mind=object(), workspace=workspace)
        scheduler.seed(self.target)
        intent = self.board.claim_next_intent(self.project.id, "owner")

        with self.assertRaisesRegex(RuntimeError, "run_cairn_task"):
            scheduler.process_claimed_intent(intent, mind=object(), owner_token="owner")

    def test_runtime_binding_contains_only_workspace_and_cleanup(self):
        workspace = IsolatedWorkspace(Path(self.temporary.name) / "binding")
        binding = ProjectRuntimeBinding(workspace=workspace)
        self.assertEqual(binding.workspace.root, workspace.root)
        self.assertEqual(ProjectRuntimeBinding.__slots__, ("workspace", "cleanup"))


if __name__ == "__main__":
    unittest.main()
