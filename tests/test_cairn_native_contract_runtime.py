from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from slime_cairn.domain.context import ContextCapsule, ContextManifest
from slime_cairn.workers.execution import CommandExecution
from slime_cairn.domain.models import Fact, Intent, IntentProposal
from slime_cairn.workers.native import NativeAgentConfig, NativeAgentMind
from slime_cairn.protocol.prompting import load_prompt, validate_prompt_group


class ContractBackend:
    """Minimal native backend that exposes each model turn to the assertions."""

    def __init__(self, workspace_root: Path, outputs: list[CommandExecution]) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    def execute_with_environment(self, argv, cwd, timeout, environment=None):
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": Path(cwd),
                "timeout": timeout,
                "environment": dict(environment or {}),
            }
        )
        if not self.outputs:
            raise AssertionError("fake native output queue exhausted")
        return self.outputs.pop(0)


def cli_turn(payload: dict, *, session_id: str = "") -> CommandExecution:
    events: list[dict] = []
    if session_id:
        # The generic session event represents a session that can be resumed
        # for Cairn's bounded conclude phase.
        events.append({"type": "session", "id": session_id})
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(payload),
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 13, "output_tokens": 7},
            },
        ]
    )
    return CommandExecution(0, "\n".join(json.dumps(event) for event in events), "")


def manifest() -> ContextManifest:
    return ContextManifest(
        reason_kind="incremental",
        budget=10_000,
        estimated_tokens=100,
        pinned_fact_ids=[],
        included_fact_ids=[],
        omitted_fact_ids=[],
        included_branch_entities=[],
        omitted_branch_entities=[],
        branch_reserved_fact_ids=[],
        revived_fact_ids=[],
        audit_fact_ids=[],
        reasons={},
    )


class CairnNativeContractRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = "fixture.local"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def intent(self, mode: str, *, intent_id: str | None = None) -> Intent:
        return Intent(
            kind=mode,
            objective=f"run the {mode} Cairn task",
            target_entity=self.target,
            parent_fact_ids=[],
            id=intent_id or f"intent-{mode}",
            project_id="project-contract",
        )

    def capsule(
        self,
        mode: str,
        *,
        facts: list[Fact] | None = None,
        open_intents: list[dict] | None = None,
        scope: dict | None = None,
    ) -> ContextCapsule:
        selected_facts = list(facts or [])
        capsule_manifest = manifest()
        capsule_manifest.included_fact_ids = [fact.id for fact in selected_facts]
        return ContextCapsule(
            mode=mode,
            objective=f"run the {mode} Cairn task",
            goal="return an evidence-backed result",
            scope={"targets": [self.target], **dict(scope or {})},
            environment_brief="local fixture",
            facts=selected_facts,
            recent_fact_ids=[fact.id for fact in selected_facts],
            previous_summary="",
            branches=[],
            evidence_refs=[],
            manifest=capsule_manifest,
            open_intents=list(open_intents or []),
        )

    def fact(self, fact_id: str, description: str) -> Fact:
        return Fact(
            subject=self.target,
            predicate="cairn_fact",
            object=description,
            confidence=0.9,
            evidence_refs=[],
            id=fact_id,
            project_id="project-contract",
        )

    @staticmethod
    def prompt_text(backend: ContractBackend) -> str:
        paths = list(backend.workspace_root.glob("pods/**/prompt.txt"))
        if len(paths) != 1:
            raise AssertionError(f"expected one persisted prompt, found {len(paths)}")
        return paths[0].read_text(encoding="utf-8")

    def run_task(
        self,
        mode: str,
        outputs: list[CommandExecution],
        *,
        facts: list[Fact] | None = None,
        open_intents: list[dict] | None = None,
        reason_max_intents: int = 2,
        intent_id: str | None = None,
        scope: dict | None = None,
    ):
        backend = ContractBackend(self.root / f"workspace-{mode}-{intent_id or 'default'}", outputs)
        mind = NativeAgentMind(
            NativeAgentConfig(
                f"codex-{mode}",
                "codex-cli",
                "codex",
                reason_max_intents=reason_max_intents,
            ),
            lambda project_id: backend,
        )
        task_intent = self.intent(mode, intent_id=intent_id)
        selected_facts = list(facts or [])
        report = mind.run_cairn_task(
            task_intent,
            selected_facts,
            mode,
            self.capsule(
                mode,
                facts=selected_facts,
                open_intents=open_intents,
                scope=scope,
            ),
        )
        return report, backend

    def test_bootstrap_fact_and_complete_normalize_to_new_fact_completion(self) -> None:
        payload = {
            "accepted": True,
            "data": {
                "fact": {"description": "verified the target entry point"},
                "complete": {"description": "the entry point proves the fixture goal"},
            },
        }

        report, backend = self.run_task("bootstrap", [cli_turn(payload)])

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(report.candidate_facts), 1)
        self.assertEqual(report.candidate_facts[0].subject, self.target)
        self.assertEqual(report.candidate_facts[0].predicate, "cairn_fact")
        self.assertEqual(report.candidate_facts[0].object, "verified the target entry point")
        self.assertIsNotNone(report.completion)
        self.assertEqual(report.completion.fact_ids, [])
        self.assertEqual(report.completion.candidate_fact_indexes, [0])
        self.assertEqual(
            report.completion.description,
            "the entry point proves the fixture goal",
        )

    def test_partial_bootstrap_uses_same_session_conclude_and_keeps_only_its_fact(self) -> None:
        partial = {
            "accepted": True,
            "data": {"fact": {"description": "execute found partial evidence"}},
        }
        concluded = {
            "accepted": True,
            "data": {"fact": {"description": "bounded conclusion from confirmed evidence"}},
        }

        report, backend = self.run_task(
            "bootstrap",
            [cli_turn(partial, session_id="bootstrap-session"), cli_turn(concluded)],
            intent_id="intent-bootstrap-conclude",
        )

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(backend.calls), 2)
        self.assertIn("resume", backend.calls[1]["argv"])
        self.assertIn("bootstrap-session", backend.calls[1]["argv"])
        self.assertEqual(report.stop_reason, "native_agent_conclude_fallback")
        self.assertEqual(len(report.candidate_facts), 1)
        self.assertEqual(
            report.candidate_facts[0].object,
            "bounded conclusion from confirmed evidence",
        )
        self.assertIsNone(report.completion)

    def test_explore_description_normalizes_to_exactly_one_fact(self) -> None:
        payload = {
            "accepted": True,
            "data": {"description": "confirmed a distinct route on the target"},
        }

        report, backend = self.run_task("explore", [cli_turn(payload)])

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(report.candidate_facts), 1)
        fact = report.candidate_facts[0]
        self.assertEqual((fact.subject, fact.predicate, fact.object), (
            self.target,
            "cairn_fact",
            "confirmed a distinct route on the target",
        ))
        self.assertAlmostEqual(fact.confidence, 0.9)
        self.assertIsNone(report.completion)
        self.assertEqual(report.proposed_intents, [])

    def test_managed_benchmark_explore_preserves_explicit_candidates(self) -> None:
        payload = {
            "accepted": True,
            "data": {
                "description": "confirmed the exact benchmark value",
                "submissions": ["flag{one}"],
            },
        }

        report, backend = self.run_task(
            "explore",
            [cli_turn(payload)],
            scope={"benchmark": {"managed": True}},
            intent_id="intent-explore-candidate",
        )

        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(
            report.candidate_facts[0].attributes["benchmark_submissions"],
            ["flag{one}"],
        )

    def test_managed_benchmark_legacy_reason_completion_extracts_flag_from_description(self) -> None:
        source = self.fact("fact-reason-source", "confirmed source evidence")
        payload = {
            "summary": "the benchmark goal is complete",
            "facts": [],
            "hypotheses": [],
            "proposed_intents": [],
            "completion": {
                "description": "The confirmed response contains flag{one}.",
                "fact_ids": [source.id],
            },
        }

        report, _ = self.run_task(
            "reason",
            [cli_turn(payload)],
            facts=[source],
            scope={"benchmark": {"managed": True}},
            intent_id="intent-reason-auto-flag",
        )

        self.assertIsNotNone(report.completion)
        self.assertEqual(report.completion.submissions, ["flag{one}"])

    def test_managed_benchmark_reason_completion_extracts_flag_without_submissions(self) -> None:
        source = self.fact("fact-cairn-reason-source", "confirmed source evidence")
        payload = {
            "accepted": True,
            "data": {
                "complete": {
                    "from": [source.id],
                    "description": "The confirmed response contains flag{two}.",
                }
            },
        }

        report, _ = self.run_task(
            "reason",
            [cli_turn(payload)],
            facts=[source],
            scope={"benchmark": {"managed": True}},
            intent_id="intent-cairn-reason-auto-flag",
        )

        self.assertIsNotNone(report.completion)
        self.assertEqual(report.completion.submissions, ["flag{two}"])

    def test_explore_placeholder_uses_same_session_conclude_before_writing_fact(self) -> None:
        placeholder = {
            "accepted": True,
            "data": {"description": "latest confirmed incremental facts"},
        }
        concluded = {
            "accepted": True,
            "data": {"description": "concrete evidence preserved by conclude"},
        }

        report, backend = self.run_task(
            "explore",
            [cli_turn(placeholder, session_id="explore-placeholder-session"), cli_turn(concluded)],
            intent_id="intent-explore-placeholder",
        )

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.stop_reason, "native_agent_conclude_fallback")
        self.assertEqual(len(backend.calls), 2)
        self.assertIn("resume", backend.calls[1]["argv"])
        self.assertIn("explore-placeholder-session", backend.calls[1]["argv"])
        self.assertEqual(len(report.candidate_facts), 1)
        self.assertEqual(
            report.candidate_facts[0].object,
            "concrete evidence preserved by conclude",
        )

    def test_reason_intents_map_from_to_parent_fact_ids(self) -> None:
        source_a = self.fact("fact-a", "first source")
        source_b = self.fact("fact-b", "second source")
        payload = {
            "accepted": True,
            "data": {
                "intents": [
                    {
                        "from": [source_a.id, source_b.id],
                        "description": "combine both confirmed branches",
                    }
                ]
            },
        }

        report, _ = self.run_task("reason", [cli_turn(payload)], facts=[source_a, source_b])

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(report.proposed_intents), 1)
        proposal = report.proposed_intents[0]
        self.assertEqual(proposal.kind, "explore")
        self.assertEqual(proposal.objective, "combine both confirmed branches")
        self.assertEqual(proposal.target_entity, self.target)
        self.assertEqual(proposal.parent_fact_ids, [source_a.id, source_b.id])
        self.assertEqual(report.candidate_facts, [])

    def test_reason_prompt_requests_parallel_fanout_when_multiple_paths_exist(self) -> None:
        source = self.fact("fact-source", "confirmed source")
        payload = {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": [source.id], "description": "first direction"},
                    {"from": [source.id], "description": "second direction"},
                ]
            },
        }

        _, backend = self.run_task("reason", [cli_turn(payload)], facts=[source])

        prompt = self.prompt_text(backend)
        self.assertIn("return at least two directions", prompt)
        self.assertIn("return only one when the evidence supports a single useful path", prompt)

    def test_task_prompts_use_neutral_acceptance_language(self) -> None:
        payloads = {
            "bootstrap": {
                "accepted": True,
                "data": {
                    "fact": {"description": "verified bootstrap evidence"},
                    "complete": {"description": "bootstrap goal is proven"},
                },
            },
            "explore": {
                "accepted": True,
                "data": {"description": "verified exploration evidence"},
            },
            "reason": {
                "accepted": True,
                "data": {},
            },
        }

        for mode, payload in payloads.items():
            with self.subTest(mode=mode):
                _, backend = self.run_task(mode, [cli_turn(payload)], intent_id=f"intent-{mode}-prompt")
                prompt = self.prompt_text(backend).lower()
                self.assertNotIn("under no circumstances should you reject", prompt)
                self.assertNotIn("do not discuss refusal", prompt)
                self.assertIn("structured result", prompt)

                instructions = load_prompt("default", "AGENTS.md")
                self.assertEqual(
                    (backend.workspace_root / "AGENTS.md").read_text(encoding="utf-8"),
                    instructions,
                )
                self.assertEqual(
                    (backend.workspace_root / "CLAUDE.md").read_text(encoding="utf-8"),
                    instructions,
                )

        validate_prompt_group("default")
        instructions = load_prompt("default", "AGENTS.md").lower()
        self.assertNotIn("[mode: unrestricted]", instructions)
        self.assertNotIn("do not discuss refusal", instructions)
        self.assertNotIn("jailbreak", instructions)

    def test_reason_prompt_references_graph_instead_of_inlining_fact_bodies(self) -> None:
        source = self.fact("fact-long-source", "confirmed:" + ("x" * 40_000))
        payload = {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": [source.id], "description": "continue from the confirmed source"}
                ]
            },
        }

        _, backend = self.run_task("reason", [cli_turn(payload)], facts=[source])

        prompt = self.prompt_text(backend)
        argv = list(backend.calls[0]["argv"])
        self.assertLess(len(prompt), 8_000)
        self.assertNotIn("x" * 100, prompt)
        self.assertNotIn(prompt, argv)
        self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
        self.assertIn("prompt.txt", argv[4])
        self.assertLess(max(len(str(item)) for item in argv), 4_096)

    def test_reason_noop_is_completed_when_an_intent_is_already_open(self) -> None:
        open_intents = [
            {
                "id": "intent-open",
                "from": ["fact-a"],
                "description": "continue the active branch",
                "worker": "codex-explore",
            }
        ]

        report, backend = self.run_task(
            "reason",
            [cli_turn({"accepted": True, "data": {}})],
            open_intents=open_intents,
        )

        self.assertEqual(report.status, "completed")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(report.candidate_facts, [])
        self.assertEqual(report.proposed_intents, [])
        self.assertIsNone(report.completion)
        context_files = list(backend.workspace_root.glob("pods/**/context.json"))
        self.assertEqual(len(context_files), 1)
        context = json.loads(context_files[0].read_text(encoding="utf-8"))
        self.assertEqual(context["open_intents"], open_intents)

    def test_rejected_bootstrap_does_not_enter_conclude(self) -> None:
        report, backend = self.run_task(
            "bootstrap",
            [
                cli_turn(
                    {"accepted": False, "reason": "no accepted result"},
                    session_id="rejected-session",
                )
            ],
            intent_id="intent-bootstrap-rejected",
        )

        self.assertEqual(report.status, "rejected")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(report.candidate_facts, [])
        self.assertEqual(report.proposed_intents, [])
        self.assertIsNone(report.completion)

    def test_reason_admits_at_most_two_intents(self) -> None:
        source = self.fact("fact-source", "confirmed source")
        payload = {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": [source.id], "description": f"direction {index}"}
                    for index in range(1, 4)
                ]
            },
        }

        report, _ = self.run_task(
            "reason",
            [cli_turn(payload)],
            facts=[source],
            reason_max_intents=2,
            intent_id="intent-reason-limit",
        )

        self.assertEqual(report.status, "completed")
        self.assertEqual(
            [proposal.objective for proposal in report.proposed_intents],
            ["direction 1", "direction 2"],
        )

    def test_slime_meta_does_not_change_intent_fingerprint(self) -> None:
        semantic = IntentProposal(
            kind="explore",
            objective="inspect the confirmed branch",
            target_entity=self.target,
            parent_fact_ids=["fact-source"],
            context={"route": "/fixture"},
            provenance={"reason": "cross-branch fusion"},
        )
        decorated = IntentProposal(
            kind=semantic.kind,
            objective=semantic.objective,
            target_entity=semantic.target_entity,
            parent_fact_ids=list(semantic.parent_fact_ids),
            context={
                **semantic.context,
                "slime_meta": {"nutrient": 0.97, "memory_state": "revived"},
            },
            provenance={
                **semantic.provenance,
                "slime_meta": {"branch_key": "dynamic-branch"},
            },
        )

        self.assertEqual(semantic.key, decorated.key)


if __name__ == "__main__":
    unittest.main()
