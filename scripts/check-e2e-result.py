from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any


def load_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_loads(value: str, default):
    try:
        return json.loads(value)
    except Exception:
        return default


def latest_project_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM projects ORDER BY created_at DESC LIMIT 1").fetchone()
    return str(row["id"]) if row else None


def latest_project_id_by_name(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM projects WHERE name = ? ORDER BY created_at DESC LIMIT 1",
        (name,),
    ).fetchone()
    return str(row["id"]) if row else None


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def get_active_completion(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM project_completions WHERE project_id = ? AND active = 1 ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["fact_ids"] = json_loads(item.pop("fact_ids_json", "[]"), [])
    item["active"] = bool(item["active"])
    return item


def list_facts(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    facts = []
    for row in rows:
        item = dict(row)
        item["evidence_refs"] = json_loads(item.pop("evidence_json", "[]"), [])
        item["attributes"] = json_loads(item.pop("attributes_json", "{}"), {})
        facts.append(item)
    return facts


def list_evidence(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT evidence_ref, kind, source_action_id, metadata_json, created_at
        FROM evidence_records WHERE project_id = ?""",
        (project_id,),
    ).fetchall()
    evidence = {}
    for row in rows:
        item = dict(row)
        item["metadata"] = json_loads(item.pop("metadata_json", "{}"), {})
        evidence[item["evidence_ref"]] = item
    return evidence


def list_intents(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT kind, objective, target_entity, status, nutrient, attempts FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_worker_runs(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, intent_id, mode, worker_name, status, tool_calls, progress_score, stop_reason,
        activity_json, errors_json, model_session_json, started_at, finished_at
        FROM worker_runs WHERE project_id = ? ORDER BY started_at""",
        (project_id,),
    ).fetchall()
    runs = []
    for row in rows:
        item = dict(row)
        item["activity"] = json_loads(item.pop("activity_json", "[]"), [])
        item["errors"] = json_loads(item.pop("errors_json", "[]"), [])
        item["model_session"] = json_loads(item.pop("model_session_json", "{}"), {})
        if item["status"] == "running":
            live = live_cli_session(item)
            if live:
                item["model_session"] = {**item["model_session"], **live}
        runs.append(item)
    return runs


def get_dispatcher_state(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT dispatcher_id, state, status_json, heartbeat_at
        FROM dispatcher_states WHERE project_id = ?""",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    status = json_loads(row["status_json"], {})
    return {
        "dispatcher_id": row["dispatcher_id"],
        "state": row["state"],
        "status": status,
        "heartbeat_at": row["heartbeat_at"],
    }


def live_cli_session(run: dict[str, Any]) -> dict[str, Any]:
    session_key = f"{run.get('mode')}:{run.get('intent_id')}"
    digest = sha256(session_key.encode("utf-8")).hexdigest()[:24]
    roots = [
        Path(".slime-sessions") / "codex-cli" / digest,
        Path(".slime-sessions") / "claude-code" / digest,
        Path(".slime-sessions") / "pi-cli" / digest,
    ]
    for root in roots:
        path = root / "session.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("session_key") != session_key:
            continue
        return {
            "live_manifest": str(path.resolve()),
            "live_status": data.get("status"),
            "turns": data.get("turns"),
            "last_error": data.get("last_error", ""),
            "last_exit_code": data.get("last_exit_code"),
            "last_elapsed_seconds": data.get("last_elapsed_seconds"),
            "transcript_refs": data.get("transcript_refs") or [],
        }
    return {}


def status_from_db(database: Path, project_id: str | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        pid = project_id or latest_project_id(conn)
        if not pid:
            return {"ok": False, "state": "empty_database", "message": "database has no project"}
        project = get_project(conn, pid)
        if not project:
            return {"ok": False, "state": "project_missing", "message": f"project not found: {pid}"}
        completion = get_active_completion(conn, pid)
        facts = list_facts(conn, pid)
        evidence = list_evidence(conn, pid)
        facts_by_id = {item["id"]: item for item in facts}
        intents = list_intents(conn, pid)
        runs = list_worker_runs(conn, pid)
        dispatcher_state = get_dispatcher_state(conn, pid)
        cited_facts = [facts_by_id[fact_id] for fact_id in (completion or {}).get("fact_ids", []) if fact_id in facts_by_id]
        project_seed_kinds = {"project_directive", "control_artifact"}
        cited_facts_with_task_evidence = [
            item
            for item in cited_facts
            if any(
                evidence.get(ref, {}).get("kind") not in project_seed_kinds
                for ref in item.get("evidence_refs", [])
            )
        ]
        evidence_count = sum(
            1
            for item in cited_facts
            for ref in item.get("evidence_refs", [])
            if evidence.get(ref, {}).get("kind") not in project_seed_kinds
        )
        pending = [item for item in intents if item["status"] in {"pending", "running"}]
        failed_runs = [item for item in runs if item["status"] == "failed"]
        running_runs = [item for item in runs if item["status"] == "running"]
        done = project["status"] == "completed" and completion is not None and bool(cited_facts) and evidence_count > 0
        if done:
            state = "solved"
        elif project["status"] == "completed" and completion is not None:
            state = "completed_needs_evidence_review"
        elif pending or running_runs:
            state = "still_running_or_incomplete"
        elif failed_runs:
            state = "stopped_with_failed_runs"
        else:
            state = "incomplete"
        return {
            "ok": True,
            "state": state,
            "done": done,
            "project": {
                "id": pid,
                "name": project["name"],
                "target": project["target"],
                "status": project["status"],
                "goal": project["goal"],
            },
            "completion": completion,
            "cited_facts": cited_facts,
            "cited_facts_with_task_evidence": cited_facts_with_task_evidence,
            "fact_count": len(facts),
            "intent_counts": count_by(intents, "status"),
            "worker_run_counts": count_by(runs, "status"),
            "failed_runs": failed_runs[-5:],
            "running_runs": running_runs[-8:],
            "recent_runs": runs[-8:],
            "recent_facts": facts[-12:],
            "dispatcher_state": dispatcher_state,
        }
    finally:
        conn.close()


def project_id_for_name(database: Path, name: str) -> str | None:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        return latest_project_id_by_name(conn, name)
    finally:
        conn.close()


def count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, ""))
        result[value] = result.get(value, 0) + 1
    return result


def print_human(report: dict[str, Any]) -> None:
    print(f"STATE: {report['state']}")
    print(f"DONE:  {report.get('done', False)}")
    project = report.get("project") or {}
    if project:
        print(f"PROJECT: {project.get('name')} [{project.get('status')}]")
        print(f"TARGET:  {project.get('target')}")
    completion = report.get("completion")
    if completion:
        print("\nCOMPLETION:")
        print(f"- worker: {completion.get('worker_name')}")
        print(f"- description: {completion.get('description')}")
        print(f"- fact_ids: {', '.join(completion.get('fact_ids', []))}")
    print("\nCOUNTS:")
    print(f"- facts: {report.get('fact_count', 0)}")
    print(f"- intents: {report.get('intent_counts', {})}")
    print(f"- worker_runs: {report.get('worker_run_counts', {})}")
    dispatcher = report.get("dispatcher_state") or {}
    dispatcher_status = dispatcher.get("status") or {}
    if dispatcher:
        cooldown = float(dispatcher_status.get("reason_cooldown_remaining") or 0)
        print("\nDISPATCHER:")
        print(f"- state: {dispatcher.get('state')} heartbeat_at={dispatcher.get('heartbeat_at')}")
        print(
            f"- pending_reason_signals={dispatcher_status.get('pending_reason_signals', 0)} "
            f"reason_failures={dispatcher_status.get('reason_failures', 0)} "
            f"reason_cooldown_remaining={cooldown:.1f}s"
        )
    running = report.get("running_runs") or []
    if running:
        print("\nRUNNING WORKERS:")
        now_ts = time.time()
        for run in running:
            elapsed = max(0, int(now_ts - float(run.get("started_at") or now_ts)))
            session = run.get("model_session") or {}
            refs = session.get("transcript_refs") or []
            last_ref = refs[-1] if refs else ""
            live_status = session.get("live_status") or session.get("status") or ""
            last_error = str(session.get("last_error") or "")
            error_hint = f" live_status={live_status}" if live_status else ""
            if last_error:
                error_hint += f" last_error={last_error[:160]}"
            print(
                f"- {run.get('mode')} {run.get('worker_name')} "
                f"elapsed={elapsed}s tool_calls={run.get('tool_calls')} "
                f"last_transcript={last_ref}{error_hint}"
            )
    cited = report.get("cited_facts") or []
    if cited:
        print("\nCITED FACTS:")
        for fact in cited:
            refs = ", ".join(fact.get("evidence_refs", [])[:3])
            print(f"- {fact['id']} | {fact['predicate']} => {fact['object']} | evidence: {refs}")
    recent = report.get("recent_facts") or []
    if recent:
        print("\nRECENT FACTS:")
        for fact in recent[-8:]:
            print(f"- {fact['predicate']} => {fact['object']}")
    failures = report.get("failed_runs") or []
    if failures:
        print("\nRECENT FAILED RUNS:")
        for run in failures:
            print(f"- {run['worker_name']} {run['mode']} stop={run['stop_reason']} errors={run['errors'][-2:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether a Slime-Cairn e2e run reached evidence-backed completion.")
    parser.add_argument("--database", "-d", help="Path to e2e sqlite database, e.g. runs/name.db")
    parser.add_argument("--result", "-r", help="Path to e2e result json; database path will be read from it")
    parser.add_argument("--project-id", help="Project id inside the database; default is latest project")
    parser.add_argument("--name", help="Latest project with this name")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after printing the report")
    args = parser.parse_args()

    database = Path(args.database) if args.database else None
    if args.result:
        result = load_result(Path(args.result))
        database = Path(result.get("database", database or ""))
        if not args.project_id and result.get("project"):
            args.project_id = result["project"].get("id")
    if not database:
        print("usage: provide --database runs/name.db or --result runs/name-result.json", file=sys.stderr)
        return 2
    if args.name and not args.project_id:
        args.project_id = project_id_for_name(database, args.name)
        if not args.project_id:
            print(f"project name not found: {args.name}", file=sys.stderr)
            return 2
    report = status_from_db(database, args.project_id)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0 if args.no_fail or report.get("done") else 1


if __name__ == "__main__":
    raise SystemExit(main())
