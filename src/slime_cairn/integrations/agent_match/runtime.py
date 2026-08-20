"""Map competition exercises to persistent Slime projects."""

from __future__ import annotations

from dataclasses import asdict
import json
import re
import time
from typing import Any

from ...domain.models import Fact, FactCandidate, Project, fingerprint, new_id
from ...domain.seeding import seed_project_context_facts
from ...server.blackboard import Blackboard
from ...server.writeups import collect_team_entries, generate_team_writeup, generate_writeup, project_competition_key
from ...workers.writeup import configured as writeup_worker_configured
from ...workers.writeup import queue_team_writeup
from .client import AgentMatchClient, AgentMatchError, AgentMatchSettings


MAX_SUBMISSION_ATTEMPTS = 50
WRAPPED_FLAG = re.compile(r"^(?:DASCTF|flag)\{(.+)\}$", re.IGNORECASE | re.DOTALL)


def agent_match_metadata(project: Project) -> dict[str, Any] | None:
    value = project.scope.get("agent_match")
    if isinstance(value, dict) and value.get("managed") is True:
        return dict(value)
    return None


class AgentMatchProjectController:
    """Own exercise lifecycle, without exposing the competition AccessKey."""

    def __init__(
        self,
        board: Blackboard,
        client: AgentMatchClient,
        settings: AgentMatchSettings,
    ) -> None:
        self.board = board
        self.client = client
        self.settings = settings
        self._category_by_exercise: dict[int, dict[str, Any]] | None = None

    def _category_map(self) -> dict[int, dict[str, Any]]:
        """Build the platform exercise-id to CTF-category mapping once per controller."""

        if self._category_by_exercise is not None:
            return self._category_by_exercise
        mapping: dict[int, dict[str, Any]] = {}
        for group in self.client.list_exercises():
            if not isinstance(group, dict):
                continue
            group_id = group.get("id")
            group_name = str(group.get("name") or "").strip()
            corpus = group.get("corpus")
            if isinstance(corpus, list):
                for item in corpus:
                    if not isinstance(item, dict):
                        continue
                    try:
                        exercise_id = int(item.get("id"))
                    except (TypeError, ValueError):
                        continue
                    mapping[exercise_id] = {
                        "category_id": group_id,
                        "category_name": group_name,
                    }
                continue
            # Accept the flat exercise-list shape too; this keeps metadata
            # synchronization working across both platform API versions.
            try:
                exercise_id = int(group.get("id"))
            except (TypeError, ValueError):
                continue
            if group.get("category_name") or group.get("category_id"):
                mapping[exercise_id] = {
                    "category_id": group.get("category_id"),
                    "category_name": str(group.get("category_name") or "").strip(),
                }
        self._category_by_exercise = mapping
        return mapping

    def _category_for_exercise(self, exercise_id: int) -> dict[str, Any]:
        return dict(self._category_map().get(int(exercise_id), {}))

    def _persist_category(self, project: Project, category: dict[str, Any]) -> Project:
        if project.status == "deleting" or not category:
            return project
        metadata = agent_match_metadata(project)
        if not metadata:
            return project
        changed = any(metadata.get(key) != value for key, value in category.items() if value not in (None, ""))
        if not changed:
            return project
        metadata.update({key: value for key, value in category.items() if value not in (None, "")})
        scope = dict(project.scope)
        scope["agent_match"] = metadata
        return self.board.update_project_context(project.id, scope=scope)

    def backfill_project_categories(self) -> int:
        """Persist platform categories on already-completed legacy projects."""

        mapping = self._category_map()
        changed = 0
        for project in self.board.list_projects(include_deleting=True):
            metadata = agent_match_metadata(project)
            if not metadata or metadata.get("task_key") != self.settings.task_key:
                continue
            category = mapping.get(int(metadata.get("exercise_id") or 0), {})
            before = (metadata.get("category_id"), metadata.get("category_name"))
            updated = self._persist_category(project, category)
            after_metadata = agent_match_metadata(updated) or {}
            if before != (after_metadata.get("category_id"), after_metadata.get("category_name")):
                changed += 1
        return changed

    def find_project(self, exercise_id: int) -> Project | None:
        for project in self.board.list_projects(include_deleting=True):
            metadata = agent_match_metadata(project)
            if not metadata:
                continue
            if (
                metadata.get("task_key") == self.settings.task_key
                and int(metadata.get("exercise_id") or 0) == int(exercise_id)
            ):
                return project
        return None

    def list_exercises(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for category in self.client.list_exercises():
            category_id = category.get("id")
            category_name = str(category.get("name") or "").strip()
            corpus = category.get("corpus")
            if not isinstance(corpus, list):
                continue
            for raw_exercise in corpus:
                if not isinstance(raw_exercise, dict):
                    continue
                exercise = dict(raw_exercise)
                try:
                    exercise_id = int(exercise.get("id"))
                except (TypeError, ValueError):
                    continue
                project = self.find_project(exercise_id)
                if project is not None:
                    project = self._persist_category(
                        project,
                        {"category_id": category_id, "category_name": category_name},
                    )
                exercise.update(
                    {
                        "category_id": category_id,
                        "category_name": category_name,
                        "project_id": project.id if project and project.status != "deleting" else None,
                        "project_status": project.status if project and project.status != "deleting" else None,
                    }
                )
                rows.append(exercise)
        return rows

    def exercise(self, exercise_id: int) -> dict[str, Any]:
        detail = self.client.get_exercise(exercise_id)
        project = self.find_project(exercise_id)
        return {
            "exercise": detail,
            "project_id": project.id if project and project.status != "deleting" else None,
            "project_status": project.status if project and project.status != "deleting" else None,
        }

    def start(self, exercise_id: int) -> dict[str, Any]:
        match_info = self.client.match_info()
        detail = self.client.get_exercise(exercise_id)
        if bool(detail.get("hasSolved")):
            raise AgentMatchError(409, "exercise_solved", "Exercise has already been solved")
        if bool(detail.get("isNeedInit")):
            self.client.build_environment(exercise_id)
        detail = self._wait_until_ready(exercise_id, detail)
        return self._activate_exercise(
            detail,
            event_kind="agent_match.exercise_started",
            match_info=match_info,
        )

    def sync_active(self, exercise_id: int) -> dict[str, Any]:
        match_info = self.client.match_info()
        detail = self.client.get_exercise(exercise_id)
        if not self._environment_ready(detail):
            raise AgentMatchError(
                409,
                "environment_not_ready",
                "Exercise environment is not ready; start it and wait for endpoints",
            )
        return self._activate_exercise(
            detail,
            event_kind="agent_match.exercise_synchronized",
            match_info=match_info,
        )

    def submit_candidates(
        self,
        project_id: str,
        submissions: list[str],
        *,
        worker_name: str,
        completion_fact_ids: list[str] | None = None,
        completion_description: str = "",
    ) -> dict[str, Any]:
        project = self.board.get_project(project_id)
        metadata = agent_match_metadata(project)
        if not metadata or metadata.get("task_key") != self.settings.task_key:
            raise ValueError("project is not managed by the configured competition task")
        exercise_id = int(metadata.get("exercise_id") or 0)
        if exercise_id < 1:
            raise ValueError("managed competition project has no exercise_id")
        normalized = [self._normalize_submission(value) for value in submissions]
        candidates = list(dict.fromkeys(value for value in normalized if value))
        if not candidates:
            raise ValueError("competition completion requires at least one candidate submission")
        prior_submissions = {
            fact.object
            for fact in self.board.list_facts(project_id)
            if fact.predicate in {"agent_match_flag_verified", "agent_match_flag_rejected"}
        }
        new_submission_count = sum(candidate not in prior_submissions for candidate in candidates)
        if len(prior_submissions) + new_submission_count > MAX_SUBMISSION_ATTEMPTS:
            raise ValueError("competition exercise submission limit of 50 distinct candidates reached")

        outcomes: list[dict[str, Any]] = []
        submission_fact_ids: list[str] = []
        verified_any = False
        for candidate in candidates:
            if len(candidate) > 256:
                raise ValueError("competition candidate submission exceeds 256 characters")
            prior = self._prior_submission(project_id, candidate)
            if prior is not None:
                correct = prior.predicate == "agent_match_flag_verified"
                outcomes.append(
                    {
                        "candidate": candidate,
                        "local_duplicate": True,
                        "correct": correct,
                        "fact_id": prior.id,
                    }
                )
                submission_fact_ids.append(prior.id)
                verified_any = verified_any or correct
                if correct:
                    break
                continue

            response = self.client.submit(exercise_id, candidate)
            correct = bool(response.get("isCorrect"))
            predicate = "agent_match_flag_verified" if correct else "agent_match_flag_rejected"
            evidence_ref = (
                f"agent-match-submit:{exercise_id}:{fingerprint(candidate)[:16]}:{new_id('evidence')}"
            )
            self.board.register_evidence(
                project_id,
                evidence_ref,
                "agent_match_platform_response",
                metadata={
                    "exercise_id": exercise_id,
                    "correct": correct,
                    "platform_code": response.get("platform_code"),
                    "platform_message": response.get("platform_message"),
                },
            )
            fact, _ = self.board.add_fact(
                project_id,
                FactCandidate(
                    subject=project.target,
                    predicate=predicate,
                    object=candidate,
                    confidence=1.0,
                    evidence_refs=[evidence_ref],
                    attributes={
                        "agent_match": True,
                        "platform_verified": correct,
                        "pinned": correct,
                        "exercise_id": exercise_id,
                        "platform_code": response.get("platform_code"),
                    },
                ),
            )
            submission_fact_ids.append(fact.id)
            verified_any = verified_any or correct
            outcomes.append({"candidate": candidate, "fact_id": fact.id, "correct": correct, **response})
            self.board.add_event(
                project_id,
                "agent_match.flag_verified" if correct else "agent_match.flag_rejected",
                {
                    "exercise_id": exercise_id,
                    "fact_id": fact.id,
                    "correct": correct,
                    "platform_code": response.get("platform_code"),
                },
            )
            if correct:
                break

        detail = self.client.get_exercise(exercise_id)
        project = self._refresh_project_metadata(self.board.get_project(project_id), detail)
        completed = bool(detail.get("hasSolved")) or verified_any
        progress = {
            "correct_flag_count": 1 if completed else 0,
            "flag_count": 1,
            "is_completed": completed,
            "has_solved": bool(detail.get("hasSolved")),
            "endpoints_ready": self._environment_ready(detail),
        }

        completion = None
        environment_recovery: dict[str, Any] | None = None
        if completed:
            verified_facts = [
                fact
                for fact in self.board.list_facts(project_id)
                if fact.predicate == "agent_match_flag_verified"
            ]
            evidence_ref = f"agent-match-complete:{exercise_id}:{new_id('evidence')}"
            self.board.register_evidence(
                project_id,
                evidence_ref,
                "agent_match_platform_completion",
                metadata={"exercise_id": exercise_id, **progress},
            )
            verification, _ = self.board.add_fact(
                project_id,
                FactCandidate(
                    subject=project.target,
                    predicate="agent_match_completion_verified",
                    object=json.dumps(
                        {
                            "exercise_id": exercise_id,
                            "verified_candidates": list(dict.fromkeys(fact.object for fact in verified_facts)),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    confidence=1.0,
                    evidence_refs=[evidence_ref],
                    attributes={"agent_match": True, "platform_verified": True, "pinned": True},
                ),
            )
            referenced = list(
                dict.fromkeys(
                    [
                        *(completion_fact_ids or []),
                        *(fact.id for fact in verified_facts),
                        verification.id,
                    ]
                )
            )
            description = completion_description.strip() or "Competition platform verified the exercise answer"
            completion, _ = self.board.complete_project(
                project_id,
                referenced,
                description,
                worker_name,
            )
            # A solved exercise no longer needs its remote container.  Reclaim
            # it immediately, while keeping the Slime project completed so
            # its facts, Flag verification and WP remain available locally.
            environment_recovery = self._recover_after_completion(project_id, exercise_id)
            try:
                try:
                    overview = self.client.overview()
                except Exception:
                    overview = {}
                completed_count = sum(
                    1
                    for item in self.board.list_projects(include_deleting=True)
                    if item.status == "completed"
                    and (agent_match_metadata(item) or {}).get("task_key") == self.settings.task_key
                )
                writeup_markdown, validation = generate_writeup(
                    self.board.get_project(project_id),
                    completion,
                    self.board.list_facts(project_id),
                    self.board.list_evidence(project_id),
                    self.board.list_worker_runs(project_id),
                    team={
                        "rank": overview.get("stageRank", overview.get("rank", "待填写")),
                        "solved_count": completed_count,
                    },
                )
                writeup = self.board.save_project_writeup(
                    project_id,
                    writeup_markdown,
                    validation["status"],
                    validation["template_version"],
                    validation,
                )
                self.board.add_event(
                    project_id,
                    "writeup.generated",
                    {"writeup_id": writeup["id"], "status": writeup["status"], "validation": validation},
                )
                team_key = project_competition_key(self.board.get_project(project_id))
                if team_key:
                    team_markdown, team_validation = generate_team_writeup(
                        collect_team_entries(self.board, team_key),
                        team={"rank": overview.get("stageRank", overview.get("rank", "待填写"))},
                    )
                    self.board.save_competition_writeup(
                        team_key,
                        team_markdown,
                        team_validation["status"],
                        team_validation["template_version"],
                        team_validation,
                    )
                    self.board.add_event(
                        project_id,
                        "writeup.team_generated",
                        {"task_key": team_key, "status": team_validation["status"], "validation": team_validation},
                    )
                    if writeup_worker_configured():
                        queue_team_writeup(
                            self.board,
                            project_id,
                            team={"rank": overview.get("stageRank", overview.get("rank", "待填写"))},
                        )
            except Exception as exc:
                # Writeup generation is auxiliary; a verified flag remains a
                # successful completion even if persistence is temporarily unavailable.
                try:
                    self.board.add_event(
                        project_id,
                        "writeup.validation_failed",
                        {"error": str(exc)[:500]},
                    )
                except Exception:
                    pass

        return {
            "project": asdict(self.board.get_project(project_id)),
            "exercise": detail,
            "outcomes": outcomes,
            "submission_fact_ids": list(dict.fromkeys(submission_fact_ids)),
            "progress": progress,
            "completion": completion,
            "environment_recovery": environment_recovery,
        }

    def _recover_after_completion(self, project_id: str, exercise_id: int) -> dict[str, Any]:
        """Best-effort remote target cleanup after a platform-verified solve."""

        try:
            self.client.recover_environment(exercise_id)
        except AgentMatchError as exc:
            detail = {"exercise_id": int(exercise_id), "code": exc.code, "message": exc.message[:300]}
            self.board.add_event(project_id, "agent_match.exercise_recovery_failed", detail)
            return {"recovered": False, "project_id": project_id, "error": exc.message[:300]}
        project = self.board.get_project(project_id)
        metadata = agent_match_metadata(project) or {}
        metadata.update({"environment_recovered": True, "endpoints": []})
        scope = dict(project.scope)
        scope["agent_match"] = metadata
        self.board.update_project_context(project.id, scope=scope)
        self.board.add_event(
            project.id,
            "agent_match.exercise_recovered",
            {"exercise_id": int(exercise_id), "trigger": "completion"},
        )
        return {"recovered": True, "project_id": project_id}

    def recover(self, exercise_id: int) -> dict[str, Any]:
        self.client.recover_environment(exercise_id)
        project = self.find_project(exercise_id)
        if project is not None and project.status != "deleting":
            metadata = agent_match_metadata(project) or {}
            metadata.update({"environment_recovered": True, "endpoints": []})
            scope = dict(project.scope)
            scope["agent_match"] = metadata
            self.board.update_project_context(project.id, scope=scope)
            if project.status == "running":
                self.board.set_project_status(project.id, "stopped")
            self.board.add_event(
                project.id,
                "agent_match.exercise_recovered",
                {"exercise_id": int(exercise_id)},
            )
        return {"recovered": True, "project_id": project.id if project else None}

    def _wait_until_ready(self, exercise_id: int, detail: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.environment_ready_timeout
        current = detail
        while not self._environment_ready(current):
            if time.monotonic() >= deadline:
                raise AgentMatchError(
                    504,
                    "environment_ready_timeout",
                    "Exercise environment did not become ready before the configured timeout",
                )
            time.sleep(self.settings.environment_poll_interval)
            current = self.client.get_exercise(exercise_id)
        return current

    @staticmethod
    def _environment_ready(detail: dict[str, Any]) -> bool:
        endpoints = detail.get("endpoints")
        if bool(detail.get("isNeedInit")) or bool(detail.get("isNeedCheck")):
            return False
        if isinstance(endpoints, list) and endpoints:
            return True
        # Some Misc challenges are attachment-only and deliberately expose no
        # network endpoint. They are ready once the platform says no init/check
        # is required and provides an attachment to inspect.
        return bool(AgentMatchProjectController._attachments(detail))

    def _activate_exercise(
        self,
        detail: dict[str, Any],
        *,
        event_kind: str,
        match_info: dict[str, Any],
    ) -> dict[str, Any]:
        exercise_id = int(detail.get("id") or 0)
        targets = self._targets(detail)
        if not targets:
            if self._attachments(detail):
                targets = [f"attachment://agent-match/{exercise_id}"]
            else:
                raise AgentMatchError(
                    503,
                    "resource_unavailable",
                    "Exercise environment returned no usable endpoint address or attachment",
                )
        target = targets[0]
        project = self.find_project(exercise_id)
        scope = self._scope(detail, targets, match_info)
        if project is None:
            project = self.board.create_project(
                name=f"agent-match-{exercise_id}",
                target=target,
                goal=self._goal(detail),
                scope=scope,
            )
            self.board.ensure_scope_hints(project.id)
            seed_project_context_facts(self.board, project)
            self._record_exercise_context(project, detail, "agent_match.exercise_context_created")
        else:
            if project.status == "deleting":
                raise AgentMatchError(409, "invalid_state", "Mapped project is being deleted")
            if project.status == "completed":
                raise AgentMatchError(409, "invalid_state", "Mapped project is already completed")
            previous_target = project.target
            if previous_target != target:
                self.board.archive_pending_intents_for_target(
                    project.id,
                    previous_target,
                    "agent_match_endpoint_replaced",
                )
            project = self.board.update_project_context(project.id, target=target, scope=scope)
            seed_project_context_facts(self.board, project)
            self._record_exercise_context(project, detail, "agent_match.exercise_context_refreshed")
            if previous_target != target:
                self.board.add_hint(
                    project.id,
                    f"Competition endpoint changed from {previous_target} to {target}. Replan against the current endpoint and do not continue queued work for the old endpoint.",
                    "agent-match-control-plane",
                )
            if project.status == "stopped":
                project = self.board.set_project_status(project.id, "running")

        self.board.add_event(
            project.id,
            event_kind,
            {"exercise_id": exercise_id, "targets": targets},
        )
        return {
            "exercise": detail,
            "project": asdict(self.board.get_project(project.id)),
            "snapshot": self.board.snapshot(project.id),
        }

    def _refresh_project_metadata(self, project: Project, detail: dict[str, Any]) -> Project:
        metadata = agent_match_metadata(project)
        if not metadata:
            return project
        targets = self._targets(detail)
        metadata.update(self._metadata(detail))
        scope = dict(project.scope)
        scope["agent_match"] = metadata
        if targets:
            scope["targets"] = targets
            return self.board.update_project_context(project.id, target=targets[0], scope=scope)
        return self.board.update_project_context(project.id, scope=scope)

    def _record_exercise_context(self, project: Project, detail: dict[str, Any], event_kind: str) -> None:
        evidence_ref = f"agent-match-exercise:{int(detail.get('id') or 0)}:{new_id('evidence')}"
        self.board.register_evidence(
            project.id,
            evidence_ref,
            "agent_match_exercise_detail",
            metadata={
                "exercise_id": int(detail.get("id") or 0),
                "name": detail.get("name"),
                "difficulty": detail.get("difficulty"),
                "attachments": self._attachments(detail),
            },
        )
        self.board.add_fact(
            project.id,
            FactCandidate(
                subject=project.target,
                predicate="agent_match_exercise_description",
                object=str(detail.get("description") or "No description supplied").strip() or "No description supplied",
                confidence=1.0,
                evidence_refs=[evidence_ref],
                attributes={"agent_match": True, "pinned": True, "exercise_id": int(detail.get("id") or 0)},
            ),
        )
        self.board.add_event(project.id, event_kind, {"exercise_id": int(detail.get("id") or 0)})

    def _scope(
        self,
        detail: dict[str, Any],
        targets: list[str],
        match_info: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "targets": targets,
            "origin": f"competition exercise {int(detail.get('id') or 0)}",
            "start_mode": "hybrid",
            "bootstrap_enabled": True,
            "hints": [],
            "submission": {
                "managed": True,
                "platform": "agent_match",
                "task_key": self.settings.task_key,
            },
            "agent_match": {
                "managed": True,
                "task_key": self.settings.task_key,
                "match_note": str(match_info.get("note") or "").strip(),
                "match_rule": str(match_info.get("rule") or "").strip(),
                **self._metadata(detail),
                **self._category_for_exercise(int(detail.get("id") or 0)),
            },
        }

    def _metadata(self, detail: dict[str, Any]) -> dict[str, Any]:
        endpoints = self._endpoint_objects(detail)
        return {
            "exercise_id": int(detail.get("id") or 0),
            "name": str(detail.get("name") or "").strip(),
            "description": str(detail.get("description") or "").strip(),
            "difficulty": detail.get("difficulty"),
            "score": detail.get("score"),
            "has_solved": bool(detail.get("hasSolved")),
            "attachments": self._attachments(detail),
            "endpoints": endpoints,
            "endpoint_type": detail.get("endpointType"),
            "endpoint_expire_times": [
                item.get("expireTime")
                for item in endpoints
                if isinstance(item, dict) and item.get("expireTime") is not None
            ],
            "environment_recovered": False,
        }

    @staticmethod
    def _attachments(detail: dict[str, Any]) -> list[dict[str, Any]]:
        attachment = detail.get("attachment")
        if not isinstance(attachment, dict):
            return []
        files = attachment.get("files")
        if isinstance(files, list):
            return [dict(item) for item in files if isinstance(item, dict)]
        # The live platform also returns one attachment object directly.
        if any(str(attachment.get(key) or "").strip() for key in ("url", "previewUrl", "name")):
            return [dict(attachment)]
        return []

    @staticmethod
    def _endpoint_objects(detail: dict[str, Any]) -> list[dict[str, Any]]:
        endpoints = detail.get("endpoints")
        if not isinstance(endpoints, list):
            return []
        return [dict(item) for item in endpoints if isinstance(item, dict)]

    @classmethod
    def _targets(cls, detail: dict[str, Any]) -> list[str]:
        direct: list[str] = []
        proxy: list[str] = []
        endpoints = AgentMatchProjectController._endpoint_objects(detail)
        for endpoint in endpoints:
            expose_ips = cls._strings(endpoint.get("exposeIps"))
            ports = cls._strings(endpoint.get("ports"))
            direct.extend(cls._addresses(expose_ips, ports))
            proxy_ips = cls._strings(endpoint.get("proxyIps"))
            mappings = endpoint.get("portMappings")
            proxy_ports = [
                str(item.get("proxy")).strip()
                for item in mappings
                if isinstance(item, dict) and str(item.get("proxy") or "").strip()
            ] if isinstance(mappings, list) else []
            proxy.extend(cls._addresses(proxy_ips, proxy_ports or ports))
        preferred = proxy + direct if any(
            bool(endpoint.get("isProxy"))
            for endpoint in endpoints
        ) else direct + proxy
        return list(dict.fromkeys(value for value in preferred if value))

    @staticmethod
    def _strings(value: Any) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip())) if isinstance(value, list) else []

    @staticmethod
    def _addresses(hosts: list[str], ports: list[str]) -> list[str]:
        if not ports:
            return hosts
        return [f"{host}:{port}" for host in hosts for port in ports]

    @staticmethod
    def _goal(detail: dict[str, Any]) -> str:
        exercise_id = int(detail.get("id") or 0)
        name = str(detail.get("name") or f"exercise-{exercise_id}").strip()
        description = str(detail.get("description") or "No description supplied").strip()
        return (
            f"Solve authorized competition exercise {exercise_id} ({name}). {description} "
            "Analyze only the supplied endpoints and attachments, preserve evidence, and return exact answer "
            "candidates for platform verification."
        )

    def _prior_submission(self, project_id: str, candidate: str) -> Fact | None:
        for fact in self.board.list_facts(project_id):
            if fact.predicate in {"agent_match_flag_verified", "agent_match_flag_rejected"} and fact.object == candidate:
                return fact
        return None

    @staticmethod
    def _normalize_submission(value: object) -> str:
        candidate = str(value or "").strip()
        match = WRAPPED_FLAG.fullmatch(candidate)
        return match.group(1).strip() if match else candidate
