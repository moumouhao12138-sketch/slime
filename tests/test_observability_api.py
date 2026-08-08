from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from slime_cairn import api
from slime_cairn.blackboard import Blackboard
from slime_cairn.models import FactCandidate, HypothesisCandidate, IntentProposal


class ObservabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = Blackboard(root / "board.db")
        self.previous_board = api.board
        api.board = self.board
        self.client = TestClient(api.app)
        self.project = self.board.create_project(
            "observability",
            "fixture.local",
            "reach the fixture goal",
            {"targets": ["fixture.local"]},
        )
        self.evidence_ref = "evidence://fixture/confirmed"
        self.board.register_evidence(self.project.id, self.evidence_ref, "fixture")
        self.origin, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "project_origin",
                "https://fixture.local/",
                1.0,
                [self.evidence_ref],
                attributes={"role": "origin", "pinned": True},
            ),
        )
        self.goal, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "project_goal",
                "return an evidence-backed result",
                1.0,
                [self.evidence_ref],
                attributes={"role": "goal", "pinned": True},
            ),
        )
        self.parent, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "reachable",
                "true",
                0.9,
                [self.evidence_ref],
            ),
        )
        self.intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="inspect the fixture response",
                target_entity="fixture.local",
                parent_fact_ids=[self.parent.id],
                context={"branch": "fixture"},
            ),
            nutrient=0.8,
        )
        self.board.claim_next_intent(self.project.id, "fixture-worker")
        self.output, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "response_marker",
                "confirmed",
                0.95,
                [self.evidence_ref],
                source_intent_id=self.intent.id,
            ),
        )
        self.hypothesis, _ = self.board.add_hypothesis(
            self.project.id,
            HypothesisCandidate(
                statement="the marker advances the goal",
                supporting_fact_ids=[self.output.id],
                evidence_refs=[self.evidence_ref],
                confidence=0.7,
                next_validation="reason over the marker",
                source_intent_id=self.intent.id,
            ),
        )
        self.board.add_fact_relation(
            self.project.id,
            self.parent.id,
            self.output.id,
            "supports",
            weight=0.8,
        )

    def tearDown(self) -> None:
        api.board = self.previous_board
        self.board.close()
        self.temporary.cleanup()

    def test_view_exposes_graph_records_and_runtime_projection(self) -> None:
        response = self.client.get(f"/projects/{self.project.id}/view")

        self.assertEqual(response.status_code, 200)
        view = response.json()
        nodes = {node["id"]: node for node in view["graph"]["nodes"]}
        edge_kinds = {edge["kind"] for edge in view["graph"]["edges"]}

        self.assertEqual(view["project"]["id"], self.project.id)
        self.assertEqual(nodes[f"fact:{self.origin.id}"]["kind"], "origin")
        self.assertEqual(nodes[f"fact:{self.goal.id}"]["kind"], "goal")
        self.assertEqual(nodes[f"intent:{self.intent.id}"]["status"], "running")
        self.assertEqual(nodes[f"hypothesis:{self.hypothesis.id}"]["kind"], "hypothesis")
        self.assertIn("intent_input", edge_kinds)
        self.assertIn("intent_output", edge_kinds)
        self.assertIn("intent_hypothesis", edge_kinds)
        self.assertIn("hypothesis_support", edge_kinds)
        self.assertIn("fact_relation", edge_kinds)
        self.assertEqual(view["summary"]["intent_status"]["running"], 1)
        self.assertEqual(view["runtime"]["active_leases"][0]["intent_id"], self.intent.id)
        self.assertGreaterEqual(view["latest_event_id"], 1)

    def test_readable_projection_groups_results_by_branch_and_hides_dormant_facts(self) -> None:
        self.assertTrue(
            self.board.finish_intent(
                self.project.id,
                self.intent.id,
                owner="fixture-worker",
            )
        )
        placeholder, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "cairn_fact",
                "latest confirmed incremental facts",
                0.9,
                [],
                source_intent_id=self.intent.id,
            ),
        )
        self.board.set_fact_memory_state(self.project.id, placeholder.id, "dormant")
        self.board.complete_project(
            self.project.id,
            [self.output.id],
            "the confirmed marker satisfies the fixture goal",
            "reason-worker",
        )

        view = self.client.get(f"/projects/{self.project.id}/view").json()
        readable = view["readable"]
        branch = next(item for item in readable["branches"] if item["id"] == self.intent.id)

        self.assertEqual(readable["completion"]["fact_ids"], [self.output.id])
        self.assertEqual([fact["id"] for fact in readable["key_facts"]], [self.output.id])
        self.assertEqual([fact["id"] for fact in branch["facts"]], [self.output.id])
        self.assertEqual(branch["group"], "result")
        self.assertNotIn(placeholder.id, {fact["id"] for fact in readable["key_facts"]})

    def test_readable_projection_does_not_show_terminal_project_intents_as_active(self) -> None:
        pending_intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="follow a direction that becomes obsolete after completion",
                target_entity="fixture.local",
                parent_fact_ids=[self.parent.id],
                context={"branch": "terminal-fixture"},
            ),
            nutrient=0.7,
        )
        self.assertTrue(
            self.board.finish_intent(
                self.project.id,
                self.intent.id,
                owner="fixture-worker",
            )
        )
        self.board.complete_project(
            self.project.id,
            [self.output.id],
            "the confirmed marker satisfies the fixture goal",
            "reason-worker",
        )

        readable = self.client.get(f"/projects/{self.project.id}/view").json()["readable"]
        branch = next(item for item in readable["branches"] if item["id"] == pending_intent.id)

        self.assertEqual(branch["group"], "attempted")
        self.assertEqual(readable["counts"]["active"], 0)

    def test_core_graph_uses_cairn_fact_intent_complete_semantics(self) -> None:
        self.assertTrue(
            self.board.finish_intent(
                self.project.id,
                self.intent.id,
                owner="fixture-worker",
            )
        )
        open_intent, _ = self.board.add_intent(
            self.project.id,
            IntentProposal(
                kind="explore",
                objective="follow the confirmed response marker",
                target_entity="fixture.local",
                parent_fact_ids=[self.output.id],
            ),
            nutrient=0.6,
        )
        completion, _ = self.board.complete_project(
            self.project.id,
            [self.output.id],
            "the confirmed marker satisfies the fixture goal",
            "reason-worker",
        )

        view = self.board.view_projection(self.project.id)
        core = view["core_graph"]
        nodes = {node["id"]: node for node in core["nodes"]}
        edges = core["edges"]

        self.assertEqual(core["schema"], "cairn_core_graph_v1")
        self.assertEqual(nodes[f"fact:{self.origin.id}"]["kind"], "origin")
        self.assertEqual(nodes[f"fact:{self.goal.id}"]["kind"], "goal")
        self.assertEqual(nodes[f"fact:{self.parent.id}"]["kind"], "fact")
        self.assertEqual(nodes[f"fact:{self.output.id}"]["kind"], "fact")

        # A concluded Intent is represented by its Fact -> Fact transition,
        # while an open Intent remains a placeholder node.
        self.assertNotIn(f"intent:{self.intent.id}", nodes)
        self.assertEqual(nodes[f"intent:{open_intent.id}"]["state"], "pending")
        concluded = next(edge for edge in edges if edge.get("intent_id") == self.intent.id)
        self.assertEqual(concluded["kind"], "intent")
        self.assertEqual(concluded["source"], f"fact:{self.parent.id}")
        self.assertEqual(concluded["target"], f"fact:{self.output.id}")
        open_input = next(
            edge
            for edge in edges
            if edge.get("intent_id") == open_intent.id and edge["kind"] == "intent_input"
        )
        self.assertEqual(open_input["source"], f"fact:{self.output.id}")
        self.assertEqual(open_input["target"], f"intent:{open_intent.id}")

        complete_edge = next(edge for edge in edges if edge["kind"] == "complete")
        self.assertEqual(complete_edge["source"], f"fact:{self.output.id}")
        self.assertEqual(complete_edge["target"], f"fact:{self.goal.id}")
        self.assertEqual(complete_edge["completion_id"], completion["id"])
        self.assertEqual(core["completion"]["fact_ids"], [self.output.id])

        # The existing expanded graph remains unchanged for older clients.
        self.assertEqual(view["graph"], self.board.graph_projection(self.project.id))
        legacy_nodes = {node["id"] for node in view["graph"]["nodes"]}
        self.assertIn(f"intent:{self.intent.id}", legacy_nodes)
        self.assertIn(f"intent:{open_intent.id}", legacy_nodes)
        self.assertIn(f"hypothesis:{self.hypothesis.id}", legacy_nodes)
        self.assertNotIn(f"hypothesis:{self.hypothesis.id}", nodes)

    def test_slime_meta_keeps_overlays_and_runtime_queryable(self) -> None:
        view = self.board.view_projection(self.project.id)
        meta = view["slime_meta"]

        self.assertEqual(meta["schema"], "slime_meta_v1")
        self.assertEqual(meta["facts"][self.output.id]["source_intent_id"], self.intent.id)
        self.assertEqual(meta["intents"][self.intent.id]["nutrient"], 0.8)
        self.assertEqual(meta["intents"][self.intent.id]["context"], {"branch": "fixture"})

        hypothesis_ids = {item["id"] for item in meta["overlays"]["hypotheses"]}
        self.assertIn(self.hypothesis.id, hypothesis_ids)
        self.assertTrue(
            any(
                {relation["source_fact_id"], relation["target_fact_id"]}
                == {self.parent.id, self.output.id}
                for relation in meta["overlays"]["fact_relations"]
            )
        )
        self.assertTrue(
            any(
                edge["fact_id"] == self.parent.id and edge["intent_id"] == self.intent.id
                for edge in meta["overlays"]["path_edges"]
            )
        )

        self.assertEqual(meta["runtime"]["active_leases"][0]["intent_id"], self.intent.id)
        self.assertTrue(
            any(item["evidence_ref"] == self.evidence_ref for item in meta["runtime"]["evidence"])
        )
        self.assertIn("worker_runs", meta["runtime"])
        self.assertIn("actions", meta["runtime"])
        self.assertIn("reason_state", meta["runtime"])
        self.assertIn("reason_lease", meta["runtime"])

    def test_events_are_cursor_paginated_in_creation_order(self) -> None:
        cursor = self.board.latest_event_id(self.project.id)
        self.board.add_event(self.project.id, "fixture.one", {"index": 1})
        self.board.add_event(self.project.id, "fixture.two", {"index": 2})
        self.board.add_event(self.project.id, "fixture.three", {"index": 3})

        first = self.client.get(f"/projects/{self.project.id}/events?after_id={cursor}&limit=2")

        self.assertEqual(first.status_code, 200)
        first_page = first.json()
        self.assertEqual([event["kind"] for event in first_page["events"]], ["fixture.one", "fixture.two"])
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["next_after_id"], first_page["events"][-1]["id"])

        second = self.client.get(
            f"/projects/{self.project.id}/events?after_id={first_page['next_after_id']}&limit=2"
        )

        self.assertEqual(second.status_code, 200)
        second_page = second.json()
        self.assertEqual([event["kind"] for event in second_page["events"]], ["fixture.three"])
        self.assertFalse(second_page["has_more"])

    def test_hints_are_persistent_visible_and_idempotent(self) -> None:
        response = self.client.post(
            f"/projects/{self.project.id}/hints",
            json={"content": "Prioritize the response marker branch.", "creator": "operator"},
        )

        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertTrue(created["created"])
        hint_id = created["hint"]["id"]

        duplicate = self.client.post(
            f"/projects/{self.project.id}/hints",
            json={"content": "  Prioritize   the response marker branch.  ", "creator": "operator"},
        )
        self.assertEqual(duplicate.status_code, 201)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(duplicate.json()["hint"]["id"], hint_id)

        view = self.client.get(f"/projects/{self.project.id}/view").json()
        self.assertEqual(view["summary"]["hint_count"], 1)
        self.assertEqual(view["hints"][0]["content"], "Prioritize the response marker branch.")
        self.assertIn(
            "hint.created",
            [event["kind"] for event in self.board.list_events(self.project.id)],
        )

    def test_view_and_events_return_404_for_unknown_project(self) -> None:
        self.assertEqual(self.client.get("/projects/project_missing/view").status_code, 404)
        self.assertEqual(self.client.get("/projects/project_missing/events").status_code, 404)

    def test_root_serves_the_offline_observability_ui(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Slime Cairn", response.text)
        self.assertIn("id=\"graph\"", response.text)
        self.assertIn("id=\"readable-view\"", response.text)
        self.assertIn("setInterval(() => refresh(false), 5000)", response.text)
        self.assertEqual(self.client.get("/static/index.html").status_code, 200)

    def test_bootstrap_disabled_project_keeps_origin_goal_as_initial_reason_signal(self) -> None:
        response = self.client.post(
            "/projects",
            json={
                "title": "reason-first",
                "origin": "https://fixture.local/",
                "goal": "derive the first exploration direction",
                "bootstrap_enabled": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        project_id = response.json()["project"]["id"]
        facts = self.board.list_facts(project_id)
        by_role = {fact.attributes["role"]: fact for fact in facts}

        self.assertEqual(set(by_role), {"origin", "goal"})
        self.assertFalse(by_role["origin"].attributes["seed"])
        self.assertFalse(by_role["goal"].attributes["seed"])
        self.assertEqual(self.board.list_intents(project_id), [])


if __name__ == "__main__":
    unittest.main()
