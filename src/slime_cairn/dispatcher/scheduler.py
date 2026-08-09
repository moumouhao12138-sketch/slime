from __future__ import annotations

from ..domain.branch_policy import SmaBranchPolicy
from ..domain.context import ContextBuilder
from ..domain.growth import SlimeGrowthLayer
from ..domain.models import Fact, IntentProposal, new_id, now
from ..domain.nutrients import NutrientEngine
from ..domain.validation import FactEvidenceGate, HypothesisGate, IntentGate
from ..domain.workspace import IsolatedWorkspace
from ..integrations.benchmark.client import BenchmarkClient, BenchmarkError, BenchmarkSettings
from ..integrations.benchmark.runtime import (
    BenchmarkProjectController,
    benchmark_metadata,
    extract_submission_candidates,
)
from ..server.blackboard import Blackboard
from dataclasses import asdict
from hashlib import sha256
import json
import time
from typing import Any
import yaml


class Scheduler:
    def __init__(
        self,
        board: Blackboard,
        project_id: str,
        mind=None,
        context_builder: ContextBuilder | None = None,
        global_audit_interval: int = 5,
        no_progress_audit_threshold: int = 2,
        workspace: IsolatedWorkspace | None = None,
        benchmark_client: BenchmarkClient | None = None,
    ) -> None:
        self.board = board
        self.project_id = project_id
        self.workspace = workspace
        self.nutrients = NutrientEngine()
        self.slime = SlimeGrowthLayer(self.nutrients)
        self._growth_policy = SmaBranchPolicy()
        self._growth_selection_count = 0
        self.mind = mind
        self.context_builder = context_builder or ContextBuilder()
        self.global_audit_interval = max(1, global_audit_interval)
        self.no_progress_audit_threshold = max(1, no_progress_audit_threshold)
        self.last_reason_report = None
        self.last_reason_kind = "incremental"
        self.last_reason_rejections: list[str] = []
        self.last_reason_completion: dict | None = None
        self.benchmark_client = benchmark_client

    def _benchmark_controller(self) -> BenchmarkProjectController:
        if self.benchmark_client is None:
            settings = BenchmarkSettings.from_env()
            self.benchmark_client = BenchmarkClient(settings)
        return BenchmarkProjectController(
            self.board,
            self.benchmark_client,
            self.benchmark_client.settings,
        )

    def _discard_report_if_project_not_running(
        self,
        report,
        mode: str,
        worker_name: str,
        owner_token: str = "",
        intent_id: str | None = None,
    ) -> bool:
        """Fence late reports after a project stop or lease ownership loss."""

        if report.status == "discarded" and report.stop_reason in {
            "project_not_running",
            "lease_not_owned",
        }:
            return True
        project = self.board.get_project(self.project_id)
        discard_reason = ""
        if project.status != "running":
            discard_reason = "project_not_running"
        elif owner_token:
            lease_owned = (
                self.board.owns_reason_lease(self.project_id, owner_token)
                if mode == "reason"
                else self.board.owns_intent_lease(
                    self.project_id,
                    intent_id or report.intent_id,
                    owner_token,
                )
            )
            if not lease_owned:
                discard_reason = "lease_not_owned"
        if not discard_reason:
            return False

        if discard_reason == "project_not_running":
            message = f"project is {project.status}; late {mode} report discarded"
        else:
            message = f"{mode} lease is no longer owned; late report discarded"
        report.status = "discarded"
        report.stop_reason = discard_reason
        if message not in report.errors:
            report.errors.append(message)
        self.board.fail_worker_run_if_running(
            report.pseudopod_id,
            message,
            stop_reason=(
                f"project_{project.status}_late_report"
                if discard_reason == "project_not_running"
                else "lease_not_owned_late_report"
            ),
        )
        self.board.add_event(
            self.project_id,
            "worker.late_report_discarded",
            {
                "worker_run_id": report.pseudopod_id,
                "intent_id": report.intent_id,
                "worker_name": worker_name,
                "mode": mode,
                "project_status": project.status,
                "discard_reason": discard_reason,
                "owner_token": owner_token,
            },
        )
        return True

    def _run_pseudopod(
        self,
        intent,
        capsule,
        mode: str,
        mind=None,
        worker_name: str = "",
        owner_token: str = "",
    ):
        active_mind = mind if mind is not None else self.mind
        direct_runner = getattr(active_mind, "run_cairn_task", None)
        pod_id = new_id("pod")
        self.board.start_worker_run(
            pod_id,
            self.project_id,
            intent.id,
            mode,
            asdict(capsule.manifest),
            now(),
            worker_name=worker_name,
            owner_token=owner_token,
        )
        try:
            if not callable(direct_runner):
                raise RuntimeError("Cairn worker must implement run_cairn_task")
            report = direct_runner(intent, capsule.facts, mode, capsule)
            # A project can be stopped while the native CLI is still returning.
            # Do not register artifacts or import its result after that transition.
            report.pseudopod_id = pod_id
            if self._discard_report_if_project_not_running(
                report,
                mode,
                worker_name,
                owner_token,
                intent.id,
            ):
                return report
            evidence_refs = set(report.evidence_refs)
            evidence_refs.update(
                ref
                for candidate in report.candidate_facts
                for ref in candidate.evidence_refs
            )
            evidence_refs.update(
                ref
                for candidate in report.candidate_hypotheses
                for ref in candidate.evidence_refs
            )
            for evidence_ref in sorted(evidence_refs):
                self.board.register_evidence(
                    self.project_id,
                    evidence_ref,
                    "native_agent_artifact",
                    metadata={
                        "worker_name": worker_name,
                        "intent_id": intent.id,
                        "mode": mode,
                    },
                )
            # Native runners create the report after the Dispatcher has leased
            # it. Keep a single worker-run identifier for SQLite lifecycle.
            self.board.complete_worker_run(report)
            return report
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            self.board.fail_worker_run(pod_id, error_message)
            self.board.add_event(
                self.project_id,
                "worker.unhandled_error",
                {"worker_id": pod_id, "intent_id": intent.id, "error": error_message},
            )
            raise

    def seed(self, target: str) -> None:
        proposal = IntentProposal(
            kind="bootstrap",
            objective="自由观察授权目标，建立第一批有证据的事实",
            target_entity=target,
            parent_fact_ids=[],
            expected_value=1.0,
            novelty=1.0,
            cost=0.1,
            risk=0.0,
            context={"phase": "bootstrap"},
        )
        self.board.add_intent(self.project_id, proposal, self.nutrients.score(proposal))

    def _attach_graph_snapshot(self, capsule, intent, mode: str) -> None:
        """Write the same minimal graph.yaml shape used by Cairn."""

        workspace = self.workspace
        if workspace is None:
            return
        project = self.board.get_project(self.project_id)
        facts = self.board.list_facts(self.project_id)
        hints = self.board.list_hints(self.project_id)
        intents = self.board.list_intents(self.project_id)
        fact_outputs: dict[str, list[str]] = {}
        fact_created_at: dict[str, float] = {}
        for fact in facts:
            fact_created_at[fact.id] = fact.created_at
            if fact.source_intent_id:
                fact_outputs.setdefault(fact.source_intent_id, []).append(fact.id)

        payload: dict[str, Any] = {
            "project": asdict(project),
            "facts": [{"id": item.id, "description": item.object} for item in facts],
        }
        payload["project"] = {
            "title": project.name,
            "origin": str(project.scope.get("origin") or project.target),
            "goal": project.goal,
            "bootstrap_enabled": bool(project.scope.get("bootstrap_enabled", True)),
        }
        if hints:
            payload["hints"] = [
                {
                    "content": item.content,
                    "creator": item.creator,
                    "created_at": item.created_at,
                }
                for item in hints
            ]
        if intents:
            payload["intents"] = [
                {
                    "from": list(item.parent_fact_ids),
                    "to": (fact_outputs.get(item.id) or [None])[0],
                    "description": item.objective,
                    "creator": str(item.provenance.get("native_agent") or "reason"),
                    "worker": item.owner,
                    "created_at": item.created_at,
                    "concluded_at": (
                        fact_created_at.get((fact_outputs.get(item.id) or [""])[0])
                    ),
                }
                for item in intents
            ]
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        digest = sha256(text.encode("utf-8")).hexdigest()
        relative = workspace.write_text(
            f"context/{mode}-{intent.id}-{digest[:12]}/graph.yaml",
            text,
        )
        capsule.manifest.graph_snapshot_ref = relative
        capsule.manifest.graph_snapshot_sha256 = digest
        capsule.manifest.graph_snapshot_counts = {
            "facts": len(facts),
            "hints": len(hints),
            "intents": len(intents),
        }
        self.board.add_event(
            self.project_id,
            "context.graph_snapshot_written",
            {
                "intent_id": intent.id,
                "mode": mode,
                "path": relative,
                "sha256": digest,
                "counts": dict(capsule.manifest.graph_snapshot_counts),
            },
        )

    @staticmethod
    def _recent_fact_ids(facts: list[Fact], reason_state: dict) -> list[str]:
        last_fact_time = float(reason_state.get("last_fact_time", 0.0))
        cursor_fact_ids = set(reason_state.get("last_fact_ids", []))
        return [
            fact.id
            for fact in facts
            if fact.created_at > last_fact_time
            or (fact.created_at == last_fact_time and fact.id not in cursor_fact_ids)
        ]

    def _enrich_proposal(
        self,
        proposal: IntentProposal,
        facts: list[Fact],
        open_intents: list,
        reason_kind: str,
        revived_fact_ids: list[str],
    ) -> tuple[IntentProposal, float]:
        return self.slime.decorate_intent(
            proposal,
            facts,
            open_intents,
            reason_kind=reason_kind,
            revived_fact_ids=revived_fact_ids,
        )

    def _materialize_branch_hypothesis(self, intent) -> None:
        """Persist the purpose of a Reason-created path as a Slime overlay.

        Cairn continues to receive only Facts and Intents in graph.yaml.  The
        overlay makes a path's claim, inherited evidence and next validation
        queryable by the UI and available to the Worker in context.json.
        """

        from ..domain.models import HypothesisCandidate

        slime_meta = intent.context.get("slime_meta", {})
        if not isinstance(slime_meta, dict):
            return
        source_facts = [
            fact
            for fact in self.board.list_facts(self.project_id)
            if fact.id in set(intent.parent_fact_ids)
        ]
        candidate = HypothesisCandidate(
            statement=intent.objective.strip(),
            supporting_fact_ids=list(intent.parent_fact_ids),
            evidence_refs=sorted(
                set(str(reference) for fact in source_facts for reference in fact.evidence_refs)
            ),
            confidence=max(0.0, min(1.0, float(intent.expected_value))),
            next_validation=str(slime_meta.get("next_validation") or intent.objective).strip(),
            source_intent_id=intent.id,
        )
        hypothesis, created = self.board.add_hypothesis(self.project_id, candidate)
        if created:
            self.board.add_event(
                self.project_id,
                "slime.branch_opened",
                {
                    "intent_id": intent.id,
                    "hypothesis_id": hypothesis.id,
                    "branch_root_id": slime_meta.get("branch_root_id") or intent.id,
                    "branch_depth": int(slime_meta.get("branch_depth", 0) or 0),
                    "predecessor_intent_ids": list(slime_meta.get("predecessor_intent_ids") or []),
                },
            )

    def _set_branch_state(self, intent_id: str, status: str, *, next_validation: str | None = None) -> None:
        for hypothesis in self.board.list_hypotheses_for_intent(self.project_id, intent_id):
            self.board.update_hypothesis_status(
                self.project_id,
                hypothesis.id,
                status,
                next_validation=next_validation,
            )

    def reason(
        self,
        force_global_audit: bool = False,
        worker_progress: bool = True,
        mind=None,
        worker_name: str = "reason",
        owner_token: str = "",
        reason_intent_id: str | None = None,
    ) -> int:
        if not self.board.is_project_running(self.project_id):
            self.last_reason_report = None
            self.last_reason_completion = None
            self.last_reason_kind = "skipped"
            self.last_reason_rejections = ["project_not_running"]
            return 0
        if owner_token and not self.board.owns_reason_lease(self.project_id, owner_token):
            self.last_reason_report = None
            self.last_reason_completion = None
            self.last_reason_kind = "skipped"
            self.last_reason_rejections = ["reason_lease_not_owned"]
            return 0
        reason_state = self.board.get_reason_state(self.project_id)
        facts = self.board.list_facts(self.project_id)
        hints = self.board.list_hints(self.project_id)
        hint_cursor = max(
            (hint.created_at for hint in hints),
            default=float(reason_state.get("last_hint_created_at", 0.0)),
        )
        recent_fact_ids = self._recent_fact_ids(facts, reason_state)
        revived_fact_ids = self.board.revive_related_facts(self.project_id, recent_fact_ids)
        self.board.refresh_fact_importance(self.project_id)
        facts = self.board.list_facts(self.project_id)

        reason_run = int(reason_state.get("incremental_runs", 0)) + 1
        projected_no_progress = (
            0 if worker_progress else int(reason_state.get("no_progress_streak", 0)) + 1
        )
        # Cairn triggers Reason from graph checkpoints. Global audit remains a
        # manual Slime diagnostic and never starts on an automatic interval.
        global_audit = force_global_audit
        audit_fact_ids = self.board.global_audit_candidates(self.project_id) if global_audit else []
        reason_kind = "global_audit" if global_audit else "incremental"
        self.last_reason_kind = reason_kind

        project = self.board.get_project(self.project_id)
        reason_intent = IntentProposal(
            kind="reason",
            objective=(
                "分批巡检黑板中的休眠事实和弱分支，重新判断跨分支关系与下一轮生长方向"
                if global_audit
                else "读取整体黑板变化，完成启明、总结、跨分支融合和下一轮生长判断"
            ),
            target_entity=project.target,
            # Reason reads the complete graph.yaml snapshot. Its transient
            # scheduling Intent therefore needs no artificial parent edges.
            parent_fact_ids=[],
            context={"ephemeral": True, "reason_kind": reason_kind},
        )
        # Reason is a transient task mode, not a persistent attack Intent.
        from ..domain.models import Intent
        transient = Intent(
            kind=reason_intent.kind,
            objective=reason_intent.objective,
            target_entity=reason_intent.target_entity,
            parent_fact_ids=reason_intent.parent_fact_ids,
            context=reason_intent.context,
            id=reason_intent_id or new_id("reason"),
            project_id=self.project_id,
            fingerprint=reason_intent.key,
        )
        context_state = {
            **reason_state,
            "global_audit": global_audit,
            "audit_fact_ids": audit_fact_ids,
        }
        all_intents = self.board.list_intents(self.project_id)
        capsule = self.context_builder.build(
            "reason",
            project,
            transient,
            facts,
            all_intents,
            context_state,
            hints=[asdict(hint) for hint in hints],
        )
        self._attach_graph_snapshot(capsule, transient, "reason")
        report = self._run_pseudopod(
            transient,
            capsule,
            "reason",
            mind=mind,
            worker_name=worker_name,
            owner_token=owner_token,
        )
        self.last_reason_report = report
        self.last_reason_completion = None
        if self._discard_report_if_project_not_running(
            report,
            "reason",
            worker_name,
            owner_token,
            transient.id,
        ):
            self.last_reason_rejections = [report.stop_reason]
            return 0
        if report.status == "failed":
            # A failed Reason run did not produce a trustworthy planning
            # decision.  Do not advance the reason_state cursor here; the
            # dispatcher will keep the same blackboard facts eligible for the
            # next Reason attempt instead of making the project look idle.
            self.last_reason_rejections = list(report.errors[-3:]) or [report.stop_reason]
            detail = "; ".join(self.last_reason_rejections) or report.stop_reason
            raise RuntimeError(f"reason worker failed: {detail}")
        if report.status == "rejected":
            # Cairn treats an accepted:false model result as a Worker-level
            # rejection, not as a valid no-op planning checkpoint. Let the
            # Dispatcher cool down this Worker and retry Reason elsewhere.
            self.last_reason_rejections = list(report.errors[-3:]) or [report.stop_reason]
            detail = "; ".join(self.last_reason_rejections) or report.stop_reason
            raise RuntimeError(f"reason worker rejected: {detail}")
        decorated = [
            self._enrich_proposal(
                proposal,
                facts,
                all_intents,
                reason_kind,
                capsule.manifest.revived_fact_ids,
            )
            for proposal in report.proposed_intents
        ]
        created = 0
        created_intents: list[dict[str, Any]] = []
        benchmark_submission: dict[str, Any] | None = None
        self.last_reason_rejections = []
        if report.completion is not None:
            if benchmark_metadata(project):
                benchmark_submission = self._benchmark_controller().submit_candidates(
                    self.project_id,
                    report.completion.submissions,
                    worker_name=worker_name,
                    completion_fact_ids=report.completion.fact_ids,
                    completion_description=report.completion.description,
                )
                self.last_reason_completion = benchmark_submission.get("completion")
                if self.last_reason_completion is None:
                    progress = benchmark_submission.get("progress") or {}
                    submission_fact_ids = list(benchmark_submission.get("submission_fact_ids") or [])
                    follow_up = IntentProposal(
                        kind="explore",
                        objective=(
                            "The Benchmark platform has not verified every required flag "
                            f"({int(progress.get('correct_flag_count') or 0)}/"
                            f"{int(progress.get('flag_count') or 0)}). Continue autonomous analysis, "
                            "use the verified/rejected submission Facts, find remaining exact flag values, "
                            "and avoid repeating prior candidates."
                        ),
                        target_entity=project.target,
                        parent_fact_ids=list(
                            dict.fromkeys([*report.completion.fact_ids, *submission_fact_ids])
                        ),
                        expected_value=0.9,
                        novelty=0.8,
                        cost=0.3,
                        risk=0.0,
                        context={"benchmark_follow_up": True, "progress": progress},
                    )
                    review = IntentGate(self.board, project).review(follow_up)
                    if review.accepted:
                        intent, is_new = self.board.add_intent(
                            self.project_id,
                            follow_up,
                            self.nutrients.score(follow_up),
                        )
                        created += int(is_new)
                        if is_new:
                            self._materialize_branch_hypothesis(intent)
                            created_intents.append(
                                {
                                    "id": intent.id,
                                    "objective": intent.objective,
                                    "parent_fact_ids": list(intent.parent_fact_ids),
                                    "target_entity": intent.target_entity,
                                    "nutrient": intent.nutrient,
                                }
                            )
                    else:
                        self.last_reason_rejections.append(review.reason)
            else:
                completion, _ = self.board.complete_project(
                    self.project_id,
                    report.completion.fact_ids,
                    report.completion.description,
                    worker_name,
                )
                self.last_reason_completion = completion
        else:
            intent_gate = IntentGate(self.board, project)
            for proposal, nutrient in decorated:
                review = intent_gate.review(proposal)
                if not review.accepted:
                    self.last_reason_rejections.append(review.reason)
                    continue
                intent, is_new = self.board.add_intent(self.project_id, proposal, nutrient)
                created += int(is_new)
                if is_new:
                    self._materialize_branch_hypothesis(intent)
                    created_intents.append(
                        {
                            "id": intent.id,
                            "objective": intent.objective,
                            "parent_fact_ids": list(intent.parent_fact_ids),
                            "target_entity": intent.target_entity,
                            "nutrient": intent.nutrient,
                        }
                    )
            if created_intents:
                self.board.add_event(
                    self.project_id,
                    "reason.intent_batch_created",
                    {
                        "reason_intent_id": transient.id,
                        "worker_name": worker_name,
                        "reason_kind": reason_kind,
                        "fanout_count": len(created_intents),
                        "parallel": len(created_intents) > 1,
                        "intent_ids": [item["id"] for item in created_intents],
                        "intents": created_intents,
                    },
                )
        self.board.record_worker_validation(
            report.pseudopod_id,
            {
                "accepted_intents": created,
                "rejected_intents": list(self.last_reason_rejections),
                "completion": self.last_reason_completion,
                "benchmark_submission": benchmark_submission,
                "slime_growth": [
                    {
                        "objective": proposal.objective,
                        "nutrient": nutrient,
                        "meta": proposal.context.get("slime_meta", {}),
                    }
                    for proposal, nutrient in decorated
                ],
            },
        )
        self.board.record_context_usage(
            self.project_id,
            capsule.manifest.included_fact_ids,
            capsule.manifest.omitted_fact_ids,
        )
        last_fact_time = max((fact.created_at for fact in facts), default=float(reason_state.get("last_fact_time", 0.0)))
        no_progress_streak = (
            0
            if worker_progress or created or self.last_reason_completion is not None
            else projected_no_progress
        )
        global_summary = json.dumps(
            {
                "reason_kind": reason_kind,
                "branches": [asdict(branch) for branch in capsule.branches],
                "last_reason_activity": report.activity_summary,
                "proposed_intents": len(report.proposed_intents),
                "completion": self.last_reason_completion,
                "revived_fact_ids": revived_fact_ids,
                "audit_fact_ids": audit_fact_ids,
            },
            ensure_ascii=False,
        )
        self.board.save_reason_state(
            self.project_id,
            last_fact_time,
            self.board.latest_event_id(self.project_id),
            global_summary,
            incremental_runs=reason_run,
            last_global_audit_event_id=(
                self.board.latest_event_id(self.project_id)
                if global_audit
                else int(reason_state.get("last_global_audit_event_id", 0))
            ),
            last_global_audit_at=(now() if global_audit else float(reason_state.get("last_global_audit_at", 0.0))),
            no_progress_streak=no_progress_streak,
            last_hint_created_at=hint_cursor,
        )
        return created

    def run_global_audit(self) -> int:
        return self.reason(force_global_audit=True, worker_progress=False)

    def tick(self) -> dict | None:
        worker_id = new_id("worker")
        candidates = self.board.list_runnable_intents(self.project_id)
        selection = self._growth_policy.select(
            candidates,
            project_id=self.project_id,
            selection_index=self._growth_selection_count,
            all_intents=self.board.list_intents(self.project_id),
        )
        intent = None
        for candidate in selection.ordered:
            intent = self.board.claim_intent(self.project_id, candidate.id, worker_id)
            if intent is not None:
                break
        if intent is None:
            return None
        self._growth_selection_count += 1
        details = dict(selection.details)
        policy_selected_intent_id = details.get("selected_intent_id")
        details["policy_selected_intent_id"] = policy_selected_intent_id
        details["selected_intent_id"] = intent.id
        details["fallback"] = intent.id != policy_selected_intent_id
        self.board.add_event(
            self.project_id,
            "growth.branch_selected",
            {
                "dispatcher_id": "legacy-synchronous",
                "intent_id": intent.id,
                "worker_name": "legacy-synchronous",
                "mode": "bootstrap" if intent.kind == "bootstrap" else "explore",
                **details,
            },
        )
        return self.process_claimed_intent(
            intent,
            mind=self.mind,
            worker_name="legacy-synchronous",
            owner_token=worker_id,
            run_reason=True,
        )

    def process_claimed_intent(
        self,
        intent,
        mind=None,
        worker_name: str = "explore",
        owner_token: str = "",
        run_reason: bool = False,
        retry_failed: bool = False,
    ) -> dict:
        """Execute an already leased Intent; AsyncDispatcher owns claiming and concurrency."""

        self._set_branch_state(intent.id, "testing")
        facts = self.board.list_facts(self.project_id)
        hints = self.board.list_hints(self.project_id)
        mode = "bootstrap" if intent.kind == "bootstrap" else "explore"
        capsule = self.context_builder.build(
            mode,
            self.board.get_project(self.project_id),
            intent,
            facts,
            self.board.list_intents(self.project_id),
            self.board.get_reason_state(self.project_id),
            hints=[asdict(hint) for hint in hints],
        )
        self._attach_graph_snapshot(capsule, intent, mode)
        report = self._run_pseudopod(
            intent,
            capsule,
            mode,
            mind=mind,
            worker_name=worker_name,
            owner_token=owner_token,
        )
        if self._discard_report_if_project_not_running(
            report,
            mode,
            worker_name,
            owner_token,
            intent.id,
        ):
            return {
                "intent": intent,
                "report": report,
                "accepted_facts": [],
                "rejected_facts": [],
                "accepted_hypotheses": [],
                "rejected_hypotheses": [],
                "reward": 0.0,
                "new_intents": 0,
                "reason_report": None,
                "reason_kind": "deferred",
                "reason_rejections": [],
                "lease_retained": False,
                "discarded": True,
            }
        self.board.record_context_usage(
            self.project_id,
            capsule.manifest.included_fact_ids,
            capsule.manifest.omitted_fact_ids,
        )

        accepted = []
        accepted_by_candidate_index: dict[int, Fact] = {}
        rejected_facts = []
        duplicates = 0
        project = self.board.get_project(self.project_id)
        fact_gate = FactEvidenceGate(self.board, project)
        for candidate_index, candidate in enumerate(report.candidate_facts):
            review = fact_gate.review(candidate)
            if not review.accepted:
                rejected_facts.append({"candidate": candidate, "reason": review.reason})
                continue
            fact, is_new = self.board.add_fact(self.project_id, candidate)
            accepted.append(fact)
            accepted_by_candidate_index[candidate_index] = fact
            duplicates += int(not is_new)

        accepted_hypotheses = []
        rejected_hypotheses = []
        hypothesis_gate = HypothesisGate(self.board, self.project_id)
        for candidate in report.candidate_hypotheses:
            review = hypothesis_gate.review(candidate)
            if not review.accepted:
                rejected_hypotheses.append({"candidate": candidate, "reason": review.reason})
                continue
            hypothesis, _ = self.board.add_hypothesis(self.project_id, candidate)
            accepted_hypotheses.append(hypothesis)

        direct_completion = None
        completion_rejection = ""
        benchmark_submission: dict[str, Any] | None = None
        if benchmark_metadata(project):
            explicit_candidates: list[str] = []
            automatic_candidates: list[str] = []
            candidate_fact_ids: list[str] = []
            for candidate_index, candidate in enumerate(report.candidate_facts):
                fact = accepted_by_candidate_index.get(candidate_index)
                if fact is None:
                    continue
                automatic_candidates.extend(extract_submission_candidates(candidate.object))
                raw_candidates = candidate.attributes.get("benchmark_submissions")
                if not isinstance(raw_candidates, list):
                    raw_candidates = []
                explicit_candidates.extend(
                    str(value).strip()
                    for value in raw_candidates
                    if isinstance(value, str) and value.strip()
                )
                candidate_fact_ids.append(fact.id)
            submission_candidates = list(
                dict.fromkeys([*explicit_candidates, *automatic_candidates])
            )
            if submission_candidates:
                try:
                    benchmark_submission = self._benchmark_controller().submit_candidates(
                        self.project_id,
                        submission_candidates,
                        worker_name=worker_name,
                        completion_fact_ids=candidate_fact_ids,
                        completion_description=(
                            "Worker Fact automatically yielded Benchmark candidates"
                            if automatic_candidates
                            else "Explore explicitly reported Benchmark candidates"
                        ),
                    )
                except (BenchmarkError, ValueError) as exc:
                    # Facts are already durable. Raising keeps the Intent
                    # retryable while avoiding a silent candidate drop.
                    raise RuntimeError(
                        f"Benchmark candidate submission failed: {exc}"
                    ) from exc
        if report.completion is not None:
            if mode != "bootstrap":
                completion_rejection = f"{mode} completion is not handled by an Intent worker"
            else:
                selected_indexes = list(report.completion.candidate_fact_indexes)
                selected_facts = [
                    accepted_by_candidate_index[index]
                    for index in selected_indexes
                    if index in accepted_by_candidate_index
                ]
                missing_indexes = [
                    index for index in selected_indexes if index not in accepted_by_candidate_index
                ]
                if missing_indexes:
                    completion_rejection = (
                        "Bootstrap completion selected Fact candidates that did not pass validation: "
                        + ", ".join(str(index) for index in missing_indexes)
                    )
                elif not selected_facts:
                    completion_rejection = "Bootstrap completion did not resolve to a validated Fact"
                else:
                    try:
                        direct_completion, _ = self.board.complete_project(
                            self.project_id,
                            [fact.id for fact in selected_facts],
                            report.completion.description,
                            worker_name,
                        )
                    except ValueError as exc:
                        completion_rejection = f"Bootstrap completion rejected: {exc}"
            if completion_rejection:
                self.board.add_event(
                    self.project_id,
                    "bootstrap.direct_completion_rejected",
                    {
                        "intent_id": intent.id,
                        "worker_name": worker_name,
                        "reason": completion_rejection,
                    },
                )

        if accepted or accepted_hypotheses:
            status = "completed"
        elif report.status in {"failed", "rejected"}:
            status = report.status
        elif report.candidate_facts or report.candidate_hypotheses:
            status = "dormant"
        else:
            status = report.status
        lease_retained = retry_failed and status in {"failed", "rejected"}
        if status in {"failed", "rejected"} and not lease_retained:
            # The legacy synchronous Scheduler owns its lease directly.  It
            # must still use the durable retry queue instead of converting a
            # transient Worker report into a terminal Intent.
            self.board.release_intent(
                self.project_id,
                intent.id,
                owner_token or intent.owner or "",
                "; ".join(report.errors) or report.stop_reason or "worker report failed",
            )
        elif not lease_retained:
            self.board.finish_intent(
                self.project_id,
                intent.id,
                status,
                owner=owner_token or intent.owner,
            )
        if accepted:
            self._set_branch_state(
                intent.id,
                "supported",
                next_validation="Reason should decide whether to continue this evidence-backed path.",
            )
        elif status in {"failed", "rejected"}:
            self._set_branch_state(
                intent.id,
                "blocked",
                next_validation="Retry only after the recorded Worker failure has been resolved.",
            )
        else:
            self._set_branch_state(
                intent.id,
                "inconclusive",
                next_validation="Reason should either derive a narrower validation step or retire this path.",
            )
        reward = self.nutrients.reward(accepted, duplicates)
        self.board.update_intent_strength(self.project_id, intent.id, reward)
        self.board.decay_paths(self.project_id)
        new_intents = 0
        reason_report = None
        reason_kind = "deferred"
        reason_rejections: list[str] = []
        if run_reason:
            try:
                new_intents = self.reason(worker_progress=bool(accepted or accepted_hypotheses))
                reason_report = self.last_reason_report
                reason_kind = self.last_reason_kind
                reason_rejections = list(self.last_reason_rejections)
            except Exception as exc:
                reason_report = self.last_reason_report
                reason_kind = self.last_reason_kind
                reason_rejections = [f"{type(exc).__name__}: {exc}"]
                self.board.add_event(
                    self.project_id,
                    "reason.failed_after_worker",
                    {
                        "intent_id": intent.id,
                        "worker_name": worker_name,
                        "error": reason_rejections[0][:1000],
                    },
                )

        self.board.record_worker_validation(
            report.pseudopod_id,
            {
                "accepted_fact_ids": [fact.id for fact in accepted],
                "rejected_facts": [item["reason"] for item in rejected_facts],
                "accepted_hypothesis_ids": [item.id for item in accepted_hypotheses],
                "rejected_hypotheses": [item["reason"] for item in rejected_hypotheses],
                "direct_completion": direct_completion,
                "completion_rejection": completion_rejection,
                "benchmark_submission": benchmark_submission,
            },
        )

        return {
            "intent": intent,
            "report": report,
            "accepted_facts": accepted,
            "rejected_facts": rejected_facts,
            "accepted_hypotheses": accepted_hypotheses,
            "rejected_hypotheses": rejected_hypotheses,
            "reward": reward,
            "new_intents": new_intents,
            "reason_report": reason_report,
            "reason_kind": reason_kind,
            "reason_rejections": reason_rejections,
            "completion": direct_completion,
            "completion_rejection": completion_rejection,
            "benchmark_submission": benchmark_submission,
            "lease_retained": lease_retained,
        }

    def run_until_idle(self, max_ticks: int = 20) -> list[dict]:
        history = []
        for _ in range(max_ticks):
            result = self.tick()
            if result is None:
                break
            history.append(result)
            if any(fact.predicate == "has_verified_auth_surface" for fact in self.board.list_facts(self.project_id)):
                break
        return history
