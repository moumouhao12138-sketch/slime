from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from slime_cairn.server import api
from slime_cairn.integrations.benchmark.client import BenchmarkClient, BenchmarkError, BenchmarkSettings
from slime_cairn.integrations.benchmark.automation import BenchmarkAutomationService
from slime_cairn.integrations.benchmark.runtime import (
    BenchmarkProjectController,
    extract_flag_candidates,
    extract_submission_candidates,
)
from slime_cairn.server.blackboard import Blackboard
from slime_cairn.protocol.contracts import validate_reason_payload
from slime_cairn.domain.models import CompletionProposal, FactCandidate, IntentProposal, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler


class BenchmarkPlatformFixture:
    def __init__(self) -> None:
        self.token = "fixture-benchmark-secret-token"
        self.address = "10.20.30.40:8080"
        self.correct_flags = {"flag{one}": 0, "flag{two}": 1}
        self.accepted: set[str] = set()
        self.status = "stopped"
        self.submit_calls = 0
        self.hint_calls = 0

    def challenge(self) -> dict:
        return {
            "unique_code": "web_fixture_01",
            "description": "Inspect the fixture and find both flags",
            "difficulty": "medium",
            "level": 2,
            "total_score": 120,
            "flag_count": 2,
            "correct_flag_count": len(self.accepted),
            "is_completed": len(self.accepted) == 2,
            "container_status": self.status,
            "container_addr": [self.address] if self.status == "available" else [],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("BENCHMARK_TOKEN") != self.token:
            return httpx.Response(404, json={"code": "task_not_found", "message": "bad token", "detail": {}})
        path = request.url.path
        if path == "/openapi/v1/challenges" and request.method == "GET":
            return httpx.Response(200, json=[self.challenge()])
        if path.endswith("/start"):
            self.status = "available"
            return httpx.Response(200, json={"unique_code": "web_fixture_01", "container_addr": [self.address]})
        if path.endswith("/hint"):
            self.hint_calls += 1
            return httpx.Response(200, json={"unique_code": "web_fixture_01", "hint": "fixture hint"})
        if path.endswith("/submit"):
            self.submit_calls += 1
            body = json.loads(request.content)
            candidate = body["flag"]
            if candidate in self.accepted:
                return httpx.Response(409, json={"code": "duplicate", "message": "already submitted", "detail": {}})
            correct = candidate in self.correct_flags
            if correct:
                self.accepted.add(candidate)
            return httpx.Response(
                200,
                json={
                    "correct": correct,
                    "awarded": 60 if correct else 0,
                    "cumulative_score": len(self.accepted) * 60,
                    "correct_flag_count": len(self.accepted),
                    "total_flag_count": 2,
                    "matched_flag_index": self.correct_flags.get(candidate),
                },
            )
        if path.endswith("/close"):
            self.status = "stopped"
            return httpx.Response(200, json={"unique_code": "web_fixture_01", "closed": True})
        return httpx.Response(404, json={"code": "challenge_not_found", "message": "missing", "detail": {}})


class MultiChallengePlatformFixture:
    def __init__(self, count: int = 5) -> None:
        self.token = "fixture-multi-challenge-token"
        self.codes = [f"challenge_{index:02d}" for index in range(1, count + 1)]
        self.statuses = {code: "stopped" for code in self.codes}
        self.completed: set[str] = set()
        self.start_calls: list[str] = []
        self.close_calls: list[str] = []

    def challenges(self) -> list[dict]:
        return [
            {
                "unique_code": code,
                "description": f"Solve fixture {code}",
                "difficulty": "medium",
                "level": index,
                "total_score": 100,
                "flag_count": 1,
                "correct_flag_count": int(code in self.completed),
                "is_completed": code in self.completed,
                "container_status": self.statuses[code],
                "container_addr": [f"10.0.0.{index}:8080"] if self.statuses[code] == "available" else [],
            }
            for index, code in enumerate(self.codes, 1)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("BENCHMARK_TOKEN") != self.token:
            return httpx.Response(404, json={"code": "task_not_found", "message": "bad token", "detail": {}})
        path = request.url.path
        code = request.url.params.get("unique_code")
        if path == "/openapi/v1/challenges" and request.method == "GET":
            return httpx.Response(200, json=self.challenges())
        if path.endswith("/start"):
            if sum(status != "stopped" for status in self.statuses.values()) >= 3:
                return httpx.Response(409, json={"code": "invalid_state", "message": "instance limit", "detail": {}})
            self.statuses[code] = "available"
            self.start_calls.append(code)
            index = self.codes.index(code) + 1
            return httpx.Response(200, json={"unique_code": code, "container_addr": [f"10.0.0.{index}:8080"]})
        if path.endswith("/close"):
            self.statuses[code] = "stopped"
            self.close_calls.append(code)
            return httpx.Response(200, json={"unique_code": code, "closed": True})
        return httpx.Response(404, json={"code": "challenge_not_found", "message": "missing", "detail": {}})


class CandidateReasonMind:
    def __init__(self, submissions: list[str]) -> None:
        self.submissions = submissions

    def run_cairn_task(self, intent, facts, mode, capsule):
        fact_ids = [capsule.manifest.included_fact_ids[0]]
        return PseudopodReport(
            pseudopod_id="fixture",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=1.0,
            stop_reason="candidate_ready",
            completion=CompletionProposal(
                fact_ids,
                "Evidence supports explicit candidates",
                submissions=self.submissions,
            ),
        )


class CandidateExploreMind:
    def run_cairn_task(self, intent, facts, mode, capsule):
        return PseudopodReport(
            pseudopod_id="fixture-explore",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    subject=intent.target_entity,
                    predicate="cairn_fact",
                    object="confirmed exact flag from the response",
                    confidence=0.95,
                    evidence_refs=["fixture-explore-evidence"],
                    attributes={"benchmark_submissions": ["flag{one}"]},
                )
            ],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=10.0,
            stop_reason="candidate_ready",
        )


class AutoExtractExploreMind:
    def run_cairn_task(self, intent, facts, mode, capsule):
        return PseudopodReport(
            pseudopod_id="fixture-auto-explore",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    subject=intent.target_entity,
                    predicate="cairn_fact",
                    object="The response disclosed flag{one}; flag{two} is the next value.",
                    confidence=0.95,
                    evidence_refs=["fixture-auto-explore-evidence"],
                )
            ],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=10.0,
            stop_reason="candidate_ready",
        )


class AutoExtractArbitraryExploreMind:
    def run_cairn_task(self, intent, facts, mode, capsule):
        return PseudopodReport(
            pseudopod_id="fixture-auto-arbitrary-explore",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    subject=intent.target_entity,
                    predicate="cairn_fact",
                    object=(
                        "The response disclosed flag: TOKEN-ONE; "
                        "answer = 88341566-f0b6-4b1a-bde8-e45ba8ed52f8"
                    ),
                    confidence=0.95,
                    evidence_refs=["fixture-auto-arbitrary-evidence"],
                )
            ],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=10.0,
            stop_reason="candidate_ready",
        )


class BenchmarkIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.platform = BenchmarkPlatformFixture()
        self.settings = BenchmarkSettings("https://benchmark.fixture", self.platform.token, 5)
        self.client = BenchmarkClient(
            self.settings,
            transport=httpx.MockTransport(self.platform.handler),
        )
        self.controller = BenchmarkProjectController(self.board, self.client, self.settings)

    def tearDown(self) -> None:
        self.client.close_client()
        self.board.close()
        self.temporary.cleanup()

    def test_settings_load_env_file_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "BENCHMARK_BASE_URL=https://platform.fixture/\n"
                "BENCHMARK_TOKEN=super-secret\n"
                "BENCHMARK_TIMEOUT=17\n",
                encoding="utf-8",
            )
            settings = BenchmarkSettings.from_env(environment={}, cwd=root)

        self.assertEqual(settings.base_url, "https://platform.fixture")
        self.assertEqual(settings.timeout, 17)
        self.assertTrue(settings.configured)
        self.assertNotIn("super-secret", repr(settings))
        self.assertNotIn("super-secret", json.dumps(settings.public_status()))

    def test_extract_flag_candidates_ignores_obvious_placeholders(self) -> None:
        self.assertEqual(
            extract_flag_candidates(
                "real flag{one}; examples flag{...}, flag{YOUR_FLAG}, flag{redacted}"
            ),
            ["flag{one}"],
        )

    def test_extract_submission_candidates_does_not_require_a_flag_wrapper(self) -> None:
        self.assertEqual(
            extract_submission_candidates(
                "legacy flag{one}; flag: TOKEN-42; "
                "answer = `88341566-f0b6-4b1a-bde8-e45ba8ed52f8`; "
                "答案：'answer with spaces'; candidate: unknown"
            ),
            [
                "flag{one}",
                "TOKEN-42",
                "88341566-f0b6-4b1a-bde8-e45ba8ed52f8",
                "answer with spaces",
            ],
        )

    def test_fact_flag_in_description_is_not_submitted_without_explicit_submissions_field(self) -> None:
        project_id = self.controller.start("web_fixture_01")["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=AutoExtractExploreMind(),
            benchmark_client=self.client,
        )
        intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="collect a response candidate",
                target_entity=self.platform.address,
                parent_fact_ids=[],
            ),
            0.8,
        )
        claimed = self.board.claim_intent(project_id, intent.id, "fixture-auto-owner")
        self.assertIsNotNone(claimed)

        result = scheduler.process_claimed_intent(
            claimed,
            mind=scheduler.mind,
            worker_name="fixture-auto-explore",
            owner_token="fixture-auto-owner",
            retry_failed=True,
        )

        self.assertEqual(self.platform.submit_calls, 0)
        self.assertIsNone(result["benchmark_submission"])
        verified = [
            fact for fact in self.board.list_facts(project_id)
            if fact.predicate == "benchmark_flag_verified"
        ]
        self.assertEqual(verified, [])

    def test_fact_candidate_with_arbitrary_format_is_not_submitted_without_explicit_submissions(self) -> None:
        self.platform.correct_flags = {
            "TOKEN-ONE": 0,
            "88341566-f0b6-4b1a-bde8-e45ba8ed52f8": 1,
        }
        project_id = self.controller.start("web_fixture_01")["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=AutoExtractArbitraryExploreMind(),
            benchmark_client=self.client,
        )
        intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="collect arbitrary-format response candidates",
                target_entity=self.platform.address,
                parent_fact_ids=[],
            ),
            0.8,
        )
        claimed = self.board.claim_intent(project_id, intent.id, "fixture-arbitrary-owner")
        self.assertIsNotNone(claimed)

        result = scheduler.process_claimed_intent(
            claimed,
            mind=scheduler.mind,
            worker_name="fixture-auto-arbitrary-explore",
            owner_token="fixture-arbitrary-owner",
            retry_failed=True,
        )

        self.assertEqual(self.platform.submit_calls, 0)
        self.assertIsNone(result["benchmark_submission"])

    def test_client_normalizes_business_and_framework_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(
                    422,
                    json={"detail": [{"loc": ["body", "flag"], "msg": "too short", "type": "value_error"}]},
                )
            return httpx.Response(404, json={"code": "challenge_not_found", "message": "missing", "detail": {}})

        client = BenchmarkClient(self.settings, transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(BenchmarkError) as validation:
                client.submit("web_fixture_01", "x")
            self.assertEqual(validation.exception.code, "validation_error")
            self.assertEqual(validation.exception.status_code, 422)
            with self.assertRaises(BenchmarkError) as missing:
                client.start("missing")
            self.assertEqual(missing.exception.code, "challenge_not_found")
        finally:
            client.close_client()

    def test_lifecycle_multiflag_hint_gate_completion_gate_and_secret_boundary(self) -> None:
        started = self.controller.start("web_fixture_01")
        project_id = started["project"]["id"]
        project = self.board.get_project(project_id)
        self.assertFalse(project.scope["bootstrap_enabled"])
        self.assertEqual(project.target, self.platform.address)
        self.assertNotIn(self.platform.token, json.dumps(self.board.snapshot(project_id), default=str))

        seed_fact = self.board.list_facts(project_id)[0]
        with self.assertRaisesRegex(ValueError, "平台完成验证"):
            self.board.complete_project(project_id, [seed_fact.id], "model-only claim", "reason")

        with self.assertRaisesRegex(BenchmarkError, "confirm_score_penalty"):
            self.controller.hint("web_fixture_01", confirm_score_penalty=False)
        hinted = self.controller.hint("web_fixture_01", confirm_score_penalty=True)
        self.assertEqual(hinted["blackboard_hint"]["content"], "fixture hint")
        self.assertEqual(self.platform.hint_calls, 1)

        rejected = self.controller.submit_candidates(project_id, ["flag{wrong}"], worker_name="reason")
        self.assertFalse(rejected["outcomes"][0]["correct"])
        self.assertIsNone(rejected["completion"])

        first = self.controller.submit_candidates(project_id, ["flag{one}"], worker_name="reason")
        self.assertEqual(first["progress"]["correct_flag_count"], 1)
        self.assertIsNone(first["completion"])
        calls_before_duplicate = self.platform.submit_calls
        duplicate = self.controller.submit_candidates(project_id, ["flag{one}"], worker_name="reason")
        self.assertTrue(duplicate["outcomes"][0]["local_duplicate"])
        self.assertEqual(self.platform.submit_calls, calls_before_duplicate)

        complete = self.controller.submit_candidates(
            project_id,
            ["flag{two}"],
            worker_name="reason",
            completion_fact_ids=[seed_fact.id],
            completion_description="Both candidates are evidence-backed",
        )
        self.assertIsNotNone(complete["completion"])
        self.assertEqual(self.board.get_project(project_id).status, "completed")
        self.assertIn("flag{one}", complete["completion"]["description"])
        self.assertIn("flag{two}", complete["completion"]["description"])
        predicates = {fact.predicate for fact in self.board.list_facts(project_id)}
        self.assertIn("benchmark_flag_rejected", predicates)
        self.assertIn("benchmark_flag_verified", predicates)
        self.assertIn("benchmark_completion_verified", predicates)

        closed = self.controller.close("web_fixture_01")
        self.assertTrue(closed["closed"])
        self.assertEqual(self.board.get_project(project_id).status, "completed")

    def test_close_stops_incomplete_project_and_restart_updates_address(self) -> None:
        project_id = self.controller.start("web_fixture_01")["project"]["id"]
        stale_intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="inspect the current fixture instance",
                target_entity=self.platform.address,
                parent_fact_ids=[],
            ),
            0.8,
        )
        self.controller.close("web_fixture_01")
        self.assertEqual(self.board.get_project(project_id).status, "stopped")

        self.platform.address = "10.20.30.41:9090"
        restarted = self.controller.start("web_fixture_01")
        self.assertEqual(restarted["project"]["id"], project_id)
        self.assertEqual(restarted["project"]["status"], "running")
        self.assertEqual(restarted["project"]["target"], self.platform.address)
        archived = self.board.get_intent(project_id, stale_intent.id)
        self.assertEqual(archived.status, "dormant")
        self.assertEqual(archived.last_error, "benchmark_instance_target_replaced")
        self.assertTrue(
            any(fact.predicate == "benchmark_instance_target" for fact in self.board.list_facts(project_id))
        )
        self.assertTrue(
            any("address changed" in hint.content for hint in self.board.list_hints(project_id))
        )

    def test_scheduler_turns_unverified_completion_into_follow_up_branch(self) -> None:
        project_id = self.controller.start("web_fixture_01")["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=CandidateReasonMind(["flag{wrong}"]),
            benchmark_client=self.client,
        )

        created = scheduler.reason(worker_name="fixture-reason")

        self.assertEqual(created, 1)
        self.assertIsNone(scheduler.last_reason_completion)
        pending = self.board.list_intents(project_id, "pending")
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].context["benchmark_follow_up"])
        self.assertIn("0/2", pending[0].objective)

    def test_explore_explicit_candidate_is_submitted_and_verified(self) -> None:
        project_id = self.controller.start("web_fixture_01")["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=CandidateExploreMind(),
            benchmark_client=self.client,
        )
        intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="collect the exact response candidate",
                target_entity=self.platform.address,
                parent_fact_ids=[],
            ),
            0.8,
        )
        claimed = self.board.claim_intent(project_id, intent.id, "fixture-owner")
        self.assertIsNotNone(claimed)

        result = scheduler.process_claimed_intent(
            claimed,
            mind=scheduler.mind,
            worker_name="fixture-explore",
            owner_token="fixture-owner",
            retry_failed=True,
        )

        self.assertEqual(self.platform.submit_calls, 1)
        self.assertEqual(result["benchmark_submission"]["progress"]["correct_flag_count"], 1)
        self.assertEqual(
            len([fact for fact in self.board.list_facts(project_id) if fact.predicate == "benchmark_flag_verified"]),
            1,
        )

    def test_reason_contract_preserves_explicit_submissions(self) -> None:
        kind, data = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "complete": {
                        "from": ["fact_1"],
                        "description": "candidate derived from response evidence",
                        "submissions": [" flag{one} ", "flag{one}"],
                    }
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )

        self.assertEqual(kind, "complete")
        self.assertEqual(data["submissions"], ["flag{one}"])

    def test_automation_keeps_three_slots_full_and_closes_completed_instances(self) -> None:
        platform = MultiChallengePlatformFixture()
        settings = BenchmarkSettings("https://benchmark.fixture", platform.token, 5)
        client = BenchmarkClient(settings, transport=httpx.MockTransport(platform.handler))
        automation = BenchmarkAutomationService(self.board, client, settings, interval=0.01)
        self.board.configure_benchmark_automation(settings.task_key, enabled=True, parallelism=3)
        try:
            first = automation.tick()
            self.assertEqual(first["active_count"], 3)
            self.assertEqual(first["queued_count"], 2)
            self.assertEqual(platform.start_calls, platform.codes[:3])

            completed = platform.codes[1]
            resumed_project = automation.controller.find_project(platform.codes[0])
            self.assertIsNotNone(resumed_project)
            self.board.set_project_status(resumed_project.id, "stopped")
            platform.completed.add(completed)
            second = automation.tick()
            self.assertIn(completed, platform.close_calls)
            self.assertEqual(platform.start_calls, platform.codes[:4])
            self.assertEqual(second["active_count"], 3)
            self.assertEqual(second["completed_count"], 1)
            self.assertEqual(self.board.get_project(resumed_project.id).status, "running")

            while len(platform.completed) < len(platform.codes):
                platform.completed.update(
                    code for code, status in platform.statuses.items() if status == "available"
                )
                final = automation.tick()

            self.assertFalse(final["enabled"])
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["completed_count"], len(platform.codes))
            self.assertEqual(platform.start_calls, platform.codes)
            self.assertTrue(all(status == "stopped" for status in platform.statuses.values()))
        finally:
            client.close_client()

    def test_api_exposes_lifecycle_without_returning_credential(self) -> None:
        previous_board = api.board
        api.board = self.board
        environment = {
            "BENCHMARK_BASE_URL": self.settings.base_url,
            "BENCHMARK_TOKEN": self.settings.token,
            "BENCHMARK_TIMEOUT": "5",
        }

        def client_factory(settings: BenchmarkSettings) -> BenchmarkClient:
            return BenchmarkClient(settings, transport=httpx.MockTransport(self.platform.handler))

        try:
            with patch.dict("os.environ", environment, clear=False), patch.object(
                api, "BenchmarkClient", side_effect=client_factory
            ):
                web = TestClient(api.app)
                status = web.get("/benchmark/status")
                self.assertEqual(status.status_code, 200)
                self.assertTrue(status.json()["configured"])
                self.assertFalse(status.json()["automation"]["enabled"])
                self.assertNotIn(self.settings.token, status.text)

                automated = web.post("/benchmark/automation/start", json={"parallelism": 3})
                self.assertEqual(automated.status_code, 200)
                self.assertTrue(automated.json()["enabled"])
                self.assertEqual(automated.json()["parallelism"], 3)
                automation_status = web.get("/benchmark/automation")
                self.assertTrue(automation_status.json()["enabled"])

                listed = web.get("/benchmark/challenges")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(len(listed.json()["challenges"]), 1)

                started = web.post("/benchmark/challenges/web_fixture_01/start")
                self.assertEqual(started.status_code, 200)
                project_id = started.json()["project"]["id"]

                unconfirmed = web.post(
                    "/benchmark/challenges/web_fixture_01/hint",
                    json={"confirm_score_penalty": False},
                )
                self.assertEqual(unconfirmed.status_code, 409)
                confirmed = web.post(
                    "/benchmark/challenges/web_fixture_01/hint",
                    json={"confirm_score_penalty": True},
                )
                self.assertEqual(confirmed.status_code, 200)

                first = web.post(
                    "/benchmark/challenges/web_fixture_01/submit",
                    json={"flag": "flag{one}"},
                )
                self.assertEqual(first.status_code, 200)
                second = web.post(
                    "/benchmark/challenges/web_fixture_01/submit",
                    json={"flag": "flag{two}"},
                )
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.json()["project"]["status"], "completed")

                closed = web.post("/benchmark/challenges/web_fixture_01/close")
                self.assertEqual(closed.status_code, 200)
                self.assertTrue(closed.json()["closed"])
                self.assertEqual(self.board.get_project(project_id).status, "completed")
                self.assertNotIn(self.settings.token, json.dumps(self.board.snapshot(project_id), default=str))

                stopped = web.post("/benchmark/automation/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertFalse(stopped.json()["enabled"])
        finally:
            api.board = previous_board


if __name__ == "__main__":
    unittest.main()
