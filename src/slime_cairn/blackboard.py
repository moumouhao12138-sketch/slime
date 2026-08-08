from __future__ import annotations

from dataclasses import asdict
from functools import wraps
from itertools import combinations
import json
import re
import sqlite3
import threading
from typing import Any

from .models import (
    Fact,
    FactCandidate,
    Hint,
    Hypothesis,
    HypothesisCandidate,
    Intent,
    IntentProposal,
    PseudopodReport,
    Project,
    fingerprint,
    new_id,
    now,
    stable_json,
)


def synchronized(method):
    """Serialize compound SQLite operations while model/tool work stays parallel."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class Blackboard:
    """Persistent project memory. AI sessions may disappear; this data does not."""

    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    @synchronized
    def close(self) -> None:
        self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                target TEXT NOT NULL,
                goal TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_completions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                fact_ids_json TEXT NOT NULL,
                description TEXT NOT NULL,
                worker_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                revoked_at REAL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_completion_per_project
            ON project_completions(project_id) WHERE active = 1;
            CREATE TABLE IF NOT EXISTS hints (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                hint_key TEXT NOT NULL,
                content TEXT NOT NULL,
                creator TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(project_id, hint_key),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS hints_project_created
            ON hints(project_id, created_at, id);
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                source_intent_id TEXT,
                attributes_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                memory_state TEXT NOT NULL DEFAULT 'active',
                structural_importance REAL NOT NULL DEFAULT 0.0,
                reference_count INTEGER NOT NULL DEFAULT 0,
                branch_count INTEGER NOT NULL DEFAULT 0,
                omission_count INTEGER NOT NULL DEFAULT 0,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at REAL NOT NULL DEFAULT 0.0,
                last_revived_at REAL,
                UNIQUE(project_id, fact_key),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                objective TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                parent_fact_ids_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                expected_value REAL NOT NULL,
                novelty REAL NOT NULL,
                cost REAL NOT NULL,
                risk REAL NOT NULL,
                nutrient REAL NOT NULL,
                strength REAL NOT NULL,
                status TEXT NOT NULL,
                owner TEXT,
                lease_expires_at REAL,
                last_heartbeat_at REAL,
                last_error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL,
                failure_streak INTEGER NOT NULL DEFAULT 0,
                retry_not_before REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL,
                UNIQUE(project_id, fingerprint),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                action_key TEXT NOT NULL,
                tool TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                UNIQUE(project_id, action_key),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS evidence_records (
                project_id TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_action_id TEXT,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(project_id, evidence_ref)
            );
            CREATE TABLE IF NOT EXISTS hypotheses (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                statement TEXT NOT NULL,
                supporting_fact_ids_json TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                next_validation TEXT NOT NULL,
                source_intent_id TEXT,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(project_id, statement, source_intent_id)
            );
            CREATE TABLE IF NOT EXISTS worker_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                worker_name TEXT NOT NULL DEFAULT '',
                owner_token TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                context_manifest_json TEXT NOT NULL,
                activity_json TEXT NOT NULL,
                errors_json TEXT NOT NULL,
                model_usage_json TEXT NOT NULL,
                model_session_json TEXT NOT NULL DEFAULT '{}',
                validation_json TEXT NOT NULL DEFAULT '{}',
                tool_calls INTEGER NOT NULL DEFAULT 0,
                progress_score REAL NOT NULL DEFAULT 0.0,
                stop_reason TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS path_edges (
                project_id TEXT NOT NULL,
                fact_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                strength REAL NOT NULL DEFAULT 1.0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(project_id, fact_id, intent_id)
            );
            CREATE TABLE IF NOT EXISTS fact_relations (
                project_id TEXT NOT NULL,
                source_fact_id TEXT NOT NULL,
                target_fact_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                created_at REAL NOT NULL,
                PRIMARY KEY(project_id, source_fact_id, target_fact_id, relation)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reason_state (
                project_id TEXT PRIMARY KEY,
                last_fact_time REAL NOT NULL,
                last_fact_ids_json TEXT NOT NULL DEFAULT '[]',
                last_event_id INTEGER NOT NULL,
                global_summary TEXT NOT NULL,
                incremental_runs INTEGER NOT NULL DEFAULT 0,
                last_global_audit_event_id INTEGER NOT NULL DEFAULT 0,
                last_global_audit_at REAL NOT NULL DEFAULT 0.0,
                no_progress_streak INTEGER NOT NULL DEFAULT 0,
                last_hint_created_at REAL NOT NULL DEFAULT 0.0,
                failure_streak INTEGER NOT NULL DEFAULT 0,
                failure_last_error TEXT NOT NULL DEFAULT '',
                failure_paused_until REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS reason_leases (
                project_id TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                worker_name TEXT NOT NULL,
                trigger TEXT NOT NULL,
                reason_intent_id TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                last_heartbeat_at REAL NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS reason_leases_expiry
            ON reason_leases(lease_expires_at);
            CREATE TABLE IF NOT EXISTS dispatcher_states (
                project_id TEXT PRIMARY KEY,
                dispatcher_id TEXT NOT NULL,
                state TEXT NOT NULL,
                status_json TEXT NOT NULL,
                heartbeat_at REAL NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS benchmark_automations (
                task_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                parallelism INTEGER NOT NULL DEFAULT 3,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            """
        )
        migrations = {
            "facts": {
                "memory_state": "TEXT NOT NULL DEFAULT 'active'",
                "structural_importance": "REAL NOT NULL DEFAULT 0.0",
                "reference_count": "INTEGER NOT NULL DEFAULT 0",
                "branch_count": "INTEGER NOT NULL DEFAULT 0",
                "omission_count": "INTEGER NOT NULL DEFAULT 0",
                "access_count": "INTEGER NOT NULL DEFAULT 0",
                "last_accessed_at": "REAL NOT NULL DEFAULT 0.0",
                "last_revived_at": "REAL",
            },
            "intents": {
                "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_heartbeat_at": "REAL",
                "last_error": "TEXT NOT NULL DEFAULT ''",
                "failure_streak": "INTEGER NOT NULL DEFAULT 0",
                "retry_not_before": "REAL NOT NULL DEFAULT 0.0",
            },
            "reason_state": {
                "last_fact_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "incremental_runs": "INTEGER NOT NULL DEFAULT 0",
                "last_global_audit_event_id": "INTEGER NOT NULL DEFAULT 0",
                "last_global_audit_at": "REAL NOT NULL DEFAULT 0.0",
                "no_progress_streak": "INTEGER NOT NULL DEFAULT 0",
                "last_hint_created_at": "REAL NOT NULL DEFAULT 0.0",
                "failure_streak": "INTEGER NOT NULL DEFAULT 0",
                "failure_last_error": "TEXT NOT NULL DEFAULT ''",
                "failure_paused_until": "REAL NOT NULL DEFAULT 0.0",
            },
            "worker_runs": {
                "validation_json": "TEXT NOT NULL DEFAULT '{}'",
                "worker_name": "TEXT NOT NULL DEFAULT ''",
                "owner_token": "TEXT NOT NULL DEFAULT ''",
                "model_session_json": "TEXT NOT NULL DEFAULT '{}'",
            },
        }
        retry_schema_upgraded = False
        for table, definitions in migrations.items():
            columns = {
                str(row["name"])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in definitions.items():
                if column not in columns:
                    try:
                        self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    except sqlite3.OperationalError as exc:
                        # Two API/service processes can open the same legacy
                        # database at once.  If the peer completed this exact
                        # migration between PRAGMA and ALTER, the schema is
                        # already in the required state.
                        if "duplicate column name" not in str(exc).lower():
                            raise
                        continue
                    if table == "intents" and column in {"failure_streak", "retry_not_before"}:
                        retry_schema_upgraded = True

        self._connection.execute(
            """CREATE INDEX IF NOT EXISTS intents_pending_retry
            ON intents(project_id, status, retry_not_before, nutrient DESC, created_at)"""
        )
        if retry_schema_upgraded:
            # Older releases created ``failed`` Intent rows exclusively after
            # the automatic attempt cap.  Make those records recoverable on
            # upgrade while retaining their error and lease history.
            self._connection.execute(
                """UPDATE intents SET status = 'pending', retry_not_before = 0.0,
                failure_streak = MAX(failure_streak, CASE WHEN attempts > 0 THEN attempts ELSE 1 END)
                WHERE status = 'failed'"""
            )

        # Several early project tables predate foreign-key constraints.  The
        # triggers make a final delete a hard boundary even when a cancelled
        # Worker thread wakes up after its Dispatcher has finished.
        project_child_tables = (
            "project_completions",
            "hints",
            "facts",
            "intents",
            "actions",
            "evidence_records",
            "hypotheses",
            "worker_runs",
            "path_edges",
            "fact_relations",
            "events",
            "reason_state",
            "reason_leases",
            "dispatcher_states",
        )
        for table in project_child_tables:
            self._connection.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_project_exists_insert
                BEFORE INSERT ON {table}
                FOR EACH ROW WHEN NOT EXISTS (
                    SELECT 1 FROM projects WHERE id = NEW.project_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'project not found');
                END;
                CREATE TRIGGER IF NOT EXISTS {table}_project_exists_update
                BEFORE UPDATE OF project_id ON {table}
                FOR EACH ROW WHEN NOT EXISTS (
                    SELECT 1 FROM projects WHERE id = NEW.project_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'project not found');
                END;
                """
            )
        self._migrate_intent_fingerprints()

    def _migrate_intent_fingerprints(self) -> None:
        """Upgrade pre-capability-pool Intent keys without losing idempotency."""

        rows = self._connection.execute(
            """SELECT id, kind, objective, target_entity, parent_fact_ids_json,
            context_json, provenance_json, fingerprint FROM intents"""
        ).fetchall()
        for row in rows:
            current = fingerprint(
                str(row["kind"]),
                str(row["objective"]).strip(),
                str(row["target_entity"]),
                sorted(set(json.loads(row["parent_fact_ids_json"]))),
                json.loads(row["context_json"]),
                json.loads(row["provenance_json"]),
            )
            if current != row["fingerprint"]:
                self._connection.execute(
                    "UPDATE intents SET fingerprint = ? WHERE id = ?",
                    (current, row["id"]),
                )

    @synchronized
    def create_project(self, name: str, target: str, goal: str, scope: dict[str, Any]) -> Project:
        project = Project(new_id("project"), name, target, goal, scope)
        self._connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project.id, project.name, project.target, project.goal, stable_json(project.scope), project.status, project.created_at),
        )
        self.add_event(project.id, "project.created", asdict(project))
        return project

    @synchronized
    def get_project(self, project_id: str) -> Project:
        row = self._connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return Project(row["id"], row["name"], row["target"], row["goal"], json.loads(row["scope_json"]), row["status"], row["created_at"])

    @synchronized
    def update_project_context(
        self,
        project_id: str,
        *,
        target: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> Project:
        """Refresh a restarted external target without replacing project memory."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            next_target = project.target if target is None else str(target).strip()
            next_scope = project.scope if scope is None else dict(scope)
            if not next_target:
                raise ValueError("project target must not be empty")
            changed = next_target != project.target or next_scope != project.scope
            if changed:
                self._connection.execute(
                    "UPDATE projects SET target = ?, scope_json = ? WHERE id = ?",
                    (next_target, stable_json(next_scope), project_id),
                )
                self.add_event(
                    project_id,
                    "project.context_updated",
                    {
                        "previous_target": project.target,
                        "target": next_target,
                        "scope": next_scope,
                    },
                )
            self._connection.commit()
            return self.get_project(project_id)
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def list_projects(
        self,
        status: str | None = None,
        include_deleting: bool = False,
    ) -> list[Project]:
        if status:
            rows = self._connection.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        elif include_deleting:
            rows = self._connection.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM projects WHERE status != 'deleting' ORDER BY created_at"
            ).fetchall()
        return [
            Project(
                row["id"],
                row["name"],
                row["target"],
                row["goal"],
                json.loads(row["scope_json"]),
                row["status"],
                row["created_at"],
            )
            for row in rows
        ]

    @synchronized
    def set_project_status(self, project_id: str, status: str) -> Project:
        """Pause or resume scheduling without discarding Facts or Intents."""

        if status not in {"running", "stopped"}:
            raise ValueError("项目状态只能设置为 running 或 stopped")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            if project.status == "completed":
                raise ValueError("completed 项目必须通过 reopen 恢复")
            if project.status == "deleting":
                raise ValueError("项目正在删除，不能更改状态")

            changed = project.status != status
            released_intent_ids: list[str] = []
            stopped_run_ids: list[str] = []
            cleared_reason_lease: dict[str, Any] | None = None
            if changed:
                self._connection.execute(
                    "UPDATE projects SET status = ? WHERE id = ?",
                    (status, project_id),
                )

            if status == "stopped":
                intent_rows = self._connection.execute(
                    "SELECT id FROM intents WHERE project_id = ? AND status = 'running'",
                    (project_id,),
                ).fetchall()
                released_intent_ids = [str(row["id"]) for row in intent_rows]
                if released_intent_ids:
                    self._connection.execute(
                        """UPDATE intents SET status = 'pending', owner = NULL, lease_expires_at = NULL,
                        last_heartbeat_at = NULL, last_error = 'project_stopped'
                        WHERE project_id = ? AND status = 'running'""",
                        (project_id,),
                    )
                stopped_run_ids = self._fail_project_worker_runs_locked(
                    project_id,
                    "project_stopped: project scheduling stopped before worker completed",
                    "project_stopped",
                )
                cleared_reason_lease = self._clear_reason_lease_locked(
                    project_id,
                    "project_stopped",
                )

            if changed:
                self.add_event(
                    project_id,
                    f"project.{status}",
                    {
                        "previous_status": project.status,
                        "released_intent_ids": released_intent_ids,
                        "stopped_worker_run_ids": stopped_run_ids,
                        "cleared_reason_lease": cleared_reason_lease,
                    },
                )
            if status == "stopped" and (released_intent_ids or stopped_run_ids):
                self.add_event(
                    project_id,
                    "project.runtime_released",
                    {
                        "intent_ids": released_intent_ids,
                        "worker_run_ids": stopped_run_ids,
                        "reason_lease": cleared_reason_lease,
                        "reason": "project_stopped",
                    },
                )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.get_project(project_id)

    @synchronized
    def request_project_deletion(self, project_id: str) -> tuple[Project, bool]:
        """Fence active work and make the project eligible for service cleanup.

        The service owns the final Docker/workspace cleanup.  Keeping the row
        in ``deleting`` state until that succeeds prevents concurrent Workers
        from writing into a deleted project boundary.
        """

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            if project.status == "deleting":
                self._connection.commit()
                return project, False

            intent_rows = self._connection.execute(
                "SELECT id FROM intents WHERE project_id = ? AND status = 'running'",
                (project_id,),
            ).fetchall()
            released_intent_ids = [str(row["id"]) for row in intent_rows]
            if released_intent_ids:
                self._connection.execute(
                    """UPDATE intents SET status = 'pending', owner = NULL, lease_expires_at = NULL,
                    last_heartbeat_at = NULL, last_error = 'project_deleting'
                    WHERE project_id = ? AND status = 'running'""",
                    (project_id,),
                )
            stopped_run_ids = self._fail_project_worker_runs_locked(
                project_id,
                "project_deleting: project deletion requested before worker completed",
                "project_deleting",
            )
            cleared_reason_lease = self._clear_reason_lease_locked(
                project_id,
                "project_deleting",
            )
            self._connection.execute(
                "UPDATE projects SET status = 'deleting' WHERE id = ?",
                (project_id,),
            )
            self.add_event(
                project_id,
                "project.deletion_requested",
                {
                    "previous_status": project.status,
                    "released_intent_ids": released_intent_ids,
                    "stopped_worker_run_ids": stopped_run_ids,
                    "cleared_reason_lease": cleared_reason_lease,
                },
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.get_project(project_id), True

    @synchronized
    def delete_project(self, project_id: str) -> Project:
        """Permanently delete a project after its runtime has been cleaned up."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            if project.status != "deleting":
                raise ValueError("项目必须处于 deleting 状态才能最终删除")
            for table in (
                "events",
                "reason_leases",
                "dispatcher_states",
                "reason_state",
                "project_completions",
                "hints",
                "path_edges",
                "fact_relations",
                "actions",
                "evidence_records",
                "worker_runs",
                "hypotheses",
                "intents",
                "facts",
            ):
                self._connection.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
            self._connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._connection.commit()
            return project
        except BaseException:
            self._connection.rollback()
            raise

    def _fail_project_worker_runs_locked(
        self,
        project_id: str,
        error_message: str,
        stop_reason: str,
    ) -> list[str]:
        """Fail every still-running worker row while the caller holds a DB transaction."""

        rows = self._connection.execute(
            "SELECT id, errors_json FROM worker_runs WHERE project_id = ? AND status = 'running'",
            (project_id,),
        ).fetchall()
        finished_at = now()
        run_ids: list[str] = []
        for row in rows:
            try:
                errors = json.loads(row["errors_json"])
            except json.JSONDecodeError:
                errors = []
            if not isinstance(errors, list):
                errors = [str(errors)]
            if error_message not in errors:
                errors.append(error_message)
            self._connection.execute(
                """UPDATE worker_runs SET status = 'failed', errors_json = ?, stop_reason = ?,
                finished_at = ? WHERE id = ? AND status = 'running'""",
                (stable_json(errors), stop_reason[:2000], finished_at, row["id"]),
            )
            run_ids.append(str(row["id"]))
        return run_ids

    @synchronized
    def is_project_running(self, project_id: str) -> bool:
        return self.get_project(project_id).status == "running"

    @staticmethod
    def _reason_lease_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_id": str(row["project_id"]),
            "owner_token": str(row["owner_token"]),
            "worker_name": str(row["worker_name"]),
            "trigger": str(row["trigger"]),
            "reason_intent_id": str(row["reason_intent_id"]),
            "started_at": float(row["started_at"]),
            "lease_expires_at": float(row["lease_expires_at"]),
            "last_heartbeat_at": float(row["last_heartbeat_at"]),
        }

    def _expire_reason_leases_locked(
        self,
        project_id: str | None,
        current: float,
    ) -> list[dict[str, Any]]:
        clause = "AND project_id = ?" if project_id is not None else ""
        parameters: tuple[Any, ...] = (
            (current, project_id) if project_id is not None else (current,)
        )
        rows = self._connection.execute(
            f"""SELECT * FROM reason_leases
            WHERE lease_expires_at <= ? {clause}
            ORDER BY lease_expires_at, project_id""",
            parameters,
        ).fetchall()
        expired = [self._reason_lease_row(row) for row in rows]
        if not expired:
            return []
        ids = [lease["project_id"] for lease in expired]
        placeholders = ",".join("?" for _ in ids)
        self._connection.execute(
            f"DELETE FROM reason_leases WHERE project_id IN ({placeholders})",
            tuple(ids),
        )
        for lease in expired:
            self.add_event(
                lease["project_id"],
                "reason.lease_expired",
                {
                    "owner_token": lease["owner_token"],
                    "worker_name": lease["worker_name"],
                    "reason_intent_id": lease["reason_intent_id"],
                    "lease_expires_at": lease["lease_expires_at"],
                },
            )
        return expired

    def _clear_reason_lease_locked(
        self,
        project_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM reason_leases WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        lease = self._reason_lease_row(row)
        self._connection.execute(
            "DELETE FROM reason_leases WHERE project_id = ?",
            (project_id,),
        )
        self.add_event(
            project_id,
            "reason.lease_released",
            {
                "owner_token": lease["owner_token"],
                "worker_name": lease["worker_name"],
                "reason_intent_id": lease["reason_intent_id"],
                "reason": reason,
            },
        )
        return lease

    @synchronized
    def claim_reason_lease(
        self,
        project_id: str,
        owner_token: str,
        worker_name: str,
        trigger: str,
        lease_seconds: float,
        reason_intent_id: str = "",
    ) -> dict[str, Any] | None:
        """Claim the one durable Reason slot for a running project."""

        owner_token = owner_token.strip()
        worker_name = worker_name.strip()
        trigger = trigger.strip() or "unspecified"
        reason_intent_id = reason_intent_id.strip()
        if not owner_token or not worker_name:
            raise ValueError("Reason lease needs owner_token and worker_name")
        if lease_seconds <= 0:
            raise ValueError("Reason lease_seconds must be greater than 0")

        current = now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            if project.status != "running":
                self._connection.commit()
                return None
            self._expire_reason_leases_locked(project_id, current)
            row = self._connection.execute(
                "SELECT * FROM reason_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is not None:
                lease = self._reason_lease_row(row)
                if lease["owner_token"] != owner_token:
                    self._connection.commit()
                    return None
                self._connection.execute(
                    """UPDATE reason_leases SET lease_expires_at = ?, last_heartbeat_at = ?,
                    worker_name = ?, trigger = ?, reason_intent_id = ?
                    WHERE project_id = ? AND owner_token = ?""",
                    (
                        current + lease_seconds,
                        current,
                        worker_name,
                        trigger,
                        reason_intent_id or lease["reason_intent_id"],
                        project_id,
                        owner_token,
                    ),
                )
            else:
                self._connection.execute(
                    """INSERT INTO reason_leases
                    (project_id, owner_token, worker_name, trigger, reason_intent_id,
                     started_at, lease_expires_at, last_heartbeat_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        owner_token,
                        worker_name,
                        trigger,
                        reason_intent_id,
                        current,
                        current + lease_seconds,
                        current,
                    ),
                )
                self.add_event(
                    project_id,
                    "reason.lease_claimed",
                    {
                        "owner_token": owner_token,
                        "worker_name": worker_name,
                        "trigger": trigger,
                        "reason_intent_id": reason_intent_id,
                        "lease_expires_at": current + lease_seconds,
                    },
                )
            claimed = self._connection.execute(
                "SELECT * FROM reason_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            self._connection.commit()
            return self._reason_lease_row(claimed) if claimed is not None else None
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def renew_reason_lease(
        self,
        project_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> bool:
        if not owner_token.strip() or lease_seconds <= 0:
            return False
        current = now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project_row = self._connection.execute(
                "SELECT status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None or project_row["status"] != "running":
                self._connection.commit()
                return False
            self._expire_reason_leases_locked(project_id, current)
            cursor = self._connection.execute(
                """UPDATE reason_leases SET lease_expires_at = ?, last_heartbeat_at = ?
                WHERE project_id = ? AND owner_token = ? AND lease_expires_at > ?""",
                (current + lease_seconds, current, project_id, owner_token, current),
            )
            self._connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def release_reason_lease(
        self,
        project_id: str,
        owner_token: str,
        reason: str = "reason_finished",
    ) -> bool:
        current = now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._expire_reason_leases_locked(project_id, current)
            row = self._connection.execute(
                "SELECT * FROM reason_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or str(row["owner_token"]) != owner_token:
                self._connection.commit()
                return False
            self._clear_reason_lease_locked(project_id, reason)
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def expire_reason_leases(
        self,
        project_id: str | None = None,
        as_of: float | None = None,
    ) -> list[dict[str, Any]]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            expired = self._expire_reason_leases_locked(
                project_id,
                now() if as_of is None else float(as_of),
            )
            self._connection.commit()
            return expired
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def get_reason_lease(self, project_id: str) -> dict[str, Any] | None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self._connection.execute(
                "SELECT 1 FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            self._expire_reason_leases_locked(project_id, now())
            row = self._connection.execute(
                "SELECT * FROM reason_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            self._connection.commit()
            return self._reason_lease_row(row) if row is not None else None
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def owns_reason_lease(self, project_id: str, owner_token: str) -> bool:
        if not owner_token:
            return False
        lease = self.get_reason_lease(project_id)
        return lease is not None and lease["owner_token"] == owner_token

    @synchronized
    def owns_intent_lease(
        self,
        project_id: str,
        intent_id: str,
        owner_token: str,
    ) -> bool:
        if not owner_token:
            return False
        row = self._connection.execute(
            """SELECT 1 FROM intents
            WHERE project_id = ? AND id = ? AND status = 'running' AND owner = ?
            AND lease_expires_at IS NOT NULL AND lease_expires_at > ?""",
            (project_id, intent_id, owner_token, now()),
        ).fetchone()
        return row is not None

    @synchronized
    def complete_project(
        self,
        project_id: str,
        fact_ids: list[str],
        description: str,
        worker_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist an evidence-backed Goal edge and make scheduling terminal."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            existing = self.get_active_completion(project_id)
            if existing is not None:
                self._connection.commit()
                return existing, False
            if project.status != "running":
                raise ValueError(f"只有 running 项目可以完成，当前状态为 {project.status}")
            cleaned_ids = list(dict.fromkeys(str(value).strip() for value in fact_ids if str(value).strip()))
            description = description.strip()
            worker_name = worker_name.strip()
            if not cleaned_ids or not description or not worker_name:
                raise ValueError("完成记录需要 fact_ids、description 和 worker_name")
            benchmark_scope = project.scope.get("benchmark")
            if isinstance(benchmark_scope, dict) and benchmark_scope.get("managed") is True:
                placeholders = ",".join("?" for _ in cleaned_ids)
                verified = self._connection.execute(
                    f"""SELECT id FROM facts WHERE project_id = ?
                    AND predicate = 'benchmark_completion_verified'
                    AND id IN ({placeholders}) LIMIT 1""",
                    (project_id, *cleaned_ids),
                ).fetchone()
                if verified is None:
                    raise ValueError("Benchmark 项目需要平台完成验证 Fact")
            placeholders = ",".join("?" for _ in cleaned_ids)
            rows = self._connection.execute(
                f"SELECT id FROM facts WHERE project_id = ? AND id IN ({placeholders})",
                (project_id, *cleaned_ids),
            ).fetchall()
            found = {str(row["id"]) for row in rows}
            missing = sorted(set(cleaned_ids) - found)
            if missing:
                raise ValueError(f"完成记录引用了不存在的 Fact: {missing}")
            completion_id = new_id("completion")
            created_at = now()
            self._connection.execute(
                """INSERT INTO project_completions
                (id, project_id, fact_ids_json, description, worker_name, active, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, NULL)""",
                (
                    completion_id,
                    project_id,
                    stable_json(cleaned_ids),
                    description,
                    worker_name,
                    created_at,
                ),
            )
            self._connection.execute(
                "UPDATE projects SET status = 'completed' WHERE id = ?",
                (project_id,),
            )
            self._clear_reason_lease_locked(project_id, "project_completed")
            completion = {
                "id": completion_id,
                "project_id": project_id,
                "fact_ids": cleaned_ids,
                "description": description,
                "worker_name": worker_name,
                "active": True,
                "created_at": created_at,
                "revoked_at": None,
            }
            self.add_event(project_id, "project.completed", completion)
            self._connection.commit()
            return completion, True
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def list_project_completions(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM project_completions WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._completion_row(row) for row in rows]

    @synchronized
    def get_active_completion(self, project_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """SELECT * FROM project_completions
            WHERE project_id = ? AND active = 1 ORDER BY created_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        return self._completion_row(row) if row is not None else None

    @synchronized
    def reopen_project(self, project_id: str, description: str, creator: str) -> dict[str, Any]:
        """Revoke the active completion and turn external correction into a Fact."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project = self.get_project(project_id)
            completion = self.get_active_completion(project_id)
            description = description.strip()
            creator = creator.strip()
            if project.status != "completed" or completion is None:
                raise ValueError("只有带有效完成记录的 completed 项目可以 reopen")
            if not description or not creator:
                raise ValueError("reopen 需要 description 和 creator")
            revoked_at = now()
            self._connection.execute(
                "UPDATE project_completions SET active = 0, revoked_at = ? WHERE id = ?",
                (revoked_at, completion["id"]),
            )
            self._connection.execute(
                "UPDATE projects SET status = 'running' WHERE id = ?",
                (project_id,),
            )
            self._clear_reason_lease_locked(project_id, "project_reopened")
            evidence_ref = f"external-feedback:{completion['id']}:{new_id('evidence')}"
            self.register_evidence(
                project_id,
                evidence_ref,
                "external_authorized_input",
                metadata={"creator": creator, "revoked_completion_id": completion["id"]},
            )
            feedback, _ = self.add_fact(
                project_id,
                FactCandidate(
                    subject=project.target,
                    predicate="external_feedback",
                    object=description,
                    confidence=1.0,
                    evidence_refs=[evidence_ref],
                    attributes={"pinned": True, "creator": creator},
                ),
            )
            payload = {
                "revoked_completion_id": completion["id"],
                "feedback_fact_id": feedback.id,
                "description": description,
                "creator": creator,
                "revoked_at": revoked_at,
            }
            self.add_event(project_id, "project.reopened", payload)
            result = {
                "project": asdict(self.get_project(project_id)),
                "feedback_fact": asdict(feedback),
                **payload,
            }
            self._connection.commit()
            return result
        except BaseException:
            self._connection.rollback()
            raise

    @synchronized
    def add_fact(self, project_id: str, candidate: FactCandidate) -> tuple[Fact, bool]:
        self.get_project(project_id)
        existing = self._connection.execute(
            "SELECT * FROM facts WHERE project_id = ? AND fact_key = ?", (project_id, candidate.key)
        ).fetchone()
        if existing:
            evidence = sorted(set(json.loads(existing["evidence_json"]) + candidate.evidence_refs))
            confidence = max(existing["confidence"], candidate.confidence)
            attributes = {**json.loads(existing["attributes_json"]), **candidate.attributes}
            self._connection.execute(
                "UPDATE facts SET evidence_json = ?, confidence = ?, attributes_json = ? WHERE id = ?",
                (stable_json(evidence), confidence, stable_json(attributes), existing["id"]),
            )
            self.refresh_fact_importance(project_id)
            return self._row_to_fact(
                self._connection.execute("SELECT * FROM facts WHERE id = ?", (existing["id"],)).fetchone()
            ), False

        fact = Fact(
            subject=candidate.subject,
            predicate=candidate.predicate,
            object=candidate.object,
            confidence=candidate.confidence,
            evidence_refs=candidate.evidence_refs,
            source_intent_id=candidate.source_intent_id,
            attributes=candidate.attributes,
            project_id=project_id,
        )
        self._connection.execute(
            """INSERT INTO facts
            (id, project_id, fact_key, subject, predicate, object, confidence, evidence_json,
             source_intent_id, attributes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fact.id, project_id, candidate.key, fact.subject, fact.predicate, fact.object, fact.confidence,
             stable_json(fact.evidence_refs), fact.source_intent_id, stable_json(fact.attributes), fact.created_at),
        )
        self.add_event(project_id, "fact.created", asdict(fact))
        self.refresh_fact_importance(project_id)
        return self._row_to_fact(
            self._connection.execute("SELECT * FROM facts WHERE id = ?", (fact.id,)).fetchone()
        ), True

    @synchronized
    def list_facts(self, project_id: str) -> list[Fact]:
        rows = self._connection.execute(
            "SELECT * FROM facts WHERE project_id = ? ORDER BY created_at", (project_id,)
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    @synchronized
    def list_facts_since(self, project_id: str, created_after: float) -> list[Fact]:
        rows = self._connection.execute(
            "SELECT * FROM facts WHERE project_id = ? AND created_at > ? ORDER BY created_at",
            (project_id, created_after),
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    @synchronized
    def add_fact_relation(
        self,
        project_id: str,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
        weight: float = 1.0,
    ) -> None:
        self.get_project(project_id)
        source_id, target_id = sorted((source_fact_id, target_fact_id))
        if source_id == target_id:
            return
        self._connection.execute(
            """INSERT INTO fact_relations
            (project_id, source_fact_id, target_fact_id, relation, weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, source_fact_id, target_fact_id, relation)
            DO UPDATE SET weight = MAX(weight, excluded.weight)""",
            (project_id, source_id, target_id, relation, max(0.0, weight), now()),
        )
        self.refresh_fact_importance(project_id)

    @synchronized
    def list_fact_relations(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT source_fact_id, target_fact_id, relation, weight, created_at
            FROM fact_relations WHERE project_id = ? ORDER BY created_at""",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def refresh_fact_importance(self, project_id: str) -> None:
        facts = self.list_facts(project_id)
        if not facts:
            return
        edge_rows = self._connection.execute(
            """SELECT pe.fact_id, i.target_entity
            FROM path_edges pe JOIN intents i ON i.id = pe.intent_id
            WHERE pe.project_id = ?""",
            (project_id,),
        ).fetchall()
        references: dict[str, int] = {}
        branches: dict[str, set[str]] = {}
        for row in edge_rows:
            fact_id = str(row["fact_id"])
            references[fact_id] = references.get(fact_id, 0) + 1
            branches.setdefault(fact_id, set()).add(str(row["target_entity"]))
        relation_rows = self._connection.execute(
            """SELECT source_fact_id, target_fact_id FROM fact_relations
            WHERE project_id = ?""",
            (project_id,),
        ).fetchall()
        relation_counts: dict[str, int] = {}
        for row in relation_rows:
            for fact_id in (str(row["source_fact_id"]), str(row["target_fact_id"])):
                relation_counts[fact_id] = relation_counts.get(fact_id, 0) + 1

        for fact in facts:
            reference_count = references.get(fact.id, 0)
            branch_count = len(branches.get(fact.id, set()))
            relation_count = relation_counts.get(fact.id, 0)
            importance = fact.confidence * 0.25
            importance += min(reference_count, 5) * 0.10
            importance += min(branch_count, 3) * 0.12
            importance += min(relation_count, 5) * 0.05
            if bool(fact.attributes.get("negative")):
                importance += 0.08
            if bool(fact.attributes.get("capability")):
                importance += 0.35
            importance = min(1.0, round(importance, 4))
            pinned = bool(fact.attributes.get("pinned") or fact.attributes.get("capability"))
            state = "pinned" if pinned or fact.memory_state == "pinned" else fact.memory_state
            self._connection.execute(
                """UPDATE facts SET memory_state = ?, structural_importance = ?,
                reference_count = ?, branch_count = ? WHERE id = ? AND project_id = ?""",
                (state, importance, reference_count, branch_count, fact.id, project_id),
            )

    @synchronized
    def set_fact_memory_state(self, project_id: str, fact_id: str, state: str) -> None:
        if state not in {"active", "pinned", "dormant", "revived"}:
            raise ValueError(f"未知 Fact 记忆状态: {state}")
        changed = self._connection.execute(
            "UPDATE facts SET memory_state = ? WHERE id = ? AND project_id = ?",
            (state, fact_id, project_id),
        ).rowcount
        if not changed:
            raise KeyError(fact_id)
        self.add_event(project_id, "fact.memory_state", {"fact_id": fact_id, "state": state})

    @synchronized
    def record_context_usage(
        self,
        project_id: str,
        included_fact_ids: list[str],
        omitted_fact_ids: list[str],
    ) -> None:
        self.refresh_fact_importance(project_id)
        current = now()
        for fact_id in set(included_fact_ids):
            row = self._connection.execute(
                "SELECT memory_state FROM facts WHERE id = ? AND project_id = ?",
                (fact_id, project_id),
            ).fetchone()
            if row is None:
                continue
            state = str(row["memory_state"])
            consumed_revival = state in {"dormant", "revived"}
            next_state = "active" if consumed_revival else state
            self._connection.execute(
                """UPDATE facts SET memory_state = ?, omission_count = 0,
                access_count = access_count + 1, last_accessed_at = ?,
                last_revived_at = CASE WHEN ? THEN ? ELSE last_revived_at END
                WHERE id = ? AND project_id = ?""",
                (next_state, current, int(consumed_revival), current, fact_id, project_id),
            )
            if consumed_revival:
                self.add_event(project_id, "fact.revival_consumed", {"fact_id": fact_id})

        for fact_id in set(omitted_fact_ids) - set(included_fact_ids):
            row = self._connection.execute(
                """SELECT memory_state, structural_importance, omission_count
                FROM facts WHERE id = ? AND project_id = ?""",
                (fact_id, project_id),
            ).fetchone()
            if row is None or row["memory_state"] in {"pinned", "revived"}:
                continue
            omissions = int(row["omission_count"]) + 1
            next_state = "dormant" if omissions >= 2 and float(row["structural_importance"]) < 0.55 else row["memory_state"]
            self._connection.execute(
                "UPDATE facts SET memory_state = ?, omission_count = ? WHERE id = ? AND project_id = ?",
                (next_state, omissions, fact_id, project_id),
            )
            if next_state == "dormant" and row["memory_state"] != "dormant":
                self.add_event(
                    project_id,
                    "fact.dormant",
                    {"fact_id": fact_id, "omission_count": omissions},
                )

    @synchronized
    def revive_related_facts(
        self,
        project_id: str,
        trigger_fact_ids: list[str],
        limit: int = 12,
    ) -> list[str]:
        if not trigger_fact_ids:
            return []
        self.refresh_fact_importance(project_id)
        facts = {fact.id: fact for fact in self.list_facts(project_id)}
        triggers = [facts[fact_id] for fact_id in trigger_fact_ids if fact_id in facts]
        dormant = [fact for fact in facts.values() if fact.memory_state == "dormant"]
        related_pairs = {
            frozenset((item["source_fact_id"], item["target_fact_id"]))
            for item in self.list_fact_relations(project_id)
        }

        def terms(fact: Fact) -> set[str]:
            text = f"{fact.subject} {fact.predicate} {fact.object}".lower()
            return set(re.findall(r"[a-z0-9_./:-]{3,}", text))

        ranked: list[tuple[float, Fact, list[str]]] = []
        for candidate in dormant:
            candidate_terms = terms(candidate)
            best_score = 0.0
            matched_triggers: list[str] = []
            for trigger in triggers:
                score = candidate.structural_importance * 0.35
                if candidate.subject == trigger.subject:
                    score += 0.30
                if candidate.object == trigger.object:
                    score += 0.30
                overlap = candidate_terms & terms(trigger)
                score += min(0.25, len(overlap) * 0.08)
                if frozenset((candidate.id, trigger.id)) in related_pairs:
                    score += 0.45
                if score > best_score:
                    best_score = score
                    matched_triggers = [trigger.id]
            if best_score >= 0.55:
                ranked.append((best_score, candidate, matched_triggers))

        revived = []
        for score, candidate, matched_triggers in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]:
            revived_at = now()
            self._connection.execute(
                """UPDATE facts SET memory_state = 'revived', omission_count = 0,
                last_revived_at = ? WHERE id = ? AND project_id = ?""",
                (revived_at, candidate.id, project_id),
            )
            self.add_event(
                project_id,
                "fact.revived",
                {
                    "fact_id": candidate.id,
                    "trigger_fact_ids": matched_triggers,
                    "score": round(score, 4),
                },
            )
            revived.append(candidate.id)
        return revived

    @synchronized
    def global_audit_candidates(self, project_id: str, limit: int = 20) -> list[str]:
        rows = self._connection.execute(
            """SELECT id FROM facts
            WHERE project_id = ? AND memory_state = 'dormant'
            ORDER BY structural_importance DESC, omission_count DESC, last_accessed_at ASC
            LIMIT ?""",
            (project_id, max(1, min(limit, 100))),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    @synchronized
    def add_intent(self, project_id: str, proposal: IntentProposal, nutrient: float) -> tuple[Intent, bool]:
        project = self.get_project(project_id)
        if project.status != "running":
            raise ValueError(f"项目状态为 {project.status}，不能创建 Intent")
        existing = self._connection.execute(
            "SELECT * FROM intents WHERE project_id = ? AND fingerprint = ?", (project_id, proposal.key)
        ).fetchone()
        if existing:
            return self._row_to_intent(existing), False

        intent = Intent(
            kind=proposal.kind,
            objective=proposal.objective,
            target_entity=proposal.target_entity,
            parent_fact_ids=proposal.parent_fact_ids,
            expected_value=proposal.expected_value,
            novelty=proposal.novelty,
            cost=proposal.cost,
            risk=proposal.risk,
            context=proposal.context,
            provenance=proposal.provenance,
            project_id=project_id,
            fingerprint=proposal.key,
            nutrient=nutrient,
        )
        self._connection.execute(
            """INSERT INTO intents
            (id, project_id, fingerprint, kind, objective, target_entity, parent_fact_ids_json,
             context_json, provenance_json, expected_value, novelty, cost, risk, nutrient, strength, status,
             owner, lease_expires_at, last_heartbeat_at, last_error, attempts, failure_streak,
             retry_not_before, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (intent.id, project_id, intent.fingerprint, intent.kind, intent.objective, intent.target_entity,
             stable_json(intent.parent_fact_ids), stable_json(intent.context), stable_json(intent.provenance), intent.expected_value,
             intent.novelty, intent.cost, intent.risk, intent.nutrient, intent.strength, intent.status,
             intent.owner, intent.lease_expires_at, intent.last_heartbeat_at, intent.last_error,
             intent.attempts, intent.failure_streak, intent.retry_not_before, intent.created_at),
        )
        for fact_id in intent.parent_fact_ids:
            self._connection.execute(
                "INSERT OR IGNORE INTO path_edges VALUES (?, ?, ?, 1.0, ?)",
                (project_id, fact_id, intent.id, now()),
            )
        relation = str(intent.provenance.get("relation", "co_support"))
        for source_id, target_id in combinations(sorted(set(intent.parent_fact_ids)), 2):
            self._connection.execute(
                """INSERT OR IGNORE INTO fact_relations
                (project_id, source_fact_id, target_fact_id, relation, weight, created_at)
                VALUES (?, ?, ?, ?, 1.0, ?)""",
                (project_id, source_id, target_id, relation, now()),
            )
        self.add_event(project_id, "intent.created", asdict(intent))
        self.refresh_fact_importance(project_id)
        return intent, True

    @synchronized
    def list_intents(self, project_id: str, status: str | None = None) -> list[Intent]:
        if status:
            rows = self._connection.execute(
                "SELECT * FROM intents WHERE project_id = ? AND status = ? ORDER BY nutrient DESC, created_at",
                (project_id, status),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at", (project_id,)
            ).fetchall()
        return [self._row_to_intent(row) for row in rows]

    @synchronized
    def list_runnable_intents(self, project_id: str, at: float | None = None) -> list[Intent]:
        """Return pending work whose durable retry gate has opened."""

        current = now() if at is None else float(at)
        rows = self._connection.execute(
            """SELECT * FROM intents
            WHERE project_id = ? AND status = 'pending' AND retry_not_before <= ?
            ORDER BY nutrient DESC, created_at""",
            (project_id, current),
        ).fetchall()
        return [self._row_to_intent(row) for row in rows]

    @synchronized
    def archive_pending_bootstrap_intents(
        self,
        project_id: str,
        reason: str,
    ) -> list[str]:
        """Dormant legacy Bootstrap work when a project uses Reason-first startup.

        The records stay visible in the Blackboard and graph, but they are no
        longer pending work that can block idle detection or consume retries.
        Repeating this operation is intentionally a no-op after the first
        archive so Service and Dispatcher can both enforce the invariant.
        """

        reason = reason.strip() or "bootstrap_unavailable"
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                """SELECT id FROM intents
                WHERE project_id = ? AND kind = 'bootstrap' AND status = 'pending'
                ORDER BY created_at""",
                (project_id,),
            ).fetchall()
            intent_ids = [str(row["id"]) for row in rows]
            if intent_ids:
                placeholders = ",".join("?" for _ in intent_ids)
                self._connection.execute(
                    f"""UPDATE intents SET status = 'dormant', owner = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL, last_error = ?
                    WHERE project_id = ? AND id IN ({placeholders}) AND status = 'pending'""",
                    (reason[:2000], project_id, *intent_ids),
                )
                self.add_event(
                    project_id,
                    "intent.bootstrap_archived",
                    {
                        "intent_ids": intent_ids,
                        "reason": reason,
                        "status": "dormant",
                    },
                )
            self._connection.execute("COMMIT")
            return intent_ids
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    @synchronized
    def archive_pending_intents_for_target(
        self,
        project_id: str,
        target_entity: str,
        reason: str,
    ) -> list[str]:
        """Retire queued branches that still point at a replaced external instance."""

        self.get_project(project_id)
        target_entity = target_entity.strip()
        reason = reason.strip() or "target_replaced"
        if not target_entity:
            return []
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                """SELECT id FROM intents WHERE project_id = ? AND status = 'pending'
                AND target_entity = ? ORDER BY created_at""",
                (project_id, target_entity),
            ).fetchall()
            intent_ids = [str(row["id"]) for row in rows]
            if intent_ids:
                placeholders = ",".join("?" for _ in intent_ids)
                self._connection.execute(
                    f"""UPDATE intents SET status = 'dormant', owner = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL, last_error = ?
                    WHERE project_id = ? AND id IN ({placeholders}) AND status = 'pending'""",
                    (reason[:2000], project_id, *intent_ids),
                )
                self.add_event(
                    project_id,
                    "intent.target_replaced_archived",
                    {
                        "intent_ids": intent_ids,
                        "target_entity": target_entity,
                        "reason": reason,
                        "status": "dormant",
                    },
                )
            self._connection.commit()
            return intent_ids
        except BaseException:
            self._connection.rollback()
            raise

    @staticmethod
    def _retry_delay_seconds(
        failure_streak: int,
        base_seconds: float,
        max_seconds: float,
    ) -> float:
        if base_seconds <= 0:
            return 0.0
        exponent = min(max(0, int(failure_streak) - 1), 30)
        return min(max_seconds, base_seconds * (2 ** exponent))

    def _expire_intent_leases_locked(
        self,
        project_id: str,
        current: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> list[dict[str, Any]]:
        """Move expired leases back through the same durable retry path.

        The caller owns both ``self._lock`` and an open SQLite transaction.
        Returning transition metadata lets the public boundary publish events
        only after that transaction has committed.
        """

        rows = self._connection.execute(
            """SELECT id, attempts, failure_streak, owner FROM intents
            WHERE project_id = ? AND status = 'running' AND lease_expires_at < ?""",
            (project_id, current),
        ).fetchall()
        transitions: list[dict[str, Any]] = []
        for row in rows:
            attempts = int(row["attempts"])
            failure_streak = int(row["failure_streak"]) + 1
            terminal = max_attempts > 0 and attempts >= max_attempts
            delay = 0.0 if terminal else self._retry_delay_seconds(
                failure_streak,
                retry_base_seconds,
                retry_max_seconds,
            )
            retry_not_before = 0.0 if terminal else current + delay
            status = "failed" if terminal else "pending"
            cursor = self._connection.execute(
                """UPDATE intents SET status = ?, owner = NULL, lease_expires_at = NULL,
                last_heartbeat_at = NULL, last_error = 'lease_expired', failure_streak = ?,
                retry_not_before = ?
                WHERE id = ? AND project_id = ? AND status = 'running' AND lease_expires_at < ?""",
                (
                    status,
                    failure_streak,
                    retry_not_before,
                    row["id"],
                    project_id,
                    current,
                ),
            )
            if cursor.rowcount:
                transitions.append(
                    {
                        "intent_id": str(row["id"]),
                        "owner": str(row["owner"] or ""),
                        "status": status,
                        "failure_streak": failure_streak,
                        "retry_not_before": retry_not_before,
                        "retry_delay_seconds": delay,
                        "error": "lease_expired",
                        "trigger": "lease_expired",
                    }
                )
        return transitions

    def _record_intent_retry_transition(self, project_id: str, transition: dict[str, Any]) -> None:
        status = str(transition["status"])
        if status == "failed":
            kind = "intent.retry_exhausted"
        elif transition.get("penalized", True):
            kind = "intent.retry_scheduled"
        else:
            kind = "intent.released"
        self.add_event(project_id, kind, transition)

    @synchronized
    def claim_next_intent(
        self,
        project_id: str,
        worker_id: str,
        lease_seconds: float = 120,
        modes: set[str] | None = None,
        max_attempts: int = 0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> Intent | None:
        """Atomically claim the highest-value Intent a currently available Worker can run."""

        modes = set(modes or {"bootstrap", "explore"})
        if not modes:
            return None
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if max_attempts < 0:
            raise ValueError("max_attempts 不能小于 0")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Intent 重试退避配置无效")
        transitions: list[dict[str, Any]] = []
        claimed: Intent | None = None
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                project_row = self._connection.execute(
                    "SELECT status FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if project_row is None:
                    raise KeyError(project_id)
                if project_row["status"] != "running":
                    self._connection.execute("COMMIT")
                    return None
                current = now()
                transitions = self._expire_intent_leases_locked(
                    project_id,
                    current,
                    max_attempts,
                    retry_base_seconds,
                    retry_max_seconds,
                )
                mode_clause = ""
                if modes == {"bootstrap"}:
                    mode_clause = "AND kind = 'bootstrap'"
                elif modes == {"explore"}:
                    mode_clause = "AND kind != 'bootstrap'"
                row = self._connection.execute(
                    f"""SELECT id FROM intents WHERE project_id = ? AND status = 'pending'
                    AND retry_not_before <= ? {mode_clause}
                    ORDER BY nutrient DESC, created_at LIMIT 1""",
                    (project_id, current),
                ).fetchone()
                if row is not None:
                    expires = current + lease_seconds
                    cursor = self._connection.execute(
                        """UPDATE intents SET status = 'running', owner = ?, lease_expires_at = ?,
                        last_heartbeat_at = ?, attempts = attempts + 1
                        WHERE id = ? AND status = 'pending' AND retry_not_before <= ?""",
                        (worker_id, expires, current, row["id"], current),
                    )
                    if cursor.rowcount:
                        claimed_row = self._connection.execute(
                            "SELECT * FROM intents WHERE id = ?", (row["id"],)
                        ).fetchone()
                        claimed = self._row_to_intent(claimed_row)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        for transition in transitions:
            self._record_intent_retry_transition(project_id, transition)
        return claimed

    @synchronized
    def claim_intent(
        self,
        project_id: str,
        intent_id: str,
        worker_id: str,
        lease_seconds: float = 120,
        max_attempts: int = 0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> Intent | None:
        """Atomically lease one already selected pending Intent.

        Dispatcher selects an available Worker before calling this method.  The
        conditional update is the ownership boundary: an attempt is counted
        only when that selected pending Intent was actually leased.
        """

        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if max_attempts < 0:
            raise ValueError("max_attempts 不能小于 0")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Intent 重试退避配置无效")

        transitions: list[dict[str, Any]] = []
        claimed: Intent | None = None
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            project_row = self._connection.execute(
                "SELECT status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise KeyError(project_id)
            if project_row["status"] != "running":
                self._connection.execute("COMMIT")
                return None

            current = now()
            transitions = self._expire_intent_leases_locked(
                project_id,
                current,
                max_attempts,
                retry_base_seconds,
                retry_max_seconds,
            )
            cursor = self._connection.execute(
                """UPDATE intents SET status = 'running', owner = ?, lease_expires_at = ?,
                last_heartbeat_at = ?, attempts = attempts + 1
                WHERE id = ? AND project_id = ? AND status = 'pending' AND retry_not_before <= ?""",
                (
                    worker_id,
                    current + lease_seconds,
                    current,
                    intent_id,
                    project_id,
                    current,
                ),
            )
            if cursor.rowcount == 1:
                row = self._connection.execute(
                    "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                    (intent_id, project_id),
                ).fetchone()
                claimed = self._row_to_intent(row)
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        for transition in transitions:
            self._record_intent_retry_transition(project_id, transition)
        return claimed

    @synchronized
    def renew_intent_lease(
        self,
        project_id: str,
        intent_id: str,
        owner: str,
        lease_seconds: float,
    ) -> bool:
        current = now()
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE intents SET lease_expires_at = ?, last_heartbeat_at = ?
                WHERE id = ? AND project_id = ? AND status = 'running' AND owner = ?""",
                (current + lease_seconds, current, intent_id, project_id, owner),
            )
            return cursor.rowcount == 1

    @synchronized
    def recover_expired_intents(
        self,
        project_id: str,
        max_attempts: int = 0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> dict[str, list[str]]:
        """Recover leases left behind by a crashed Dispatcher before new work is scheduled."""

        if max_attempts < 0:
            raise ValueError("max_attempts 不能小于 0")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Intent 重试退避配置无效")
        transitions: list[dict[str, Any]] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                transitions = self._expire_intent_leases_locked(
                    project_id,
                    now(),
                    max_attempts,
                    retry_base_seconds,
                    retry_max_seconds,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        for transition in transitions:
            self._record_intent_retry_transition(project_id, transition)
        return {
            "recovered": [item["intent_id"] for item in transitions if item["status"] == "pending"],
            "failed": [item["intent_id"] for item in transitions if item["status"] == "failed"],
        }

    @synchronized
    def release_intent(
        self,
        project_id: str,
        intent_id: str,
        owner: str,
        error_message: str,
        max_attempts: int = 0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
        *,
        penalize: bool = True,
    ) -> str:
        """Release a lease while preserving failure evidence and retry timing.

        Ordinary Worker failures increment a durable streak and re-enter the
        pending queue only after exponential backoff.  Deliberate Dispatcher
        cancellation releases immediately without counting as a failure.
        ``max_attempts=0`` means no terminal automatic attempt cap.
        """

        if max_attempts < 0:
            raise ValueError("max_attempts 不能小于 0")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Intent 重试退避配置无效")

        transition: dict[str, Any] | None = None
        with self._lock:
            row = self._connection.execute(
                """SELECT attempts, failure_streak FROM intents
                WHERE id = ? AND project_id = ? AND owner = ? AND status = 'running'""",
                (intent_id, project_id, owner),
            ).fetchone()
            if row is None:
                return "lost"
            current = now()
            failure_streak = int(row["failure_streak"]) + int(penalize)
            terminal = penalize and max_attempts > 0 and int(row["attempts"]) >= max_attempts
            delay = (
                0.0
                if not penalize or terminal
                else self._retry_delay_seconds(failure_streak, retry_base_seconds, retry_max_seconds)
            )
            retry_not_before = 0.0 if not penalize or terminal else current + delay
            status = "failed" if terminal else "pending"
            self._connection.execute(
                """UPDATE intents SET status = ?, owner = NULL, lease_expires_at = NULL,
                last_heartbeat_at = NULL, last_error = ?, failure_streak = ?, retry_not_before = ?
                WHERE id = ? AND project_id = ? AND owner = ?""",
                (
                    status,
                    error_message[:2000],
                    failure_streak,
                    retry_not_before,
                    intent_id,
                    project_id,
                    owner,
                ),
            )
            transition = {
                "intent_id": intent_id,
                "owner": owner,
                "status": status,
                "failure_streak": failure_streak,
                "retry_not_before": retry_not_before,
                "retry_delay_seconds": delay,
                "error": error_message[:500],
                "penalized": penalize,
                "trigger": "worker_failure" if penalize else "lease_release",
            }
        self._record_intent_retry_transition(project_id, transition)
        return status

    @synchronized
    def retry_intent(
        self,
        project_id: str,
        intent_id: str,
        reason: str = "manual_retry",
    ) -> Intent:
        """Immediately reopen a failed or delayed Intent without erasing its history."""

        project = self.get_project(project_id)
        if project.status == "deleting":
            raise ValueError("项目正在删除")
        row = self._connection.execute(
            "SELECT status, retry_not_before, failure_streak FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        previous_status = str(row["status"])
        if previous_status == "running":
            raise ValueError("运行中的 Intent 不能立即重试")
        if previous_status not in {"pending", "failed"}:
            raise ValueError(f"Intent 状态为 {previous_status}，不能重试")
        self._connection.execute(
            """UPDATE intents SET status = 'pending', owner = NULL, lease_expires_at = NULL,
            last_heartbeat_at = NULL, retry_not_before = 0.0
            WHERE id = ? AND project_id = ?""",
            (intent_id, project_id),
        )
        intent = self.get_intent(project_id, intent_id)
        self.add_event(
            project_id,
            "intent.retry_forced",
            {
                "intent_id": intent_id,
                "previous_status": previous_status,
                "previous_retry_not_before": float(row["retry_not_before"]),
                "failure_streak": int(row["failure_streak"]),
                "reason": reason[:500],
            },
        )
        return intent

    @synchronized
    def reopen_failed_intents(
        self,
        project_id: str,
        reason: str = "cairn_open_queue",
    ) -> list[str]:
        """Restore capped legacy failures to Cairn's open claim queue.

        Attempts and failure evidence are deliberately retained. Only the
        terminal scheduling fields are cleared so a running Dispatcher can
        claim the same Blackboard Intent again.
        """

        self.get_project(project_id)
        rows = self._connection.execute(
            """SELECT id, attempts, failure_streak, last_error
            FROM intents WHERE project_id = ? AND status = 'failed'
            ORDER BY created_at, id""",
            (project_id,),
        ).fetchall()
        reopened: list[str] = []
        for row in rows:
            intent_id = str(row["id"])
            changed = self._connection.execute(
                """UPDATE intents SET status = 'pending', owner = NULL,
                lease_expires_at = NULL, last_heartbeat_at = NULL,
                retry_not_before = 0.0
                WHERE id = ? AND project_id = ? AND status = 'failed'""",
                (intent_id, project_id),
            ).rowcount
            if not changed:
                continue
            reopened.append(intent_id)
            self.add_event(
                project_id,
                "intent.retry_reopened",
                {
                    "intent_id": intent_id,
                    "reason": reason[:500],
                    "attempts": int(row["attempts"]),
                    "failure_streak": int(row["failure_streak"]),
                    "last_error": str(row["last_error"] or "")[:500],
                },
            )
        return reopened

    @synchronized
    def finish_intent(
        self,
        project_id: str,
        intent_id: str,
        status: str = "completed",
        owner: str | None = None,
    ) -> bool:
        ownership_clause = " AND owner = ?" if owner is not None else ""
        clear_failure_state = status in {"completed", "dormant"}
        parameters: tuple[Any, ...] = (
            status,
            int(clear_failure_state),
            int(clear_failure_state),
            int(clear_failure_state),
            intent_id,
            project_id,
        )
        if owner is not None:
            parameters += (owner,)
        cursor = self._connection.execute(
            f"""UPDATE intents SET status = ?, owner = NULL, lease_expires_at = NULL,
            last_heartbeat_at = NULL,
            failure_streak = CASE WHEN ? THEN 0 ELSE failure_streak END,
            retry_not_before = CASE WHEN ? THEN 0.0 ELSE retry_not_before END,
            last_error = CASE WHEN ? THEN '' ELSE last_error END
            WHERE id = ? AND project_id = ?{ownership_clause}""",
            parameters,
        )
        if cursor.rowcount:
            self.add_event(project_id, f"intent.{status}", {"intent_id": intent_id})
        return cursor.rowcount == 1

    @synchronized
    def get_intent(self, project_id: str, intent_id: str) -> Intent:
        row = self._connection.execute(
            "SELECT * FROM intents WHERE project_id = ? AND id = ?",
            (project_id, intent_id),
        ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return self._row_to_intent(row)

    @synchronized
    def update_intent_strength(self, project_id: str, intent_id: str, reward: float) -> None:
        self._connection.execute(
            """UPDATE intents SET strength = MAX(0.1, strength + ?), nutrient = MAX(0.0, nutrient + ?)
            WHERE id = ? AND project_id = ?""",
            (reward, reward * 10, intent_id, project_id),
        )
        self._connection.execute(
            """UPDATE path_edges SET strength = MAX(0.1, strength + ?), updated_at = ?
            WHERE project_id = ? AND intent_id = ?""",
            (reward, now(), project_id, intent_id),
        )

    @synchronized
    def decay_paths(self, project_id: str, factor: float = 0.95) -> None:
        self._connection.execute(
            "UPDATE intents SET strength = MAX(0.1, strength * ?) WHERE project_id = ? AND status != 'running'",
            (factor, project_id),
        )
        self._connection.execute(
            "UPDATE path_edges SET strength = MAX(0.1, strength * ?), updated_at = ? WHERE project_id = ?",
            (factor, now(), project_id),
        )

    @synchronized
    def get_action(self, project_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        action_key = fingerprint(tool, arguments)
        row = self._connection.execute(
            "SELECT * FROM actions WHERE project_id = ? AND action_key = ?", (project_id, action_key)
        ).fetchone()
        if row is None or row["status"] != "completed":
            return None
        return json.loads(row["result_json"])

    @synchronized
    def list_actions(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT id, action_key, tool, arguments_json, status, result_json, started_at, finished_at
            FROM actions WHERE project_id = ? ORDER BY started_at DESC LIMIT ?""",
            (project_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "action_key": row["action_key"],
                "tool": row["tool"],
                "arguments": json.loads(row["arguments_json"]),
                "status": row["status"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ]

    @synchronized
    def begin_action(self, project_id: str, tool: str, arguments: dict[str, Any]) -> str:
        self.get_project(project_id)
        action_key = fingerprint(tool, arguments)
        existing = self._connection.execute(
            "SELECT id, status FROM actions WHERE project_id = ? AND action_key = ?",
            (project_id, action_key),
        ).fetchone()
        if existing is not None:
            self._connection.execute(
                """UPDATE actions SET status = 'running', result_json = NULL,
                started_at = ?, finished_at = NULL WHERE id = ?""",
                (now(), existing["id"]),
            )
            return str(existing["id"])
        action_id = new_id("action")
        self._connection.execute(
            """INSERT INTO actions
            (id, project_id, action_key, tool, arguments_json, status, result_json, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, 'running', NULL, ?, NULL)""",
            (action_id, project_id, action_key, tool, stable_json(arguments), now()),
        )
        return action_id

    @synchronized
    def complete_action(self, action_id: str, result: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE actions SET status = 'completed', result_json = ?, finished_at = ? WHERE id = ?",
            (stable_json(result), now(), action_id),
        )

    @synchronized
    def fail_action(self, action_id: str, error: str) -> None:
        self._connection.execute(
            "UPDATE actions SET status = 'failed', result_json = ?, finished_at = ? WHERE id = ?",
            (stable_json({"error": error}), now(), action_id),
        )

    @synchronized
    def register_evidence(
        self,
        project_id: str,
        evidence_ref: str,
        kind: str,
        source_action_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.get_project(project_id)
        evidence_ref = evidence_ref.strip()
        if not evidence_ref:
            raise ValueError("evidence_ref 不能为空")
        self._connection.execute(
            """INSERT INTO evidence_records
            (project_id, evidence_ref, kind, source_action_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, evidence_ref) DO UPDATE SET
                kind = excluded.kind,
                source_action_id = COALESCE(excluded.source_action_id, evidence_records.source_action_id),
                metadata_json = excluded.metadata_json""",
            (project_id, evidence_ref, kind, source_action_id, stable_json(metadata or {}), now()),
        )

    @synchronized
    def evidence_exists(self, project_id: str, evidence_ref: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM evidence_records WHERE project_id = ? AND evidence_ref = ?",
            (project_id, evidence_ref),
        ).fetchone()
        return row is not None

    @synchronized
    def get_evidence(self, project_id: str, evidence_ref: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """SELECT evidence_ref, kind, source_action_id, metadata_json, created_at
            FROM evidence_records WHERE project_id = ? AND evidence_ref = ?""",
            (project_id, evidence_ref),
        ).fetchone()
        if row is None:
            return None
        return {
            "evidence_ref": row["evidence_ref"],
            "kind": row["kind"],
            "source_action_id": row["source_action_id"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @synchronized
    def list_evidence(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT evidence_ref, kind, source_action_id, metadata_json, created_at
            FROM evidence_records WHERE project_id = ? ORDER BY created_at DESC LIMIT ?""",
            (project_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [
            {
                "evidence_ref": row["evidence_ref"],
                "kind": row["kind"],
                "source_action_id": row["source_action_id"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @synchronized
    def add_hypothesis(
        self,
        project_id: str,
        candidate: HypothesisCandidate,
    ) -> tuple[Hypothesis, bool]:
        self.get_project(project_id)
        existing = self._connection.execute(
            """SELECT * FROM hypotheses
            WHERE project_id = ? AND statement = ? AND source_intent_id IS ?""",
            (project_id, candidate.statement, candidate.source_intent_id),
        ).fetchone()
        if existing:
            return self._row_to_hypothesis(existing), False
        hypothesis = Hypothesis(
            statement=candidate.statement,
            supporting_fact_ids=candidate.supporting_fact_ids,
            evidence_refs=candidate.evidence_refs,
            confidence=max(0.0, min(1.0, candidate.confidence)),
            next_validation=candidate.next_validation,
            source_intent_id=candidate.source_intent_id,
            project_id=project_id,
        )
        self._connection.execute(
            """INSERT INTO hypotheses
            (id, project_id, statement, supporting_fact_ids_json, evidence_refs_json,
             confidence, next_validation, source_intent_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hypothesis.id,
                project_id,
                hypothesis.statement,
                stable_json(hypothesis.supporting_fact_ids),
                stable_json(hypothesis.evidence_refs),
                hypothesis.confidence,
                hypothesis.next_validation,
                hypothesis.source_intent_id,
                hypothesis.status,
                hypothesis.created_at,
            ),
        )
        self.add_event(project_id, "hypothesis.created", asdict(hypothesis))
        return hypothesis, True

    @synchronized
    def list_hypotheses(self, project_id: str, status: str | None = None) -> list[Hypothesis]:
        if status:
            rows = self._connection.execute(
                "SELECT * FROM hypotheses WHERE project_id = ? AND status = ? ORDER BY created_at",
                (project_id, status),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM hypotheses WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [self._row_to_hypothesis(row) for row in rows]

    @synchronized
    def list_hypotheses_for_intent(self, project_id: str, intent_id: str) -> list[Hypothesis]:
        """Return the durable inquiry records created for one branch Intent."""

        rows = self._connection.execute(
            """SELECT * FROM hypotheses WHERE project_id = ? AND source_intent_id = ?
            ORDER BY created_at, id""",
            (project_id, intent_id),
        ).fetchall()
        return [self._row_to_hypothesis(row) for row in rows]

    @synchronized
    def update_hypothesis_status(
        self,
        project_id: str,
        hypothesis_id: str,
        status: str,
        *,
        next_validation: str | None = None,
    ) -> bool:
        """Advance a branch state while retaining its original evidence trail."""

        allowed = {"open", "testing", "supported", "inconclusive", "blocked", "retired"}
        if status not in allowed:
            raise ValueError(f"invalid hypothesis status: {status}")
        row = self._connection.execute(
            "SELECT * FROM hypotheses WHERE project_id = ? AND id = ?",
            (project_id, hypothesis_id),
        ).fetchone()
        if row is None:
            return False
        updated_next = row["next_validation"] if next_validation is None else next_validation.strip()
        self._connection.execute(
            """UPDATE hypotheses SET status = ?, next_validation = ?
            WHERE project_id = ? AND id = ?""",
            (status, updated_next, project_id, hypothesis_id),
        )
        self.add_event(
            project_id,
            "hypothesis.status_changed",
            {
                "hypothesis_id": hypothesis_id,
                "status": status,
                "next_validation": updated_next,
            },
        )
        return True

    @synchronized
    def start_worker_run(
        self,
        run_id: str,
        project_id: str,
        intent_id: str,
        mode: str,
        context_manifest: dict[str, Any],
        started_at: float,
        worker_name: str = "",
        owner_token: str = "",
    ) -> None:
        self.get_project(project_id)
        self._connection.execute(
            """INSERT INTO worker_runs
            (id, project_id, intent_id, mode, worker_name, owner_token, status, context_manifest_json,
             activity_json, errors_json, model_usage_json, model_session_json, validation_json, tool_calls,
             progress_score, stop_reason, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, '[]', '[]', '{}', '{}', '{}', 0, 0.0, '', ?, NULL)""",
            (
                run_id,
                project_id,
                intent_id,
                mode,
                worker_name,
                owner_token,
                stable_json(context_manifest),
                started_at,
            ),
        )

    @synchronized
    def complete_worker_run(self, report: PseudopodReport) -> None:
        self._connection.execute(
            """UPDATE worker_runs SET status = ?, activity_json = ?, errors_json = ?,
            model_usage_json = ?, model_session_json = ?, tool_calls = ?, progress_score = ?, stop_reason = ?,
            finished_at = ? WHERE id = ? AND status = 'running'""",
            (
                report.status,
                stable_json(report.activity_summary),
                stable_json(report.errors),
                stable_json(report.model_usage),
                stable_json(report.model_session),
                report.tool_calls,
                report.progress_score,
                report.stop_reason,
                report.finished_at,
                report.pseudopod_id,
            ),
        )

    @synchronized
    def annotate_latest_worker_run_error(
        self,
        project_id: str,
        intent_id: str,
        error_message: str,
    ) -> bool:
        """Append a dispatcher-classified error to the latest run for an Intent.

        Native failures are persisted before the Dispatcher can classify them.
        Keeping the classified message in ``worker_runs.errors_json`` makes the
        Inspector show the same actionable category as the Intent record.
        """

        row = self._connection.execute(
            """SELECT id, errors_json FROM worker_runs
            WHERE project_id = ? AND intent_id = ?
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC LIMIT 1""",
            (project_id, intent_id),
        ).fetchone()
        if row is None:
            return False
        try:
            errors = json.loads(row["errors_json"])
        except (TypeError, json.JSONDecodeError):
            errors = []
        if not isinstance(errors, list):
            errors = [str(errors)]
        message = str(error_message)[:2000]
        if message not in errors:
            errors.append(message)
        self._connection.execute(
            "UPDATE worker_runs SET errors_json = ? WHERE id = ?",
            (stable_json(errors), row["id"]),
        )
        return True

    @synchronized
    def fail_worker_run(self, run_id: str, error_message: str) -> None:
        self.fail_worker_run_if_running(run_id, error_message)

    @synchronized
    def fail_worker_run_if_running(
        self,
        run_id: str,
        error_message: str,
        stop_reason: str = "unhandled_runtime_error",
    ) -> bool:
        row = self._connection.execute(
            "SELECT errors_json FROM worker_runs WHERE id = ? AND status = 'running'",
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            errors = json.loads(row["errors_json"])
        except json.JSONDecodeError:
            errors = []
        if not isinstance(errors, list):
            errors = [str(errors)]
        if error_message not in errors:
            errors.append(error_message)
        self._connection.execute(
            """UPDATE worker_runs SET status = 'failed', errors_json = ?,
            stop_reason = ?, finished_at = ? WHERE id = ? AND status = 'running'""",
            (stable_json(errors), stop_reason[:2000], now(), run_id),
        )
        return True

    @synchronized
    def fail_running_worker_runs_for_owner(
        self,
        project_id: str,
        intent_id: str | None,
        owner_token: str,
        error_message: str,
        stop_reason: str = "dispatcher_abort",
    ) -> list[str]:
        intent_clause = "AND intent_id = ?" if intent_id is not None else ""
        parameters: tuple[Any, ...] = (
            (project_id, intent_id, owner_token)
            if intent_id is not None
            else (project_id, owner_token)
        )
        rows = self._connection.execute(
            f"""SELECT id, intent_id, errors_json FROM worker_runs
            WHERE project_id = ? {intent_clause} AND owner_token = ? AND status = 'running'""",
            parameters,
        ).fetchall()
        run_ids: list[str] = []
        for row in rows:
            try:
                errors = json.loads(row["errors_json"])
            except json.JSONDecodeError:
                errors = []
            if not isinstance(errors, list):
                errors = [str(errors)]
            errors.append(error_message)
            self._connection.execute(
                """UPDATE worker_runs SET status = 'failed', errors_json = ?,
                stop_reason = ?, finished_at = ? WHERE id = ?""",
                (stable_json(errors), stop_reason[:2000], now(), row["id"]),
            )
            run_ids.append(str(row["id"]))
        if run_ids:
            self.add_event(
                project_id,
                "worker.running_run_failed",
                {
                    "intent_id": intent_id,
                    "owner_token": owner_token,
                    "run_ids": run_ids,
                    "error": error_message[:500],
                    "stop_reason": stop_reason,
                },
            )
        return run_ids

    @synchronized
    def fail_stale_worker_runs(
        self,
        project_id: str,
        older_than: float,
        error_message: str,
        stop_reason: str = "stale_worker_run",
    ) -> list[str]:
        rows = self._connection.execute(
            """SELECT id, intent_id, errors_json FROM worker_runs
            WHERE project_id = ? AND status = 'running' AND started_at <= ?""",
            (project_id, older_than),
        ).fetchall()
        run_ids: list[str] = []
        for row in rows:
            try:
                errors = json.loads(row["errors_json"])
            except json.JSONDecodeError:
                errors = []
            if not isinstance(errors, list):
                errors = [str(errors)]
            errors.append(error_message)
            self._connection.execute(
                """UPDATE worker_runs SET status = 'failed', errors_json = ?,
                stop_reason = ?, finished_at = ? WHERE id = ?""",
                (stable_json(errors), stop_reason[:2000], now(), row["id"]),
            )
            run_ids.append(str(row["id"]))
        if run_ids:
            self.add_event(
                project_id,
                "worker.stale_runs_failed",
                {
                    "run_ids": run_ids,
                    "older_than": older_than,
                    "error": error_message[:500],
                    "stop_reason": stop_reason,
                },
            )
        return run_ids

    @synchronized
    def record_worker_validation(self, run_id: str, validation: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE worker_runs SET validation_json = ? WHERE id = ?",
            (stable_json(validation), run_id),
        )

    @synchronized
    def list_worker_runs(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM worker_runs WHERE project_id = ? ORDER BY started_at DESC LIMIT ?",
            (project_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "intent_id": row["intent_id"],
                "mode": row["mode"],
                "worker_name": row["worker_name"],
                "owner_token": row["owner_token"],
                "status": row["status"],
                "context_manifest": json.loads(row["context_manifest_json"]),
                "activity": json.loads(row["activity_json"]),
                "errors": json.loads(row["errors_json"]),
                "model_usage": json.loads(row["model_usage_json"]),
                "model_session": json.loads(row["model_session_json"]),
                "validation": json.loads(row["validation_json"]),
                "tool_calls": row["tool_calls"],
                "progress_score": row["progress_score"],
                "stop_reason": row["stop_reason"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ]

    @synchronized
    def add_event(self, project_id: str, kind: str, payload: dict[str, Any]) -> bool:
        # Event logging is best-effort after a final delete.  This lets a
        # cancelled Worker finish its local error handling without recreating
        # an orphan event row.
        exists = self._connection.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if exists is None:
            return False
        self._connection.execute(
            "INSERT INTO events (project_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (project_id, kind, stable_json(payload), now()),
        )
        return True

    @synchronized
    def list_events(
        self,
        project_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one stable, forward-only page of project activity."""

        cursor = max(0, int(after_id))
        page_size = max(1, min(int(limit), 1_000))
        rows = self._connection.execute(
            """SELECT id, kind, payload_json, created_at
            FROM events WHERE project_id = ? AND id > ?
            ORDER BY id ASC LIMIT ?""",
            (project_id, cursor, page_size),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "payload": json.loads(row["payload_json"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _normalize_hint_content(content: str) -> str:
        return " ".join(str(content).strip().split())

    @synchronized
    def add_hint(
        self,
        project_id: str,
        content: str,
        creator: str = "human",
        *,
        record_duplicate: bool = True,
    ) -> tuple[Hint, bool]:
        """Append one human Hint without changing the project's declared scope.

        A normalized-content key makes repeated submissions idempotent within a
        project.  The original trimmed text is retained for display and model
        context, while the event stream tells the Dispatcher that new judgment
        is available.
        """

        self.get_project(project_id)
        text = str(content).strip()
        if not text:
            raise ValueError("Hint content 不能为空")
        author = str(creator).strip() or "human"
        hint_key = fingerprint(self._normalize_hint_content(text))
        existing = self._connection.execute(
            "SELECT * FROM hints WHERE project_id = ? AND hint_key = ?",
            (project_id, hint_key),
        ).fetchone()
        if existing is not None:
            hint = self._row_to_hint(existing)
            if record_duplicate:
                self.add_event(
                    project_id,
                    "hint.duplicate_ignored",
                    {"hint_id": hint.id, "creator": author},
                )
            return hint, False

        hint = Hint(
            project_id=project_id,
            content=text,
            creator=author,
        )
        self._connection.execute(
            """INSERT INTO hints (id, project_id, hint_key, content, creator, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (hint.id, hint.project_id, hint_key, hint.content, hint.creator, hint.created_at),
        )
        self.add_event(project_id, "hint.created", asdict(hint))
        return hint, True

    @synchronized
    def ensure_scope_hints(self, project_id: str) -> list[Hint]:
        """Import legacy/create-time ``scope.hints`` into the durable Hint log.

        Scope remains untouched so existing callers keep their original project
        declaration.  The unique hint key makes this migration safe on every
        service registration and API read path.
        """

        project = self.get_project(project_id)
        imported: list[Hint] = []
        raw_hints = project.scope.get("hints", [])
        if not isinstance(raw_hints, list):
            return imported
        for raw_hint in raw_hints:
            if isinstance(raw_hint, dict):
                content = raw_hint.get("content", raw_hint.get("text", ""))
                creator = raw_hint.get("creator", "project_scope")
            else:
                content = raw_hint
                creator = "project_scope"
            if not str(content).strip():
                continue
            hint, created = self.add_hint(
                project_id,
                str(content),
                str(creator),
                record_duplicate=False,
            )
            if created:
                imported.append(hint)
        return imported

    @synchronized
    def list_hints(self, project_id: str) -> list[Hint]:
        self.get_project(project_id)
        rows = self._connection.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [self._row_to_hint(row) for row in rows]

    @synchronized
    def list_hints_since(self, project_id: str, created_after: float) -> list[Hint]:
        self.get_project(project_id)
        rows = self._connection.execute(
            """SELECT * FROM hints WHERE project_id = ? AND created_at > ?
            ORDER BY created_at, id""",
            (project_id, float(created_after)),
        ).fetchall()
        return [self._row_to_hint(row) for row in rows]

    @synchronized
    def latest_event_id(self, project_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(id), 0) AS value FROM events WHERE project_id = ?", (project_id,)
        ).fetchone()
        return int(row["value"])

    @staticmethod
    def _core_fact_kind(fact: Fact) -> str:
        """Classify the two project anchors without changing their stored Fact form."""

        role = str(fact.attributes.get("role", "")).strip().lower()
        if role in {"origin", "goal"}:
            return role
        if fact.predicate == "project_origin":
            return "origin"
        if fact.predicate == "project_goal":
            return "goal"
        return "fact"

    @synchronized
    def core_graph_projection(self, project_id: str) -> dict[str, Any]:
        """Return the compact Cairn causal graph without Slime implementation data.

        Facts are the durable causal nodes.  Open work remains an Intent node;
        an Intent that produced one or more Facts becomes labelled Fact-to-Fact
        transitions.  Completion is the special transition into Goal.  Hints
        remain core project artifacts but deliberately stay outside the causal
        node/edge layout, matching Cairn's Hints side panel.
        """

        self.get_project(project_id)
        facts = self.list_facts(project_id)
        intents = self.list_intents(project_id)
        hints = self.list_hints(project_id)
        completion = self.get_active_completion(project_id)

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        fact_node_ids: dict[str, str] = {}
        fact_kinds: dict[str, str] = {}
        origin_node_id: str | None = None
        goal_node_id: str | None = None

        for fact in facts:
            kind = self._core_fact_kind(fact)
            node_id = f"fact:{fact.id}"
            fact_node_ids[fact.id] = node_id
            fact_kinds[fact.id] = kind
            if kind == "origin":
                origin_node_id = node_id
            elif kind == "goal":
                goal_node_id = node_id
            label = fact.object if kind in {"origin", "goal"} else f"{fact.predicate}: {fact.object}"
            nodes.append(
                {
                    "id": node_id,
                    "entity_id": fact.id,
                    "kind": kind,
                    "type": kind,
                    "label": label,
                    "created_at": fact.created_at,
                }
            )

        intent_by_id = {intent.id: intent for intent in intents}
        output_fact_ids: dict[str, list[str]] = {}
        for fact in facts:
            intent_id = str(fact.source_intent_id or "").strip()
            if not intent_id or intent_id not in intent_by_id:
                continue
            # Origin and Goal are anchors, never the product of an ordinary
            # causal transition in the core view.
            if fact_kinds.get(fact.id) in {"origin", "goal"}:
                continue
            output_fact_ids.setdefault(intent_id, []).append(fact.id)

        for intent in intents:
            output_ids = output_fact_ids.get(intent.id, [])
            source_ids = [
                fact_id
                for fact_id in dict.fromkeys(intent.parent_fact_ids)
                if fact_id in fact_node_ids
            ]
            if intent.kind == "bootstrap" and not source_ids and origin_node_id is not None:
                source_ids = [next(
                    fact_id for fact_id, node_id in fact_node_ids.items() if node_id == origin_node_id
                )]

            if output_ids and source_ids:
                for source_id in source_ids:
                    for output_id in output_ids:
                        edges.append(
                            {
                                "id": f"intent:{intent.id}:{source_id}:{output_id}",
                                "kind": "intent",
                                "type": "intent",
                                "state": "concluded",
                                "source": fact_node_ids[source_id],
                                "target": fact_node_ids[output_id],
                                "intent_id": intent.id,
                                "intent_kind": intent.kind,
                                "label": intent.objective,
                                "created_at": intent.created_at,
                            }
                        )
                continue

            # Cairn displays unresolved work as a node.  This also preserves
            # source-less work instead of silently dropping it from the graph.
            intent_node_id = f"intent:{intent.id}"
            nodes.append(
                {
                    "id": intent_node_id,
                    "entity_id": intent.id,
                    "kind": "intent",
                    "type": "intent",
                    "state": intent.status,
                    "label": "Bootstrap" if intent.kind == "bootstrap" else intent.objective,
                    "intent_kind": intent.kind,
                    "created_at": intent.created_at,
                }
            )
            for source_id in source_ids:
                edges.append(
                    {
                        "id": f"intent-input:{intent.id}:{source_id}",
                        "kind": "intent_input",
                        "type": "intent",
                        "state": intent.status,
                        "source": fact_node_ids[source_id],
                        "target": intent_node_id,
                        "intent_id": intent.id,
                        "intent_kind": intent.kind,
                        "label": intent.objective,
                    }
                )
            for output_id in output_ids:
                edges.append(
                    {
                        "id": f"intent-output:{intent.id}:{output_id}",
                        "kind": "intent_output",
                        "type": "intent",
                        "state": "concluded",
                        "source": intent_node_id,
                        "target": fact_node_ids[output_id],
                        "intent_id": intent.id,
                        "intent_kind": intent.kind,
                        "label": intent.objective,
                    }
                )
            if intent.kind == "bootstrap" and goal_node_id is not None:
                edges.append(
                    {
                        "id": f"bootstrap-scope:{intent.id}",
                        "kind": "bootstrap_scope",
                        "type": "scope",
                        "state": intent.status,
                        "source": intent_node_id,
                        "target": goal_node_id,
                        "intent_id": intent.id,
                        "visual_only": True,
                    }
                )

        completion_view: dict[str, Any] | None = None
        if completion is not None:
            completion_view = {
                "id": completion["id"],
                "type": "complete",
                "fact_ids": list(completion["fact_ids"]),
                "description": completion["description"],
                "worker_name": completion["worker_name"],
                "created_at": completion["created_at"],
            }
            if goal_node_id is not None:
                for fact_id in dict.fromkeys(completion["fact_ids"]):
                    source = fact_node_ids.get(fact_id)
                    if source is None or source == goal_node_id:
                        continue
                    edges.append(
                        {
                            "id": f"complete:{completion['id']}:{fact_id}",
                            "kind": "complete",
                            "type": "complete",
                            "state": "completed",
                            "source": source,
                            "target": goal_node_id,
                            "completion_id": completion["id"],
                            "fact_id": fact_id,
                            "label": completion["description"],
                            "created_at": completion["created_at"],
                        }
                    )

        return {
            "schema": "cairn_core_graph_v1",
            "nodes": nodes,
            "edges": edges,
            "hints": [
                {
                    "id": hint.id,
                    "type": "hint",
                    "content": hint.content,
                    "creator": hint.creator,
                    "created_at": hint.created_at,
                }
                for hint in hints
            ],
            "completion": completion_view,
        }

    @synchronized
    def slime_meta_projection(
        self,
        project_id: str,
        worker_run_limit: int = 50,
        evidence_limit: int = 100,
        action_limit: int = 100,
    ) -> dict[str, Any]:
        """Return Slime-specific annotations that decorate, never define, the core graph."""

        self.get_project(project_id)
        facts = self.list_facts(project_id)
        intents = self.list_intents(project_id)
        hypotheses = self.list_hypotheses(project_id)
        path_rows = self._connection.execute(
            """SELECT fact_id, intent_id, strength, updated_at
            FROM path_edges WHERE project_id = ? ORDER BY fact_id, intent_id""",
            (project_id,),
        ).fetchall()
        active_leases = [
            {
                "intent_id": intent.id,
                "kind": intent.kind,
                "worker": intent.owner,
                "lease_expires_at": intent.lease_expires_at,
                "last_heartbeat_at": intent.last_heartbeat_at,
                "attempts": intent.attempts,
            }
            for intent in intents
            if intent.status == "running"
        ]
        return {
            "schema": "slime_meta_v1",
            "facts": {
                fact.id: {
                    "confidence": fact.confidence,
                    "evidence_refs": list(fact.evidence_refs),
                    "evidence_count": len(fact.evidence_refs),
                    "attributes": dict(fact.attributes),
                    "source_intent_id": fact.source_intent_id,
                    "memory": {
                        "state": fact.memory_state,
                        "structural_importance": fact.structural_importance,
                        "reference_count": fact.reference_count,
                        "branch_count": fact.branch_count,
                        "omission_count": fact.omission_count,
                        "access_count": fact.access_count,
                        "last_accessed_at": fact.last_accessed_at,
                        "last_revived_at": fact.last_revived_at,
                    },
                }
                for fact in facts
            },
            "intents": {
                intent.id: {
                    "kind": intent.kind,
                    "status": intent.status,
                    "target_entity": intent.target_entity,
                    "parent_fact_ids": list(intent.parent_fact_ids),
                    "expected_value": intent.expected_value,
                    "novelty": intent.novelty,
                    "cost": intent.cost,
                    "risk": intent.risk,
                    "nutrient": intent.nutrient,
                    "strength": intent.strength,
                    "attempts": intent.attempts,
                    "failure_streak": intent.failure_streak,
                    "retry_not_before": intent.retry_not_before,
                    "worker": intent.owner,
                    "lease_expires_at": intent.lease_expires_at,
                    "last_heartbeat_at": intent.last_heartbeat_at,
                    "last_error": intent.last_error,
                    "context": dict(intent.context),
                    "provenance": dict(intent.provenance),
                }
                for intent in intents
            },
            "overlays": {
                "hypotheses": [asdict(hypothesis) for hypothesis in hypotheses],
                "fact_relations": self.list_fact_relations(project_id),
                "path_edges": [
                    {
                        "fact_id": str(row["fact_id"]),
                        "intent_id": str(row["intent_id"]),
                        "strength": float(row["strength"]),
                        "updated_at": float(row["updated_at"]),
                    }
                    for row in path_rows
                ],
            },
            "runtime": {
                "active_leases": active_leases,
                "reason_state": self.get_reason_state(project_id),
                "reason_lease": self.get_reason_lease(project_id),
                "worker_runs": self.list_worker_runs(project_id, worker_run_limit),
                "evidence": self.list_evidence(project_id, evidence_limit),
                "actions": self.list_actions(project_id, action_limit),
            },
        }

    @synchronized
    def graph_projection(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        """Build a UI-neutral directed graph from the persistent Blackboard.

        Cytoscape and other renderers can consume this without needing to infer
        relationships from Slime's storage model.  Intent nodes make the
        Fact -> Intent -> Fact flow explicit, including open work that has no
        resulting Fact yet.
        """

        facts = self.list_facts(project_id)
        intents = self.list_intents(project_id)
        hypotheses = self.list_hypotheses(project_id)
        completion = self.get_active_completion(project_id)
        fact_relations = self.list_fact_relations(project_id)
        path_rows = self._connection.execute(
            """SELECT fact_id, intent_id, strength, updated_at
            FROM path_edges WHERE project_id = ?""",
            (project_id,),
        ).fetchall()
        path_edges = {
            (str(row["fact_id"]), str(row["intent_id"])): {
                "strength": float(row["strength"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in path_rows
        }

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        fact_node_ids: dict[str, str] = {}
        intent_node_ids: dict[str, str] = {}
        hypothesis_node_ids: dict[str, str] = {}
        origin_node_id: str | None = None
        goal_node_id: str | None = None

        for fact in facts:
            role = str(fact.attributes.get("role", "")).strip().lower()
            if role not in {"origin", "goal"}:
                if fact.predicate == "project_origin":
                    role = "origin"
                elif fact.predicate == "project_goal":
                    role = "goal"
                else:
                    role = "fact"
            node_id = f"fact:{fact.id}"
            fact_node_ids[fact.id] = node_id
            if role == "origin":
                origin_node_id = node_id
            elif role == "goal":
                goal_node_id = node_id
            label = fact.object if role in {"origin", "goal"} else f"{fact.predicate}: {fact.object}"
            nodes.append(
                {
                    "id": node_id,
                    "entity_id": fact.id,
                    "kind": role,
                    "label": label,
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "object": fact.object,
                    "confidence": fact.confidence,
                    "evidence_refs": list(fact.evidence_refs),
                    "evidence_count": len(fact.evidence_refs),
                    "source_intent_id": fact.source_intent_id,
                    "memory_state": fact.memory_state,
                    "structural_importance": fact.structural_importance,
                    "created_at": fact.created_at,
                }
            )

        for intent in intents:
            node_id = f"intent:{intent.id}"
            intent_node_ids[intent.id] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "entity_id": intent.id,
                    "kind": "intent",
                    "label": intent.objective,
                    "intent_kind": intent.kind,
                    "status": intent.status,
                    "worker": intent.owner,
                    "target_entity": intent.target_entity,
                    "nutrient": intent.nutrient,
                    "strength": intent.strength,
                    "attempts": intent.attempts,
                    "failure_streak": intent.failure_streak,
                    "retry_not_before": intent.retry_not_before,
                    "lease_expires_at": intent.lease_expires_at,
                    "last_heartbeat_at": intent.last_heartbeat_at,
                    "last_error": intent.last_error,
                    "created_at": intent.created_at,
                }
            )
            for fact_id in dict.fromkeys(intent.parent_fact_ids):
                source = fact_node_ids.get(fact_id)
                if source is None:
                    continue
                path = path_edges.get((fact_id, intent.id), {})
                edges.append(
                    {
                        "id": f"intent-input:{intent.id}:{fact_id}",
                        "kind": "intent_input",
                        "source": source,
                        "target": node_id,
                        "intent_id": intent.id,
                        "fact_id": fact_id,
                        **path,
                    }
                )
            if intent.kind == "bootstrap" and origin_node_id is not None and not intent.parent_fact_ids:
                edges.append(
                    {
                        "id": f"bootstrap-origin:{intent.id}",
                        "kind": "bootstrap_origin",
                        "source": origin_node_id,
                        "target": node_id,
                        "intent_id": intent.id,
                        "visual_only": True,
                    }
                )
            if intent.kind == "bootstrap" and goal_node_id is not None:
                edges.append(
                    {
                        "id": f"bootstrap-scope:{intent.id}",
                        "kind": "bootstrap_scope",
                        "source": node_id,
                        "target": goal_node_id,
                        "intent_id": intent.id,
                        "visual_only": True,
                    }
                )

        for fact in facts:
            if not fact.source_intent_id:
                continue
            source = intent_node_ids.get(fact.source_intent_id)
            target = fact_node_ids.get(fact.id)
            if source is None or target is None:
                continue
            edges.append(
                {
                    "id": f"intent-output:{fact.source_intent_id}:{fact.id}",
                    "kind": "intent_output",
                    "source": source,
                    "target": target,
                    "intent_id": fact.source_intent_id,
                    "fact_id": fact.id,
                }
            )

        for hypothesis in hypotheses:
            node_id = f"hypothesis:{hypothesis.id}"
            hypothesis_node_ids[hypothesis.id] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "entity_id": hypothesis.id,
                    "kind": "hypothesis",
                    "label": hypothesis.statement,
                    "status": hypothesis.status,
                    "confidence": hypothesis.confidence,
                    "next_validation": hypothesis.next_validation,
                    "evidence_refs": list(hypothesis.evidence_refs),
                    "source_intent_id": hypothesis.source_intent_id,
                    "created_at": hypothesis.created_at,
                }
            )
            if hypothesis.source_intent_id in intent_node_ids:
                edges.append(
                    {
                        "id": f"intent-hypothesis:{hypothesis.source_intent_id}:{hypothesis.id}",
                        "kind": "intent_hypothesis",
                        "source": intent_node_ids[hypothesis.source_intent_id],
                        "target": node_id,
                        "intent_id": hypothesis.source_intent_id,
                        "hypothesis_id": hypothesis.id,
                    }
                )
            for fact_id in dict.fromkeys(hypothesis.supporting_fact_ids):
                source = fact_node_ids.get(fact_id)
                if source is None:
                    continue
                edges.append(
                    {
                        "id": f"hypothesis-support:{hypothesis.id}:{fact_id}",
                        "kind": "hypothesis_support",
                        "source": source,
                        "target": node_id,
                        "fact_id": fact_id,
                        "hypothesis_id": hypothesis.id,
                    }
                )

        for relation in fact_relations:
            source = fact_node_ids.get(str(relation["source_fact_id"]))
            target = fact_node_ids.get(str(relation["target_fact_id"]))
            if source is None or target is None:
                continue
            relation_name = str(relation["relation"])
            edges.append(
                {
                    "id": (
                        "fact-relation:"
                        f"{relation['source_fact_id']}:{relation['target_fact_id']}:{relation_name}"
                    ),
                    "kind": "fact_relation",
                    "source": source,
                    "target": target,
                    "relation": relation_name,
                    "weight": float(relation["weight"]),
                    "created_at": float(relation["created_at"]),
                }
            )

        if completion is not None and goal_node_id is not None:
            for fact_id in dict.fromkeys(completion["fact_ids"]):
                source = fact_node_ids.get(fact_id)
                if source is None or source == goal_node_id:
                    continue
                edges.append(
                    {
                        "id": f"completion-evidence:{completion['id']}:{fact_id}",
                        "kind": "completion_evidence",
                        "source": source,
                        "target": goal_node_id,
                        "completion_id": completion["id"],
                        "fact_id": fact_id,
                    }
                )

        return {"nodes": nodes, "edges": edges}

    @synchronized
    def view_projection(
        self,
        project_id: str,
        worker_run_limit: int = 50,
        evidence_limit: int = 100,
        action_limit: int = 100,
    ) -> dict[str, Any]:
        """Return the read-only records needed by a Cairn-style project view."""

        snapshot = self.snapshot(project_id)
        intents = snapshot["intents"]
        hypotheses = snapshot["hypotheses"]
        intent_status: dict[str, int] = {}
        hypothesis_status: dict[str, int] = {}
        for intent in intents:
            status = str(intent["status"])
            intent_status[status] = intent_status.get(status, 0) + 1
        for hypothesis in hypotheses:
            status = str(hypothesis["status"])
            hypothesis_status[status] = hypothesis_status.get(status, 0) + 1
        active_leases = [
            {
                "intent_id": intent["id"],
                "kind": intent["kind"],
                "owner": intent["owner"],
                "lease_expires_at": intent["lease_expires_at"],
                "last_heartbeat_at": intent["last_heartbeat_at"],
                "attempts": intent["attempts"],
            }
            for intent in intents
            if intent["status"] == "running"
        ]
        slime_meta = self.slime_meta_projection(
            project_id,
            worker_run_limit=worker_run_limit,
            evidence_limit=evidence_limit,
            action_limit=action_limit,
        )
        return {
            **snapshot,
            # Keep the current expanded graph while callers migrate to the
            # Cairn-shaped causal projection and its Slime overlay.
            "graph": self.graph_projection(project_id),
            "core_graph": self.core_graph_projection(project_id),
            "slime_meta": slime_meta,
            "summary": {
                "fact_count": len(snapshot["facts"]),
                "hint_count": len(snapshot["hints"]),
                "intent_status": intent_status,
                "hypothesis_status": hypothesis_status,
                "active_lease_count": len(active_leases),
            },
            "active_leases": active_leases,
            "worker_runs": slime_meta["runtime"]["worker_runs"],
            "evidence": slime_meta["runtime"]["evidence"],
            "actions": slime_meta["runtime"]["actions"],
            "reason_state": slime_meta["runtime"]["reason_state"],
            "reason_lease": slime_meta["runtime"]["reason_lease"],
            "latest_event_id": self.latest_event_id(project_id),
        }

    @synchronized
    def get_reason_state(self, project_id: str) -> dict[str, Any]:
        self.get_project(project_id)
        row = self._connection.execute(
            "SELECT * FROM reason_state WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return {
                "project_id": project_id,
                "last_fact_time": 0.0,
                "last_fact_ids": [],
                "last_event_id": 0,
                "global_summary": "",
                "incremental_runs": 0,
                "last_global_audit_event_id": 0,
                "last_global_audit_at": 0.0,
                "no_progress_streak": 0,
                "last_hint_created_at": 0.0,
                "failure_streak": 0,
                "failure_last_error": "",
                "failure_paused_until": 0.0,
                "updated_at": 0.0,
            }
        state = dict(row)
        state["last_fact_ids"] = json.loads(state.pop("last_fact_ids_json", "[]"))
        return state

    @synchronized
    def save_reason_state(
        self,
        project_id: str,
        last_fact_time: float,
        last_event_id: int,
        global_summary: str,
        last_fact_ids: list[str] | None = None,
        incremental_runs: int | None = None,
        last_global_audit_event_id: int | None = None,
        last_global_audit_at: float | None = None,
        no_progress_streak: int | None = None,
        last_hint_created_at: float | None = None,
        failure_streak: int | None = None,
        failure_last_error: str | None = None,
        failure_paused_until: float | None = None,
    ) -> None:
        self.get_project(project_id)
        previous = self.get_reason_state(project_id)
        if last_fact_ids is None:
            rows = self._connection.execute(
                "SELECT id FROM facts WHERE project_id = ? AND created_at = ? ORDER BY id",
                (project_id, last_fact_time),
            ).fetchall()
            last_fact_ids = [str(row["id"]) for row in rows]
        incremental_runs = int(previous["incremental_runs"] if incremental_runs is None else incremental_runs)
        last_global_audit_event_id = int(
            previous["last_global_audit_event_id"]
            if last_global_audit_event_id is None
            else last_global_audit_event_id
        )
        last_global_audit_at = float(
            previous["last_global_audit_at"] if last_global_audit_at is None else last_global_audit_at
        )
        no_progress_streak = int(
            previous["no_progress_streak"] if no_progress_streak is None else no_progress_streak
        )
        last_hint_created_at = float(
            previous.get("last_hint_created_at", 0.0)
            if last_hint_created_at is None
            else last_hint_created_at
        )
        failure_streak = int(
            previous.get("failure_streak", 0)
            if failure_streak is None
            else failure_streak
        )
        failure_last_error = str(
            previous.get("failure_last_error", "")
            if failure_last_error is None
            else failure_last_error
        )
        failure_paused_until = float(
            previous.get("failure_paused_until", 0.0)
            if failure_paused_until is None
            else failure_paused_until
        )
        self._connection.execute(
            """INSERT INTO reason_state
            (project_id, last_fact_time, last_fact_ids_json, last_event_id, global_summary,
             incremental_runs, last_global_audit_event_id, last_global_audit_at,
             no_progress_streak, last_hint_created_at, failure_streak,
             failure_last_error, failure_paused_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                last_fact_time = excluded.last_fact_time,
                last_fact_ids_json = excluded.last_fact_ids_json,
                last_event_id = excluded.last_event_id,
                global_summary = excluded.global_summary,
                incremental_runs = excluded.incremental_runs,
                last_global_audit_event_id = excluded.last_global_audit_event_id,
                last_global_audit_at = excluded.last_global_audit_at,
                no_progress_streak = excluded.no_progress_streak,
                last_hint_created_at = excluded.last_hint_created_at,
                failure_streak = excluded.failure_streak,
                failure_last_error = excluded.failure_last_error,
                failure_paused_until = excluded.failure_paused_until,
                updated_at = excluded.updated_at""",
            (
                project_id,
                last_fact_time,
                stable_json(last_fact_ids),
                last_event_id,
                global_summary,
                incremental_runs,
                last_global_audit_event_id,
                last_global_audit_at,
                no_progress_streak,
                last_hint_created_at,
                failure_streak,
                failure_last_error,
                failure_paused_until,
                now(),
            ),
        )

    @synchronized
    def record_reason_failure(
        self,
        project_id: str,
        error: str,
        paused_until: float,
        *,
        same_error: bool = True,
    ) -> dict[str, Any]:
        """Persist Reason failure/backoff state so a restart does not hot-loop."""

        state = self.get_reason_state(project_id)
        streak = int(state.get("failure_streak", 0)) + 1 if same_error else 1
        normalized = str(error).strip()[:2000]
        self.save_reason_state(
            project_id,
            float(state.get("last_fact_time", 0.0)),
            int(state.get("last_event_id", 0)),
            str(state.get("global_summary", "")),
            last_fact_ids=list(state.get("last_fact_ids", [])),
            incremental_runs=int(state.get("incremental_runs", 0)),
            last_global_audit_event_id=int(state.get("last_global_audit_event_id", 0)),
            last_global_audit_at=float(state.get("last_global_audit_at", 0.0)),
            no_progress_streak=int(state.get("no_progress_streak", 0)),
            last_hint_created_at=float(state.get("last_hint_created_at", 0.0)),
            failure_streak=streak,
            failure_last_error=normalized,
            failure_paused_until=float(paused_until),
        )
        return self.get_reason_state(project_id)

    @synchronized
    def clear_reason_failure(self, project_id: str) -> None:
        """Clear persisted Reason pause after a successful planning pass."""

        state = self.get_reason_state(project_id)
        if not state.get("failure_streak") and not state.get("failure_paused_until"):
            return
        self.save_reason_state(
            project_id,
            float(state.get("last_fact_time", 0.0)),
            int(state.get("last_event_id", 0)),
            str(state.get("global_summary", "")),
            last_fact_ids=list(state.get("last_fact_ids", [])),
            incremental_runs=int(state.get("incremental_runs", 0)),
            last_global_audit_event_id=int(state.get("last_global_audit_event_id", 0)),
            last_global_audit_at=float(state.get("last_global_audit_at", 0.0)),
            no_progress_streak=int(state.get("no_progress_streak", 0)),
            last_hint_created_at=float(state.get("last_hint_created_at", 0.0)),
            failure_streak=0,
            failure_last_error="",
            failure_paused_until=0.0,
        )

    @synchronized
    def snapshot(self, project_id: str) -> dict[str, Any]:
        return {
            "project": asdict(self.get_project(project_id)),
            "active_completion": self.get_active_completion(project_id),
            "completion_history": self.list_project_completions(project_id),
            "facts": [asdict(item) for item in self.list_facts(project_id)],
            "hints": [asdict(item) for item in self.list_hints(project_id)],
            "intents": [asdict(item) for item in self.list_intents(project_id)],
            "hypotheses": [asdict(item) for item in self.list_hypotheses(project_id)],
            "reason_lease": self.get_reason_lease(project_id),
        }

    @staticmethod
    def _completion_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "fact_ids": json.loads(row["fact_ids_json"]),
            "description": row["description"],
            "worker_name": row["worker_name"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    @synchronized
    def save_dispatcher_state(
        self,
        project_id: str,
        dispatcher_id: str,
        state: str,
        status: dict[str, Any],
    ) -> bool:
        if self._connection.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)
        ).fetchone() is None:
            return False
        self._connection.execute(
            """INSERT INTO dispatcher_states
            (project_id, dispatcher_id, state, status_json, heartbeat_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                dispatcher_id = excluded.dispatcher_id,
                state = excluded.state,
                status_json = excluded.status_json,
                heartbeat_at = excluded.heartbeat_at""",
            (project_id, dispatcher_id, state, stable_json(status), now()),
        )
        return True

    @synchronized
    def get_dispatcher_state(self, project_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM dispatcher_states WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "project_id": row["project_id"],
            "dispatcher_id": row["dispatcher_id"],
            "state": row["state"],
            "status": json.loads(row["status_json"]),
            "heartbeat_at": row["heartbeat_at"],
        }

    @synchronized
    def configure_benchmark_automation(
        self,
        task_key: str,
        *,
        enabled: bool,
        parallelism: int = 3,
    ) -> dict[str, Any]:
        key = str(task_key).strip()
        if not key:
            raise ValueError("Benchmark task_key is required")
        capacity = int(parallelism)
        if capacity < 1 or capacity > 3:
            raise ValueError("Benchmark parallelism must be between 1 and 3")
        existing = self._connection.execute(
            "SELECT state_json FROM benchmark_automations WHERE task_key = ?",
            (key,),
        ).fetchone()
        state = json.loads(existing["state_json"]) if existing is not None else {}
        state["status"] = "starting" if enabled else "stopped"
        state["last_error"] = ""
        updated_at = now()
        self._connection.execute(
            """INSERT INTO benchmark_automations
            (task_key, enabled, parallelism, state_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_key) DO UPDATE SET
                enabled = excluded.enabled,
                parallelism = excluded.parallelism,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at""",
            (key, int(bool(enabled)), capacity, stable_json(state), updated_at),
        )
        return self.get_benchmark_automation(key)

    @synchronized
    def save_benchmark_automation_state(
        self,
        task_key: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        key = str(task_key).strip()
        row = self._connection.execute(
            "SELECT 1 FROM benchmark_automations WHERE task_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            self.configure_benchmark_automation(key, enabled=False, parallelism=3)
        self._connection.execute(
            "UPDATE benchmark_automations SET state_json = ?, updated_at = ? WHERE task_key = ?",
            (stable_json(state), now(), key),
        )
        return self.get_benchmark_automation(key)

    @synchronized
    def get_benchmark_automation(self, task_key: str) -> dict[str, Any]:
        key = str(task_key).strip()
        row = self._connection.execute(
            "SELECT * FROM benchmark_automations WHERE task_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return {
                "task_key": key,
                "enabled": False,
                "parallelism": 3,
                "status": "stopped",
                "active_count": 0,
                "queued_count": 0,
                "completed_count": 0,
                "total_count": 0,
                "active": [],
                "last_actions": [],
                "last_error": "",
                "last_tick_at": None,
                "updated_at": None,
            }
        state = json.loads(row["state_json"])
        return {
            "task_key": key,
            "enabled": bool(row["enabled"]),
            "parallelism": int(row["parallelism"]),
            "status": str(state.get("status") or ("running" if row["enabled"] else "stopped")),
            "active_count": int(state.get("active_count") or 0),
            "queued_count": int(state.get("queued_count") or 0),
            "completed_count": int(state.get("completed_count") or 0),
            "total_count": int(state.get("total_count") or 0),
            "active": list(state.get("active") or []),
            "last_actions": list(state.get("last_actions") or []),
            "last_error": str(state.get("last_error") or ""),
            "last_tick_at": state.get("last_tick_at"),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _row_to_hint(row: sqlite3.Row) -> Hint:
        return Hint(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            content=str(row["content"]),
            creator=str(row["creator"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"], project_id=row["project_id"], subject=row["subject"], predicate=row["predicate"],
            object=row["object"], confidence=row["confidence"], evidence_refs=json.loads(row["evidence_json"]),
            source_intent_id=row["source_intent_id"], attributes=json.loads(row["attributes_json"]),
            created_at=row["created_at"],
            memory_state=row["memory_state"], structural_importance=row["structural_importance"],
            reference_count=row["reference_count"], branch_count=row["branch_count"],
            omission_count=row["omission_count"], access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"], last_revived_at=row["last_revived_at"],
        )

    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> Intent:
        return Intent(
            id=row["id"], project_id=row["project_id"], fingerprint=row["fingerprint"], kind=row["kind"],
            objective=row["objective"], target_entity=row["target_entity"],
            parent_fact_ids=json.loads(row["parent_fact_ids_json"]), context=json.loads(row["context_json"]),
            provenance=json.loads(row["provenance_json"]),
            expected_value=row["expected_value"], novelty=row["novelty"], cost=row["cost"], risk=row["risk"],
            nutrient=row["nutrient"], strength=row["strength"], status=row["status"], owner=row["owner"],
            lease_expires_at=row["lease_expires_at"], last_heartbeat_at=row["last_heartbeat_at"],
            last_error=row["last_error"], attempts=row["attempts"],
            failure_streak=row["failure_streak"], retry_not_before=row["retry_not_before"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_hypothesis(row: sqlite3.Row) -> Hypothesis:
        return Hypothesis(
            id=row["id"],
            project_id=row["project_id"],
            statement=row["statement"],
            supporting_fact_ids=json.loads(row["supporting_fact_ids_json"]),
            evidence_refs=json.loads(row["evidence_refs_json"]),
            confidence=row["confidence"],
            next_validation=row["next_validation"],
            source_intent_id=row["source_intent_id"],
            status=row["status"],
            created_at=row["created_at"],
        )
