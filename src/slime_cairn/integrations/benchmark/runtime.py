"""Bridge TSec Benchmark lifecycle operations into the persistent Blackboard."""

from __future__ import annotations

from dataclasses import asdict
import json
import re
from typing import Any

from ...domain.models import Fact, FactCandidate, Project, fingerprint, new_id
from ...domain.seeding import seed_project_context_facts
from ...protocol.contracts import extract_submission_candidates
from ...server.blackboard import Blackboard
from .client import BenchmarkClient, BenchmarkError, BenchmarkSettings


# Keep the old public import working for integrations built before submissions
# became format-neutral.
extract_flag_candidates = extract_submission_candidates


def benchmark_metadata(project: Project) -> dict[str, Any] | None:
    value = project.scope.get("benchmark")
    if isinstance(value, dict) and value.get("managed") is True:
        return dict(value)
    return None


class BenchmarkProjectController:
    def __init__(
        self,
        board: Blackboard,
        client: BenchmarkClient,
        settings: BenchmarkSettings,
    ) -> None:
        self.board = board
        self.client = client
        self.settings = settings

    def find_project(self, unique_code: str) -> Project | None:
        for project in self.board.list_projects(include_deleting=True):
            metadata = benchmark_metadata(project)
            if not metadata:
                continue
            if (
                metadata.get("task_key") == self.settings.task_key
                and metadata.get("unique_code") == unique_code
            ):
                return project
        return None

    def list_challenges(self) -> list[dict[str, Any]]:
        challenges = self.client.list_challenges()
        for challenge in challenges:
            unique_code = str(challenge.get("unique_code", "")).strip()
            project = self.find_project(unique_code) if unique_code else None
            if project is not None and project.status != "deleting":
                self._refresh_project_metadata(project, challenge)
                project = self.board.get_project(project.id)
            challenge["project_id"] = project.id if project and project.status != "deleting" else None
            challenge["project_status"] = project.status if project and project.status != "deleting" else None
        return challenges

    def start(self, unique_code: str) -> dict[str, Any]:
        challenge = self._challenge(unique_code)
        started = self.client.start(unique_code)
        addresses = self._addresses(started.get("container_addr"))
        if not addresses:
            raise BenchmarkError(
                503,
                "resource_unavailable",
                "Benchmark start returned no container address",
            )
        challenge.update(started)
        challenge["container_status"] = "available"
        challenge["container_addr"] = addresses
        return self._activate_challenge(challenge, event_kind="benchmark.challenge_started")

    def sync_active(self, challenge: dict[str, Any]) -> dict[str, Any]:
        """Attach or resume the local project for an already-running instance."""

        challenge = dict(challenge)
        addresses = self._addresses(challenge.get("container_addr"))
        if not addresses:
            raise BenchmarkError(
                503,
                "resource_unavailable",
                "Active Benchmark challenge has no container address",
            )
        challenge["container_status"] = "available"
        challenge["container_addr"] = addresses
        return self._activate_challenge(challenge, event_kind="benchmark.challenge_synchronized")

    def _activate_challenge(self, challenge: dict[str, Any], *, event_kind: str) -> dict[str, Any]:
        unique_code = str(challenge.get("unique_code") or "").strip()
        addresses = self._addresses(challenge.get("container_addr"))
        project = self.find_project(unique_code)
        scope = self._scope(challenge)
        target = addresses[0]
        if project is None:
            project = self.board.create_project(
                name=f"benchmark-{unique_code}",
                target=target,
                goal=self._goal(challenge),
                scope=scope,
            )
            self.board.ensure_scope_hints(project.id)
            seed_project_context_facts(self.board, project)
        else:
            if project.status == "deleting":
                raise BenchmarkError(409, "invalid_state", "Mapped project is being deleted")
            previous_target = project.target
            target_changed = previous_target != target
            if target_changed:
                self.board.archive_pending_intents_for_target(
                    project.id,
                    previous_target,
                    "benchmark_instance_target_replaced",
                )
            project = self.board.update_project_context(project.id, target=target, scope=scope)
            seed_project_context_facts(self.board, project)
            if target_changed:
                evidence_ref = f"benchmark-start:{unique_code}:{new_id('evidence')}"
                self.board.register_evidence(
                    project.id,
                    evidence_ref,
                    "benchmark_platform_start",
                    metadata={"unique_code": unique_code, "container_addr": addresses},
                )
                self.board.add_fact(
                    project.id,
                    FactCandidate(
                        subject=unique_code,
                        predicate="benchmark_instance_target",
                        object=target,
                        confidence=1.0,
                        evidence_refs=[evidence_ref],
                        attributes={"benchmark": True, "pinned": True, "previous_target": previous_target},
                    ),
                )
                self.board.add_hint(
                    project.id,
                    f"Benchmark instance address changed from {previous_target} to {target}. Replan against the current target and do not continue queued work for the old address.",
                    "benchmark-control-plane",
                )
            if project.status == "stopped":
                project = self.board.set_project_status(project.id, "running")

        self.board.add_event(
            project.id,
            event_kind,
            {
                "unique_code": unique_code,
                "container_addr": addresses,
                "container_status": "available",
            },
        )
        return {
            "challenge": challenge,
            "project": asdict(self.board.get_project(project.id)),
            "snapshot": self.board.snapshot(project.id),
        }

    def hint(self, unique_code: str, *, confirm_score_penalty: bool) -> dict[str, Any]:
        if not confirm_score_penalty:
            raise BenchmarkError(
                409,
                "hint_confirmation_required",
                "confirm_score_penalty=true is required before requesting a hint",
            )
        result = self.client.hint(unique_code)
        project = self.find_project(unique_code)
        hint_record = None
        hint_text = result.get("hint")
        if project is not None and isinstance(hint_text, str) and hint_text.strip():
            hint_record, _ = self.board.add_hint(
                project.id,
                hint_text.strip(),
                "benchmark-platform",
            )
            self.board.add_event(
                project.id,
                "benchmark.hint_received",
                {"unique_code": unique_code, "hint_id": hint_record.id, "score_penalty_confirmed": True},
            )
        return {
            **result,
            "project_id": project.id if project else None,
            "blackboard_hint": asdict(hint_record) if hint_record else None,
        }

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
        metadata = benchmark_metadata(project)
        if not metadata or metadata.get("task_key") != self.settings.task_key:
            raise ValueError("project is not managed by the configured Benchmark task")
        unique_code = str(metadata.get("unique_code", "")).strip()
        candidates = list(dict.fromkeys(str(value).strip() for value in submissions if str(value).strip()))
        if not candidates:
            raise ValueError("Benchmark completion requires at least one candidate submission")

        outcomes: list[dict[str, Any]] = []
        submission_fact_ids: list[str] = []
        latest_progress: dict[str, Any] = {}
        for candidate in candidates:
            if len(candidate) > 4096:
                raise ValueError("Benchmark candidate submission exceeds 4096 characters")
            prior = self._prior_submission(project_id, candidate)
            if prior is not None:
                outcomes.append(
                    {
                        "candidate": candidate,
                        "local_duplicate": True,
                        "correct": prior.predicate == "benchmark_flag_verified",
                        "fact_id": prior.id,
                    }
                )
                submission_fact_ids.append(prior.id)
                continue

            duplicate_correct = False
            try:
                response = self.client.submit(unique_code, candidate)
            except BenchmarkError as exc:
                if exc.code != "duplicate":
                    self.board.add_event(
                        project_id,
                        "benchmark.submission_error",
                        {"unique_code": unique_code, "code": exc.code, "message": exc.message},
                    )
                    raise
                duplicate_correct = True
                response = {"correct": True, "duplicate": True, "awarded": 0}

            correct = bool(response.get("correct"))
            if duplicate_correct:
                correct = True
            predicate = "benchmark_flag_verified" if correct else "benchmark_flag_rejected"
            evidence_ref = f"benchmark-submit:{unique_code}:{fingerprint(candidate)[:16]}:{new_id('evidence')}"
            self.board.register_evidence(
                project_id,
                evidence_ref,
                "benchmark_platform_response",
                metadata={"unique_code": unique_code, "response": response},
            )
            fact, _ = self.board.add_fact(
                project_id,
                FactCandidate(
                    subject=unique_code,
                    predicate=predicate,
                    object=candidate,
                    confidence=1.0,
                    evidence_refs=[evidence_ref],
                    attributes={
                        "benchmark": True,
                        "platform_verified": correct,
                        "pinned": correct,
                        "matched_flag_index": response.get("matched_flag_index"),
                        "awarded": response.get("awarded", 0),
                        "cumulative_score": response.get("cumulative_score"),
                    },
                ),
            )
            submission_fact_ids.append(fact.id)
            latest_progress = {
                "correct_flag_count": response.get("correct_flag_count"),
                "flag_count": response.get("total_flag_count"),
                "is_completed": (
                    int(response.get("correct_flag_count") or 0)
                    >= int(response.get("total_flag_count") or metadata.get("flag_count") or 0)
                    > 0
                ),
            }
            outcomes.append({"candidate": candidate, "fact_id": fact.id, **response})
            self.board.add_event(
                project_id,
                "benchmark.flag_verified" if correct else "benchmark.flag_rejected",
                {
                    "unique_code": unique_code,
                    "fact_id": fact.id,
                    "correct": correct,
                    "correct_flag_count": response.get("correct_flag_count"),
                    "total_flag_count": response.get("total_flag_count"),
                },
            )

        challenge = self._challenge(unique_code)
        latest_progress = {
            "correct_flag_count": int(challenge.get("correct_flag_count") or 0),
            "flag_count": int(challenge.get("flag_count") or metadata.get("flag_count") or 0),
            "is_completed": bool(challenge.get("is_completed")),
            "container_status": challenge.get("container_status"),
            "container_addr": self._addresses(challenge.get("container_addr")),
        }
        project = self._refresh_project_metadata(self.board.get_project(project_id), challenge)

        completion = None
        if latest_progress["is_completed"] or (
            latest_progress["flag_count"] > 0
            and latest_progress["correct_flag_count"] >= latest_progress["flag_count"]
        ):
            verified_facts = [
                fact
                for fact in self.board.list_facts(project_id)
                if fact.predicate == "benchmark_flag_verified"
            ]
            verified_flags = list(dict.fromkeys(fact.object for fact in verified_facts))
            evidence_ref = f"benchmark-complete:{unique_code}:{new_id('evidence')}"
            self.board.register_evidence(
                project_id,
                evidence_ref,
                "benchmark_platform_completion",
                metadata={"unique_code": unique_code, **latest_progress},
            )
            verification, _ = self.board.add_fact(
                project_id,
                FactCandidate(
                    subject=unique_code,
                    predicate="benchmark_completion_verified",
                    object=json.dumps(
                        {
                            "correct_flag_count": latest_progress["correct_flag_count"],
                            "total_flag_count": latest_progress["flag_count"],
                            "verified_flags": verified_flags,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    confidence=1.0,
                    evidence_refs=[evidence_ref],
                    attributes={"benchmark": True, "platform_verified": True, "pinned": True},
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
            description = completion_description.strip() or "Benchmark platform verified all flags"
            if verified_flags:
                description = f"{description}\nVerified flags: " + ", ".join(verified_flags)
            completion, _ = self.board.complete_project(
                project_id,
                referenced,
                description,
                worker_name,
            )

        return {
            "project": asdict(self.board.get_project(project_id)),
            "challenge": challenge,
            "outcomes": outcomes,
            "submission_fact_ids": list(dict.fromkeys(submission_fact_ids)),
            "progress": latest_progress,
            "completion": completion,
        }

    def close(self, unique_code: str) -> dict[str, Any]:
        result = self.client.close(unique_code)
        project = self.find_project(unique_code)
        if project is not None and project.status != "deleting":
            metadata = benchmark_metadata(project) or {}
            metadata.update({"container_status": "stopped", "container_addr": []})
            scope = dict(project.scope)
            scope["benchmark"] = metadata
            self.board.update_project_context(project.id, scope=scope)
            if project.status == "running":
                self.board.set_project_status(project.id, "stopped")
            self.board.add_event(
                project.id,
                "benchmark.challenge_closed",
                {"unique_code": unique_code, "closed": bool(result.get("closed"))},
            )
        return {**result, "project_id": project.id if project else None}

    def _challenge(self, unique_code: str) -> dict[str, Any]:
        for challenge in self.client.list_challenges():
            if str(challenge.get("unique_code", "")).strip() == unique_code:
                return dict(challenge)
        raise BenchmarkError(404, "challenge_not_found", f"Challenge not found: {unique_code}")

    def _refresh_project_metadata(self, project: Project, challenge: dict[str, Any]) -> Project:
        metadata = benchmark_metadata(project)
        if not metadata:
            return project
        metadata.update(self._metadata(challenge))
        scope = dict(project.scope)
        scope["benchmark"] = metadata
        addresses = self._addresses(challenge.get("container_addr"))
        if addresses:
            scope["targets"] = addresses
            return self.board.update_project_context(project.id, target=addresses[0], scope=scope)
        return self.board.update_project_context(project.id, scope=scope)

    def _scope(self, challenge: dict[str, Any]) -> dict[str, Any]:
        addresses = self._addresses(challenge.get("container_addr"))
        return {
            "targets": addresses,
            "origin": addresses[0] if addresses else str(challenge.get("unique_code", "")),
            "bootstrap_enabled": False,
            "hints": [],
            "benchmark": {"managed": True, "task_key": self.settings.task_key, **self._metadata(challenge)},
        }

    def _metadata(self, challenge: dict[str, Any]) -> dict[str, Any]:
        return {
            "unique_code": str(challenge.get("unique_code", "")).strip(),
            "description": challenge.get("description"),
            "difficulty": challenge.get("difficulty"),
            "level": challenge.get("level"),
            "total_score": challenge.get("total_score"),
            "flag_count": int(challenge.get("flag_count") or 0),
            "correct_flag_count": int(challenge.get("correct_flag_count") or 0),
            "is_completed": bool(challenge.get("is_completed")),
            "container_status": challenge.get("container_status"),
            "container_addr": self._addresses(challenge.get("container_addr")),
        }

    @staticmethod
    def _goal(challenge: dict[str, Any]) -> str:
        unique_code = str(challenge.get("unique_code", "")).strip()
        description = str(challenge.get("description") or "No description supplied").strip()
        flag_count = int(challenge.get("flag_count") or 0)
        return (
            f"Solve Benchmark challenge {unique_code}. {description} "
            f"Find all {flag_count or 'required'} answer values in their original format, preserve evidence, "
            "and return exact candidates without assuming a prefix, wrapper, or syntax "
            "through Reason for platform verification."
        )

    @staticmethod
    def _addresses(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    def _prior_submission(self, project_id: str, candidate: str) -> Fact | None:
        for fact in self.board.list_facts(project_id):
            if fact.predicate in {"benchmark_flag_verified", "benchmark_flag_rejected"} and fact.object == candidate:
                return fact
        return None
