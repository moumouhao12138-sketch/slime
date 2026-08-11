from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from slime_cairn.server.blackboard import Blackboard
from slime_cairn.domain.models import FactCandidate, IntentProposal, PseudopodReport
from slime_cairn.dispatcher.scheduler import Scheduler


class StopBeforeReportMind:
    def __init__(self, board: Blackboard, project_id: str, report: PseudopodReport) -> None:
        self.board = board
        self.project_id = project_id
        self.report = report

    def run_cairn_task(self, intent, facts, mode, capsule):
        self.board.set_project_status(self.project_id, "stopped")
        return self.report


class HardStopBlackboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.target = "fixture.local"
        self.project = self.board.create_project(
            "hard-stop",
            self.target,
            "preserve durable state while stopping live work",
            {"targets": [self.target]},
        )

    def tearDown(self) -> None:
        self.board.close()
        self.temporary.cleanup()

    def _claim_bootstrap_intent(self, owner: str = "owner"):
        proposal = IntentProposal(
            kind="bootstrap",
            objective="collect an evidence-backed observation",
            target_entity=self.target,
            parent_fact_ids=[],
        )
        intent, _ = self.board.add_intent(self.project.id, proposal, 1.0)
        claimed = self.board.claim_next_intent(self.project.id, owner)
        self.assertIsNotNone(claimed)
        return intent, claimed

    @staticmethod
    def _report(intent_id: str, mode: str, candidate_facts=None, proposed_intents=None) -> PseudopodReport:
        return PseudopodReport(
            pseudopod_id="ignored",
            intent_id=intent_id,
            mode=mode,
            status="completed",
            candidate_facts=list(candidate_facts or []),
            candidate_hypotheses=[],
            evidence_refs=[],
            proposed_intents=list(proposed_intents or []),
            tool_calls=0,
            progress_score=1.0,
            stop_reason="fixture",
        )

    def test_stop_releases_leases_and_prevents_completed_worker_run_overwrite(self):
        intent, _ = self._claim_bootstrap_intent()
        self.board.start_worker_run(
            "intent-run",
            self.project.id,
            intent.id,
            "bootstrap",
            {},
            1.0,
            worker_name="fixture-worker",
            owner_token="owner",
        )
        self.board.start_worker_run(
            "reason-run",
            self.project.id,
            "reason_fixture",
            "reason",
            {},
            1.0,
            worker_name="fixture-reason",
            owner_token="reason-owner",
        )

        stopped = self.board.set_project_status(self.project.id, "stopped")

        self.assertEqual(stopped.status, "stopped")
        released = self.board.get_intent(self.project.id, intent.id)
        self.assertEqual(released.status, "pending")
        self.assertIsNone(released.owner)
        self.assertIsNone(released.lease_expires_at)
        self.assertIsNone(released.last_heartbeat_at)
        self.assertEqual(released.failure_streak, 0)
        self.assertEqual(released.retry_not_before, 0.0)
        runs = {run["id"]: run for run in self.board.list_worker_runs(self.project.id)}
        self.assertEqual(runs["intent-run"]["status"], "failed")
        self.assertEqual(runs["reason-run"]["status"], "failed")
        self.assertEqual(runs["intent-run"]["stop_reason"], "project_stopped")

        late_completion = self._report(intent.id, "bootstrap")
        late_completion.pseudopod_id = "intent-run"
        self.board.complete_worker_run(late_completion)
        after_late_completion = {
            run["id"]: run for run in self.board.list_worker_runs(self.project.id)
        }
        self.assertEqual(after_late_completion["intent-run"]["status"], "failed")
        self.assertEqual(after_late_completion["intent-run"]["stop_reason"], "project_stopped")

    def test_latest_worker_run_error_can_receive_dispatcher_category(self):
        intent, _ = self._claim_bootstrap_intent("error-owner")
        self.board.start_worker_run(
            "error-run",
            self.project.id,
            intent.id,
            "bootstrap",
            {},
            1.0,
            worker_name="fixture-worker",
            owner_token="error-owner",
        )
        self.board.fail_worker_run("error-run", "ModelInvocationError: provider failure")

        updated = self.board.annotate_latest_worker_run_error(
            self.project.id,
            intent.id,
            "[model_policy_filter] ModelInvocationError: provider failure",
        )

        self.assertTrue(updated)
        run = self.board.list_worker_runs(self.project.id)[0]
        self.assertEqual(
            run["errors"][-1],
            "[model_policy_filter] ModelInvocationError: provider failure",
        )

    def test_late_worker_report_is_discarded_without_fact_or_evidence_import(self):
        _, intent = self._claim_bootstrap_intent("late-worker-owner")
        late_fact = FactCandidate(
            self.target,
            "late_result",
            "must_not_be_imported",
            0.9,
            ["evidence://late-worker"],
        )
        report = self._report(intent.id, "bootstrap", candidate_facts=[late_fact])
        mind = StopBeforeReportMind(self.board, self.project.id, report)
        scheduler = Scheduler(self.board, self.project.id, mind=mind)

        outcome = scheduler.process_claimed_intent(
            intent,
            mind=mind,
            worker_name="fixture-worker",
            owner_token="late-worker-owner",
        )

        self.assertTrue(outcome["discarded"])
        self.assertEqual(outcome["report"].status, "discarded")
        self.assertFalse(self.board.evidence_exists(self.project.id, "evidence://late-worker"))
        self.assertFalse(any(fact.predicate == "late_result" for fact in self.board.list_facts(self.project.id)))
        run = self.board.list_worker_runs(self.project.id)[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stop_reason"], "project_stopped")

    def test_late_reason_report_is_discarded_without_creating_intent(self):
        proposal = IntentProposal(
            kind="explore",
            objective="must not be created after stop",
            target_entity=self.target,
            parent_fact_ids=[],
        )
        report = self._report("reason-late", "reason", proposed_intents=[proposal])
        mind = StopBeforeReportMind(self.board, self.project.id, report)
        scheduler = Scheduler(self.board, self.project.id, mind=mind)
        lease = self.board.claim_reason_lease(
            self.project.id,
            "reason-owner",
            "fixture-reason",
            "fixture",
            60,
            "reason-late",
        )
        self.assertIsNotNone(lease)

        created = scheduler.reason(
            mind=mind,
            worker_name="fixture-reason",
            owner_token="reason-owner",
            reason_intent_id="reason-late",
        )

        self.assertEqual(created, 0)
        self.assertEqual(self.board.get_project(self.project.id).status, "stopped")
        self.assertEqual(self.board.list_intents(self.project.id), [])
        run = self.board.list_worker_runs(self.project.id)[0]
        self.assertEqual(run["mode"], "reason")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stop_reason"], "project_stopped")


if __name__ == "__main__":
    unittest.main()
