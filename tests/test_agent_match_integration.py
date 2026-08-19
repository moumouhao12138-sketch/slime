from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from slime_cairn import __version__
from slime_cairn.dispatcher.scheduler import Scheduler
from slime_cairn.integrations.agent_match.client import (
    AgentMatchClient,
    AgentMatchSettings,
)
from slime_cairn.integrations.agent_match.runtime import AgentMatchProjectController
from slime_cairn.server import api
from slime_cairn.server.blackboard import Blackboard
from slime_cairn.workers.model_gateway import validate_competition_model_route
from slime_cairn.domain.models import FactCandidate, IntentProposal, PseudopodReport


class AgentMatchPlatformFixture:
    def __init__(self) -> None:
        self.access_key = "agent-access-secret"
        self.initialized = False
        self.solved = False
        self.calls: list[httpx.Request] = []

    def _detail(self) -> dict:
        ready = self.initialized
        return {
            "id": 1001,
            "name": "fixture-web",
            "description": "Inspect the authorized fixture service.",
            "hasSolved": self.solved,
            "score": "100",
            "difficulty": "EASY",
            "attachment": {"files": [{"name": "fixture.zip", "url": "https://download.fixture/file", "ext": "zip"}]},
            "endpoints": [
                {
                    "exposeIps": ["10.0.0.10"],
                    "ports": ["80"],
                    "users": [{"username": "ctf", "password": "fixture-password"}],
                    "portMappings": [{"type": "tcp", "port": "80", "proxy": "30080"}],
                    "proxyIps": ["198.51.100.10"],
                    "isProxy": True,
                    "expireTime": 1780000000000,
                }
            ] if ready else [],
            "isNeedInit": not ready,
            "isNeedCheck": False,
        }

    @staticmethod
    def envelope(data: object, code: str = "00000", message: str = "") -> dict:
        return {"code": code, "message": message, "data": data}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.headers.get("X-Agent-AccessKey") != self.access_key:
            return httpx.Response(401, json=self.envelope({}, "UNAUTHORIZED", "invalid key"))
        path = request.url.path
        if path.endswith("/match/notice/match-info"):
            return httpx.Response(200, json=self.envelope({"note": "authorized only", "rule": "fixture rule"}))
        if path.endswith("/answer-panel/overview"):
            return httpx.Response(200, json=self.envelope({"stagePoint": 10, "stageRank": 1}))
        if path.endswith("/match/notice/now-list"):
            return httpx.Response(200, json=self.envelope([]))
        if path.endswith("/ctf/exercise-list"):
            return httpx.Response(
                200,
                json=self.envelope(
                    [{"id": 10, "name": "Web", "order": 1, "corpus": [{"id": 1001, "name": "fixture-web", "order": 1, "isOpen": True, "hasSolved": self.solved}]}]
                ),
            )
        if path.endswith("/ctf/exercise"):
            return httpx.Response(200, json=self.envelope(self._detail()))
        if path.endswith("/ctf/build-exercise-env"):
            self.initialized = True
            return httpx.Response(200, json=self.envelope({}))
        if path.endswith("/ctf/recover-exercise-env"):
            self.initialized = False
            return httpx.Response(200, json=self.envelope({}))
        if path.endswith("/answer-panel/answer"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload["flag"] != "fixture":
                return httpx.Response(200, json=self.envelope({}, "ANSWER_INCORRECT", "wrong answer"))
            self.solved = True
            return httpx.Response(200, json=self.envelope({"isCorrect": True}))
        return httpx.Response(404, json=self.envelope({}, "NOT_FOUND", "missing"))


class FlagLikeFactMind:
    def __init__(self, *, explicit: bool) -> None:
        self.explicit = explicit

    def run_cairn_task(self, intent, facts, mode, capsule):
        attributes = {"submission_candidates": ["fixture"]} if self.explicit else {}
        return PseudopodReport(
            pseudopod_id="fixture-flag-like-fact",
            intent_id=intent.id,
            mode=mode,
            status="completed",
            candidate_facts=[
                FactCandidate(
                    subject=intent.target_entity,
                    predicate="cairn_fact",
                    object="The prompt contains flag{fixture}, but this is only an unverified example.",
                    confidence=0.95,
                    evidence_refs=["fixture-flag-like-evidence"],
                    attributes=attributes,
                )
            ],
            evidence_refs=[],
            proposed_intents=[],
            tool_calls=0,
            progress_score=10.0,
            stop_reason="candidate_ready",
        )


class AgentMatchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.platform = AgentMatchPlatformFixture()
        self.settings = AgentMatchSettings(
            "https://match.fixture",
            self.platform.access_key,
            timeout=5,
            environment_poll_interval=0.001,
            environment_ready_timeout=1,
        )
        self.client = AgentMatchClient(
            self.settings,
            transport=httpx.MockTransport(self.platform.handler),
        )
        self.controller = AgentMatchProjectController(self.board, self.client, self.settings)

    def tearDown(self) -> None:
        self.client.close_client()
        self.board.close()
        self.temporary.cleanup()

    def test_start_submit_recover_and_secret_boundary(self) -> None:
        exercises = self.controller.list_exercises()
        self.assertEqual(exercises[0]["id"], 1001)
        self.assertIsNone(exercises[0]["project_id"])

        started = self.controller.start(1001)
        project_id = started["project"]["id"]
        project = self.board.get_project(project_id)
        self.assertFalse(project.scope["bootstrap_enabled"])
        self.assertEqual(project.target, "198.51.100.10:30080")
        self.assertTrue(project.scope["submission"]["managed"])
        self.assertEqual(project.scope["submission"]["platform"], "agent_match")
        self.assertEqual(project.scope["agent_match"]["category_id"], 10)
        self.assertEqual(project.scope["agent_match"]["category_name"], "Web")
        self.assertNotIn(self.platform.access_key, json.dumps(self.board.snapshot(project_id), default=str))
        self.assertTrue(any(request.url.path.endswith("/ctf/build-exercise-env") for request in self.platform.calls))
        self.assertTrue(
            all(request.headers.get("User-Agent") == f"slime-cairn/{__version__}" for request in self.platform.calls)
        )

        rejected = self.controller.submit_candidates(project_id, ["flag{wrong}"], worker_name="fixture")
        self.assertFalse(rejected["outcomes"][0]["correct"])
        self.assertIsNone(rejected["completion"])

        verified = self.controller.submit_candidates(project_id, ["DASCTF{fixture}"], worker_name="fixture")
        self.assertTrue(verified["outcomes"][0]["correct"])
        self.assertEqual(verified["outcomes"][0]["candidate"], "fixture")
        self.assertIsNotNone(verified["completion"])
        self.assertEqual(self.board.get_project(project_id).status, "completed")
        predicates = {fact.predicate for fact in self.board.list_facts(project_id)}
        self.assertIn("agent_match_flag_rejected", predicates)
        self.assertIn("agent_match_flag_verified", predicates)
        self.assertIn("agent_match_completion_verified", predicates)
        generated_writeup = self.board.get_project_writeup(project_id)
        self.assertIsNotNone(generated_writeup)
        self.assertIn("fixture-web", generated_writeup["markdown"])
        self.assertTrue(
            any(
                request.url.path.endswith("/ctf/recover-exercise-env")
                for request in self.platform.calls
            ),
            "a platform-verified completion must reclaim its remote exercise environment",
        )
        self.assertTrue(self.board.get_project(project_id).scope["agent_match"]["environment_recovered"])

        recovered = self.controller.recover(1001)
        self.assertTrue(recovered["recovered"])

    def test_submission_normalization_strips_only_standard_flag_wrappers(self) -> None:
        normalize = AgentMatchProjectController._normalize_submission

        self.assertEqual(normalize("DASCTF{answer-value}"), "answer-value")
        self.assertEqual(normalize("flag{answer-value}"), "answer-value")
        self.assertEqual(normalize("SPECIAL::answer-value"), "SPECIAL::answer-value")

    def test_scheduler_selects_agent_match_submission_controller(self) -> None:
        project_id = self.controller.start(1001)["project"]["id"]
        scheduler = Scheduler(self.board, project_id, agent_match_client=self.client)
        selected = scheduler._submission_controller(self.board.get_project(project_id))
        self.assertIsInstance(selected, AgentMatchProjectController)

    def test_scheduler_does_not_submit_flag_like_fact_text_without_explicit_candidate(self) -> None:
        project_id = self.controller.start(1001)["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=FlagLikeFactMind(explicit=False),
            agent_match_client=self.client,
        )
        intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="inspect the fixture response",
                target_entity=self.board.get_project(project_id).target,
                parent_fact_ids=[],
            ),
            0.8,
        )
        claimed = self.board.claim_intent(project_id, intent.id, "fixture-owner")
        result = scheduler.process_claimed_intent(
            claimed,
            mind=scheduler.mind,
            worker_name="fixture-explore",
            owner_token="fixture-owner",
            retry_failed=True,
        )

        self.assertIsNone(result["benchmark_submission"])
        self.assertFalse(self.platform.solved)
        self.assertFalse(any(request.url.path.endswith("/answer-panel/answer") for request in self.platform.calls))

    def test_scheduler_submits_only_explicit_agent_match_candidate(self) -> None:
        project_id = self.controller.start(1001)["project"]["id"]
        scheduler = Scheduler(
            self.board,
            project_id,
            mind=FlagLikeFactMind(explicit=True),
            agent_match_client=self.client,
        )
        intent, _ = self.board.add_intent(
            project_id,
            IntentProposal(
                kind="explore",
                objective="inspect the verified fixture response",
                target_entity=self.board.get_project(project_id).target,
                parent_fact_ids=[],
            ),
            0.8,
        )
        claimed = self.board.claim_intent(project_id, intent.id, "fixture-explicit-owner")
        result = scheduler.process_claimed_intent(
            claimed,
            mind=scheduler.mind,
            worker_name="fixture-explore",
            owner_token="fixture-explicit-owner",
            retry_failed=True,
        )

        self.assertIsNotNone(result["benchmark_submission"])
        self.assertTrue(self.platform.solved)

    def test_api_status_and_exercise_list_do_not_return_access_key(self) -> None:
        previous_board = api.board
        api.board = self.board

        def client_factory(settings: AgentMatchSettings) -> AgentMatchClient:
            return AgentMatchClient(settings, transport=httpx.MockTransport(self.platform.handler))

        environment = {
            "AGENT_MATCH_BASE_URL": self.settings.base_url,
            "AGENT_MATCH_ACCESS_KEY": self.platform.access_key,
            "AGENT_MATCH_TIMEOUT": "5",
        }
        try:
            with patch.dict("os.environ", environment, clear=False), patch.object(
                api, "AgentMatchClient", side_effect=client_factory
            ):
                from fastapi.testclient import TestClient

                web = TestClient(api.app)
                status = web.get("/agent-match/status")
                self.assertEqual(status.status_code, 200)
                self.assertTrue(status.json()["configured"])
                self.assertNotIn(self.platform.access_key, status.text)

                exercises = web.get("/agent-match/exercises")
                self.assertEqual(exercises.status_code, 200)
                self.assertEqual(exercises.json()["exercises"][0]["id"], 1001)
        finally:
            api.board = previous_board

    def test_competition_gateway_guard_requires_matching_whitelisted_route(self) -> None:
        environment = {
            "SLIME_COMPETITION_GATEWAY_ONLY": "true",
            "SLIME_CODEX_UPSTREAM_ENDPOINT": "https://api.deepseek.com/responses",
        }
        validate_competition_model_route(
            "codex-cli",
            "https://platform.fixture/llm-gateway/proxy/e/deepseek",
            "openai-responses",
            environment,
        )
        with self.assertRaisesRegex(ValueError, "whitelist"):
            validate_competition_model_route(
                "codex-cli",
                "https://platform.fixture/llm-gateway/proxy/e/deepseek",
                "openai-responses",
                {**environment, "SLIME_CODEX_UPSTREAM_ENDPOINT": "https://example.com/responses"},
            )


if __name__ == "__main__":
    unittest.main()
