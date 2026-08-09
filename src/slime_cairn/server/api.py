from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..domain.models import FactCandidate, HypothesisCandidate, IntentProposal
from ..domain.nutrients import NutrientEngine
from ..domain.seeding import seed_project_context_facts
from ..domain.validation import FactEvidenceGate, HypothesisGate, IntentGate
from ..integrations.benchmark.client import BenchmarkClient, BenchmarkError, BenchmarkSettings
from ..integrations.benchmark.runtime import BenchmarkProjectController
from .blackboard import Blackboard


STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="Slime Cairn", version=__version__)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
board = Blackboard(os.environ.get("SLIME_CAIRN_DB", "slime-cairn.db"))
nutrients = NutrientEngine()


class ProjectInput(BaseModel):
    # Slime native shape: {"name", "target", "goal"}.
    # Cairn-style shape: {"title", "origin", "goal", "bootstrap_enabled"}.
    # Keeping both makes the project creation step feel like Cairn while
    # preserving older scripts.
    name: str | None = None
    target: str | None = None
    goal: str
    allowed_targets: list[str] = Field(default_factory=list)
    title: str | None = None
    origin: str | None = None
    bootstrap_enabled: bool = True
    hints: list[dict] = Field(default_factory=list)


class HintInput(BaseModel):
    content: str
    creator: str = "human"


class FactInput(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str]
    attributes: dict = Field(default_factory=dict)


class IntentInput(BaseModel):
    kind: str
    objective: str
    target_entity: str
    parent_fact_ids: list[str] = Field(default_factory=list)
    expected_value: float = Field(default=0.5, ge=0, le=1)
    novelty: float = Field(default=0.5, ge=0, le=1)
    cost: float = Field(default=0.2, ge=0, le=1)
    risk: float = Field(default=0, ge=0, le=1)
    context: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)


class EvidenceInput(BaseModel):
    evidence_ref: str
    kind: str = "external_authorized_input"
    metadata: dict = Field(default_factory=dict)


class HypothesisInput(BaseModel):
    statement: str
    supporting_fact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    next_validation: str = ""


class ProjectStatusInput(BaseModel):
    status: Literal["running", "stopped"]


class BulkProjectDeleteInput(BaseModel):
    project_ids: list[str] = Field(min_length=1, max_length=200)


class CompletionInput(BaseModel):
    fact_ids: list[str] = Field(min_length=1)
    description: str
    worker_name: str = "human"


class ReopenInput(BaseModel):
    description: str
    creator: str = "human"


class BenchmarkHintInput(BaseModel):
    confirm_score_penalty: bool = False


class BenchmarkSubmitInput(BaseModel):
    flag: str = Field(min_length=1, max_length=4096)


class BenchmarkAutomationInput(BaseModel):
    parallelism: int = Field(default=3, ge=1, le=3)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _benchmark_settings() -> BenchmarkSettings:
    try:
        return BenchmarkSettings.from_env()
    except ValueError as exc:
        raise HTTPException(503, {"code": "benchmark_config_error", "message": str(exc), "detail": {}}) from exc


def _benchmark_controller() -> tuple[BenchmarkProjectController, BenchmarkClient]:
    settings = _benchmark_settings()
    if not settings.configured:
        raise HTTPException(
            503,
            {
                "code": "benchmark_not_configured",
                "message": "BENCHMARK_BASE_URL and BENCHMARK_TOKEN are required",
                "detail": {},
            },
        )
    client = BenchmarkClient(settings)
    return BenchmarkProjectController(board, client, settings), client


def _raise_benchmark_error(exc: BenchmarkError) -> None:
    raise HTTPException(exc.status_code, exc.as_dict()) from exc


@app.get("/benchmark/status")
def benchmark_status() -> dict:
    settings = _benchmark_settings()
    status = settings.public_status()
    status["automation"] = board.get_benchmark_automation(settings.task_key)
    return status


@app.get("/benchmark/automation")
def benchmark_automation_status() -> dict:
    settings = _benchmark_settings()
    return board.get_benchmark_automation(settings.task_key)


@app.post("/benchmark/automation/start")
def benchmark_automation_start(payload: BenchmarkAutomationInput) -> dict:
    settings = _benchmark_settings()
    if not settings.configured:
        raise HTTPException(
            503,
            {
                "code": "benchmark_not_configured",
                "message": "BENCHMARK_BASE_URL and BENCHMARK_TOKEN are required",
                "detail": {},
            },
        )
    return board.configure_benchmark_automation(
        settings.task_key,
        enabled=True,
        parallelism=payload.parallelism,
    )


@app.post("/benchmark/automation/stop")
def benchmark_automation_stop() -> dict:
    settings = _benchmark_settings()
    if not settings.configured:
        raise HTTPException(
            503,
            {
                "code": "benchmark_not_configured",
                "message": "BENCHMARK_BASE_URL and BENCHMARK_TOKEN are required",
                "detail": {},
            },
        )
    current = board.get_benchmark_automation(settings.task_key)
    return board.configure_benchmark_automation(
        settings.task_key,
        enabled=False,
        parallelism=current["parallelism"],
    )


@app.get("/benchmark/challenges")
def benchmark_challenges() -> dict:
    controller, client = _benchmark_controller()
    try:
        return {"challenges": controller.list_challenges()}
    except BenchmarkError as exc:
        _raise_benchmark_error(exc)
    finally:
        client.close_client()


@app.post("/benchmark/challenges/{unique_code}/start")
def benchmark_start(unique_code: str) -> dict:
    controller, client = _benchmark_controller()
    try:
        return controller.start(unique_code)
    except BenchmarkError as exc:
        _raise_benchmark_error(exc)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        client.close_client()


@app.post("/benchmark/challenges/{unique_code}/hint")
def benchmark_hint(unique_code: str, payload: BenchmarkHintInput) -> dict:
    controller, client = _benchmark_controller()
    try:
        return controller.hint(
            unique_code,
            confirm_score_penalty=payload.confirm_score_penalty,
        )
    except BenchmarkError as exc:
        _raise_benchmark_error(exc)
    finally:
        client.close_client()


@app.post("/benchmark/challenges/{unique_code}/submit")
def benchmark_submit(unique_code: str, payload: BenchmarkSubmitInput) -> dict:
    controller, client = _benchmark_controller()
    try:
        project = controller.find_project(unique_code)
        if project is None:
            raise BenchmarkError(
                409,
                "challenge_not_started",
                "Start the challenge before submitting a candidate flag",
            )
        return controller.submit_candidates(
            project.id,
            [payload.flag],
            worker_name="benchmark-ui",
            completion_description="Manual candidate accepted and Benchmark platform verified completion",
        )
    except BenchmarkError as exc:
        _raise_benchmark_error(exc)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        client.close_client()


@app.post("/benchmark/challenges/{unique_code}/close")
def benchmark_close(unique_code: str) -> dict:
    controller, client = _benchmark_controller()
    try:
        return controller.close(unique_code)
    except BenchmarkError as exc:
        _raise_benchmark_error(exc)
    finally:
        client.close_client()


@app.post("/projects")
def create_project(payload: ProjectInput) -> dict:
    name = _first_text(payload.name, payload.title) or "slime-project"
    target = _first_text(payload.target, payload.origin)
    if not target:
        raise HTTPException(422, "project target/origin is required")
    goal = payload.goal.strip()
    if not goal:
        raise HTTPException(422, "project goal is required")
    allowed = payload.allowed_targets or [target]
    scope = {
        "targets": allowed,
        "origin": payload.origin or target,
        "bootstrap_enabled": payload.bootstrap_enabled,
        "hints": payload.hints,
    }
    project = board.create_project(name, target, goal, scope)
    board.ensure_scope_hints(project.id)
    seed_project_context_facts(board, project)
    if payload.bootstrap_enabled:
        bootstrap = IntentProposal(
            kind="bootstrap",
            objective="Freely observe the target, gather first evidence-backed facts, and try to advance directly toward the goal.",
            target_entity=project.target,
            parent_fact_ids=[],
            expected_value=1.0,
            novelty=1.0,
            cost=0.1,
            risk=0.0,
            context={"phase": "bootstrap"},
        )
        board.add_intent(project.id, bootstrap, nutrients.score(bootstrap))
    return board.snapshot(project.id)


def _first_text(*values: str | None) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _writable_project(project_id: str):
    """Return a project that has not entered the asynchronous delete drain."""

    try:
        project = board.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    if project.status == "deleting":
        raise HTTPException(409, "project deletion is in progress")
    return project


@app.get("/projects")
def list_projects() -> dict:
    return {"projects": [asdict(project) for project in board.list_projects()]}


@app.post("/projects/bulk-delete", status_code=202)
def bulk_delete_projects(payload: BulkProjectDeleteInput) -> dict:
    """Request cleanup for several projects without aborting on stale IDs."""

    project_ids = list(dict.fromkeys(payload.project_ids))
    accepted: list[str] = []
    already_deleting: list[str] = []
    not_found: list[str] = []
    results: list[dict] = []
    for project_id in project_ids:
        try:
            project, newly_accepted = board.request_project_deletion(project_id)
        except KeyError:
            not_found.append(project_id)
            results.append({"project_id": project_id, "status": "not_found"})
            continue

        status = "accepted" if newly_accepted else "already_deleting"
        (accepted if newly_accepted else already_deleting).append(project_id)
        results.append(
            {
                "project_id": project_id,
                "status": status,
                "project": asdict(project),
            }
        )

    return {
        "requested": len(project_ids),
        "accepted": accepted,
        "already_deleting": already_deleting,
        "not_found": not_found,
        "results": results,
    }


@app.delete("/projects/{project_id}", status_code=202)
def delete_project(project_id: str) -> dict:
    """Request asynchronous cleanup of a project and its persistent runtime."""

    try:
        project, accepted = board.request_project_deletion(project_id)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    return {"accepted": accepted, "project": asdict(project)}


@app.get("/projects/{project_id}")
def get_project(project_id: str) -> dict:
    try:
        board.ensure_scope_hints(project_id)
        return board.snapshot(project_id)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc


def _intent_retry_fields(intent: object, observed_at: float) -> dict:
    """Normalize retry metadata for API consumers while migrations roll out."""

    if isinstance(intent, dict):
        retry_not_before_value = intent.get("retry_not_before", 0.0)
        failure_streak_value = intent.get("failure_streak", 0)
    else:
        retry_not_before_value = getattr(intent, "retry_not_before", 0.0)
        failure_streak_value = getattr(intent, "failure_streak", 0)
    retry_not_before = float(retry_not_before_value or 0.0)
    retry_after_seconds = max(0.0, retry_not_before - observed_at)
    return {
        "failure_streak": int(failure_streak_value or 0),
        "retry_not_before": retry_not_before or None,
        "retry_after_seconds": retry_after_seconds,
        "retry_waiting": retry_after_seconds > 0,
    }


def _decorate_retry_view(view: dict, observed_at: float) -> None:
    """Add retry timing to snapshot and graph records without changing storage."""

    retry_by_intent_id: dict[str, dict] = {}
    for intent in view.get("intents", []):
        retry_fields = _intent_retry_fields(intent, observed_at)
        intent.update(retry_fields)
        retry_by_intent_id[str(intent["id"])] = retry_fields

    for node in view.get("graph", {}).get("nodes", []):
        if node.get("kind") != "intent":
            continue
        retry_fields = retry_by_intent_id.get(str(node.get("entity_id")))
        if retry_fields is not None:
            node.update(retry_fields)

    waiting = [fields for fields in retry_by_intent_id.values() if fields["retry_waiting"]]
    summary = view.setdefault("summary", {})
    summary["retry_waiting_count"] = len(waiting)
    summary["next_retry_not_before"] = (
        min(fields["retry_not_before"] for fields in waiting) if waiting else None
    )


def _readable_projection(view: dict) -> dict:
    """Group Blackboard records into a compact result-first branch view."""

    hidden_placeholders = {
        "...",
        "confirmed key objective results",
        "why those results prove goal",
        "confirmed objective facts so far",
        "latest confirmed incremental facts",
        "independent high-value exploration direction",
    }
    visible_facts = [
        fact
        for fact in view.get("facts", [])
        if fact.get("memory_state") != "dormant"
        and str(fact.get("object", "")).strip().casefold() not in hidden_placeholders
    ]
    facts_by_id = {str(fact["id"]): fact for fact in visible_facts}
    facts_by_intent: dict[str, list[dict]] = {}
    for fact in visible_facts:
        source_intent_id = str(fact.get("source_intent_id") or "").strip()
        if source_intent_id:
            facts_by_intent.setdefault(source_intent_id, []).append(fact)
    for facts in facts_by_intent.values():
        facts.sort(key=lambda item: (float(item.get("created_at") or 0), str(item.get("id"))))

    latest_run_by_intent: dict[str, dict] = {}
    for run in sorted(
        view.get("worker_runs", []),
        key=lambda item: float(item.get("finished_at") or item.get("started_at") or 0),
        reverse=True,
    ):
        intent_id = str(run.get("intent_id") or "").strip()
        if intent_id and intent_id not in latest_run_by_intent:
            latest_run_by_intent[intent_id] = run

    project_status = str((view.get("project") or {}).get("status") or "")
    branches = []
    for intent in view.get("intents", []):
        intent_id = str(intent["id"])
        facts = facts_by_intent.get(intent_id, [])
        status = str(intent.get("status") or "pending")
        if project_status == "running" and status in {"running", "pending"}:
            group = "active"
        elif facts:
            group = "result"
        else:
            group = "attempted"
        latest_run = latest_run_by_intent.get(intent_id) or {}
        errors = latest_run.get("errors") or []
        branches.append(
            {
                "id": intent_id,
                "kind": intent.get("kind"),
                "objective": intent.get("objective"),
                "status": status,
                "attempts": int(intent.get("attempts") or 0),
                "created_at": intent.get("created_at"),
                "parent_fact_ids": list(intent.get("parent_fact_ids") or []),
                "facts": facts,
                "fact_count": len(facts),
                "group": group,
                "latest_run": {
                    "id": latest_run.get("id"),
                    "status": latest_run.get("status"),
                    "worker_name": latest_run.get("worker_name"),
                    "stop_reason": latest_run.get("stop_reason"),
                    "finished_at": latest_run.get("finished_at"),
                    "error": str(errors[-1]) if errors else "",
                },
            }
        )

    group_order = {"active": 0, "result": 1, "attempted": 2}
    branches.sort(
        key=lambda item: (
            group_order[item["group"]],
            -max(
                [float(fact.get("created_at") or 0) for fact in item["facts"]]
                or [float(item.get("created_at") or 0)]
            ),
            item["id"],
        )
    )

    completion = view.get("active_completion")
    completion_facts = []
    if completion:
        completion_facts = [
            facts_by_id[fact_id]
            for fact_id in completion.get("fact_ids", [])
            if fact_id in facts_by_id
            and facts_by_id[fact_id].get("predicate") not in {"project_origin", "project_goal"}
        ]
    key_facts = completion_facts or [
        fact
        for fact in sorted(
            visible_facts,
            key=lambda item: float(item.get("created_at") or 0),
            reverse=True,
        )
        if fact.get("predicate") not in {"project_origin", "project_goal"}
    ][:3]
    return {
        "completion": completion,
        "key_facts": key_facts,
        "branches": branches,
        "counts": {
            "active": sum(branch["group"] == "active" for branch in branches),
            "result": sum(branch["group"] == "result" for branch in branches),
            "attempted": sum(branch["group"] == "attempted" for branch in branches),
            "visible_facts": len(visible_facts),
        },
    }


def _runtime_state_payload(project_id: str, observed_at: float | None = None) -> dict:
    """Persistent Dispatcher view that remains useful after a process restart."""

    observed_at = time.time() if observed_at is None else observed_at
    intents = board.list_intents(project_id)
    status_counts: dict[str, int] = {}
    for intent in intents:
        status_counts[intent.status] = status_counts.get(intent.status, 0) + 1
    retrying_intents = []
    for intent in intents:
        retry_fields = _intent_retry_fields(intent, observed_at)
        if not retry_fields["retry_waiting"]:
            continue
        retrying_intents.append(
            {
                "intent_id": intent.id,
                "kind": intent.kind,
                "objective": intent.objective,
                "target_entity": intent.target_entity,
                "attempts": intent.attempts,
                **retry_fields,
            }
        )
    retrying_intents.sort(key=lambda item: item["retry_not_before"])
    dispatcher = board.get_dispatcher_state(project_id)
    if dispatcher is not None:
        stale_after = float(os.environ.get("SLIME_DISPATCHER_STALE_AFTER", "10"))
        dispatcher["stale"] = time.time() - dispatcher["heartbeat_at"] > stale_after
    return {
        "intent_status": status_counts,
        "active_leases": [
            {
                "intent_id": intent.id,
                "kind": intent.kind,
                "owner": intent.owner,
                "lease_expires_at": intent.lease_expires_at,
                "last_heartbeat_at": intent.last_heartbeat_at,
                "attempts": intent.attempts,
            }
            for intent in intents
            if intent.status == "running"
        ],
        "retrying_intents": retrying_intents,
        "next_retry_not_before": retrying_intents[0]["retry_not_before"] if retrying_intents else None,
        "recent_worker_runs": board.list_worker_runs(project_id, 50),
        "reason_lease": board.get_reason_lease(project_id),
        "dispatcher": dispatcher,
        "observed_at": observed_at,
    }


@app.get("/projects/{project_id}/view")
def project_view(
    project_id: str,
    worker_run_limit: int = Query(default=50, ge=1, le=250),
    evidence_limit: int = Query(default=100, ge=1, le=500),
    action_limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Cairn-style read-only projection for a project graph and inspector."""

    try:
        board.ensure_scope_hints(project_id)
        observed_at = time.time()
        view = board.view_projection(
            project_id,
            worker_run_limit=worker_run_limit,
            evidence_limit=evidence_limit,
            action_limit=action_limit,
        )
        _decorate_retry_view(view, observed_at)
        view["readable"] = _readable_projection(view)
        view["runtime"] = _runtime_state_payload(project_id, observed_at)
        return view
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc


@app.post("/projects/{project_id}/hints", status_code=201)
def add_hint(project_id: str, payload: HintInput) -> dict:
    """Append human judgment to the persistent Blackboard Hint stream."""

    try:
        _writable_project(project_id)
        hint, created = board.add_hint(project_id, payload.content, payload.creator)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"created": created, "hint": asdict(hint)}


@app.get("/projects/{project_id}/events")
def project_events(
    project_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Read one cursor-based page of immutable Blackboard activity."""

    try:
        board.get_project(project_id)
        page = board.list_events(project_id, after_id=after_id, limit=limit + 1)
        has_more = len(page) > limit
        events = page[:limit]
        return {
            "events": events,
            "after_id": after_id,
            "next_after_id": events[-1]["id"] if events else after_id,
            "latest_event_id": board.latest_event_id(project_id),
            "has_more": has_more,
        }
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc


@app.put("/projects/{project_id}/status")
def update_project_status(project_id: str, payload: ProjectStatusInput) -> dict:
    try:
        project = board.set_project_status(project_id, payload.status)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project": asdict(project)}


@app.post("/projects/{project_id}/complete")
def complete_project(project_id: str, payload: CompletionInput) -> dict:
    try:
        _writable_project(project_id)
        completion, created = board.complete_project(
            project_id,
            payload.fact_ids,
            payload.description,
            payload.worker_name,
        )
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"created": created, "completion": completion}


@app.post("/projects/{project_id}/reopen")
def reopen_project(project_id: str, payload: ReopenInput) -> dict:
    try:
        _writable_project(project_id)
        return board.reopen_project(project_id, payload.description, payload.creator)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/projects/{project_id}/facts")
def add_fact(project_id: str, payload: FactInput) -> dict:
    project = _writable_project(project_id)
    candidate = FactCandidate(**payload.model_dump())
    review = FactEvidenceGate(board, project).review(candidate)
    if not review.accepted:
        raise HTTPException(422, review.reason)
    fact, created = board.add_fact(project_id, candidate)
    return {"created": created, "fact": fact}


@app.post("/projects/{project_id}/intents")
def add_intent(project_id: str, payload: IntentInput) -> dict:
    project = _writable_project(project_id)
    proposal = IntentProposal(**payload.model_dump())
    review = IntentGate(board, project).review(proposal)
    if not review.accepted:
        raise HTTPException(422, review.reason)
    intent, created = board.add_intent(project_id, proposal, nutrients.score(proposal))
    return {"created": created, "intent": intent}


@app.post("/projects/{project_id}/intents/{intent_id}/retry")
def retry_intent(project_id: str, intent_id: str) -> dict:
    """Clear an Intent's retry delay so the Dispatcher may claim it immediately."""

    try:
        _writable_project(project_id)
        intent = board.retry_intent(project_id, intent_id, reason="manual_retry")
    except KeyError as exc:
        raise HTTPException(404, "project or intent not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"retried": True, "intent": asdict(intent)}


@app.post("/projects/{project_id}/evidence")
def register_evidence(project_id: str, payload: EvidenceInput) -> dict:
    _writable_project(project_id)
    board.register_evidence(project_id, payload.evidence_ref, payload.kind, metadata=payload.metadata)
    return {"registered": True, "evidence_ref": payload.evidence_ref}


@app.post("/projects/{project_id}/hypotheses")
def add_hypothesis(project_id: str, payload: HypothesisInput) -> dict:
    _writable_project(project_id)
    candidate = HypothesisCandidate(**payload.model_dump())
    review = HypothesisGate(board, project_id).review(candidate)
    if not review.accepted:
        raise HTTPException(422, review.reason)
    hypothesis, created = board.add_hypothesis(project_id, candidate)
    return {"created": created, "hypothesis": hypothesis}


@app.get("/projects/{project_id}/worker-runs")
def list_worker_runs(project_id: str) -> dict:
    try:
        board.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    return {"worker_runs": board.list_worker_runs(project_id)}


@app.get("/projects/{project_id}/runtime")
def runtime_state(project_id: str) -> dict:
    try:
        board.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, "project not found") from exc
    return _runtime_state_payload(project_id)


def main() -> None:
    import uvicorn

    uvicorn.run("slime_cairn.server.api:app", host="127.0.0.1", port=8000, reload=False)
