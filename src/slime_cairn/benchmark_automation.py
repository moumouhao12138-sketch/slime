"""Persistent three-slot automation for the TSec Benchmark platform."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .benchmark import BenchmarkClient, BenchmarkError, BenchmarkSettings
from .benchmark_runtime import BenchmarkProjectController
from .blackboard import Blackboard


ACTIVE_CONTAINER_STATES = {"pending", "available", "stop_pending"}


class BenchmarkAutomationService:
    """Keep Benchmark instance slots full until every challenge is completed."""

    def __init__(
        self,
        board: Blackboard,
        client: BenchmarkClient,
        settings: BenchmarkSettings,
        *,
        interval: float = 3.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("Benchmark automation interval must be greater than zero")
        self.board = board
        self.client = client
        self.settings = settings
        self.controller = BenchmarkProjectController(board, client, settings)
        self.interval = float(interval)
        self._stop_event = asyncio.Event()

    @staticmethod
    def _code(challenge: dict[str, Any]) -> str:
        return str(challenge.get("unique_code") or "").strip()

    @staticmethod
    def _container_status(challenge: dict[str, Any]) -> str:
        return str(challenge.get("container_status") or "stopped").strip().lower()

    def _active_entry(self, challenge: dict[str, Any]) -> dict[str, Any]:
        code = self._code(challenge)
        project = self.controller.find_project(code) if code else None
        return {
            "unique_code": code,
            "container_status": self._container_status(challenge),
            "project_id": project.id if project and project.status != "deleting" else None,
            "project_status": project.status if project and project.status != "deleting" else None,
        }

    def _state(
        self,
        challenges: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        *,
        status: str,
        last_error: str = "",
    ) -> dict[str, Any]:
        active = [
            self._active_entry(challenge)
            for challenge in challenges
            if not bool(challenge.get("is_completed"))
            and self._container_status(challenge) in ACTIVE_CONTAINER_STATES
        ]
        queued = [
            challenge
            for challenge in challenges
            if not bool(challenge.get("is_completed"))
            and self._container_status(challenge) == "stopped"
        ]
        return {
            "status": status,
            "active_count": len(active),
            "queued_count": len(queued),
            "completed_count": sum(bool(item.get("is_completed")) for item in challenges),
            "total_count": len(challenges),
            "active": active,
            "last_actions": actions[-12:],
            "last_error": last_error,
            "last_tick_at": time.time(),
        }

    def tick(self) -> dict[str, Any]:
        control = self.board.get_benchmark_automation(self.settings.task_key)
        if not control["enabled"]:
            return control

        challenges = self.controller.list_challenges()
        actions: list[dict[str, Any]] = []
        errors: list[str] = []

        # A previous manual run or process restart may leave a live platform
        # instance mapped to a stopped local project. Reattach it before
        # counting slots so automation actually resumes the AI work.
        for challenge in challenges:
            if bool(challenge.get("is_completed")) or self._container_status(challenge) != "available":
                continue
            code = self._code(challenge)
            project = self.controller.find_project(code) if code else None
            if project is not None and project.status == "running":
                continue
            try:
                synced = self.controller.sync_active(challenge)
                challenge["project_id"] = synced["project"]["id"]
                challenge["project_status"] = synced["project"]["status"]
                actions.append(
                    {
                        "action": "resumed",
                        "unique_code": code,
                        "project_id": synced["project"]["id"],
                    }
                )
            except BenchmarkError as exc:
                errors.append(f"resume {code}: {exc.code}: {exc.message}")

        # Completion is platform-confirmed before a project enters completed.
        # Close those instances first so their slots can be reused in this tick.
        for challenge in challenges:
            code = self._code(challenge)
            if not code or not bool(challenge.get("is_completed")):
                continue
            if self._container_status(challenge) == "stopped":
                continue
            try:
                result = self.controller.close(code)
                challenge["container_status"] = "stopped"
                challenge["container_addr"] = []
                actions.append({"action": "closed_completed", "unique_code": code, "closed": bool(result.get("closed"))})
            except BenchmarkError as exc:
                errors.append(f"close {code}: {exc.code}: {exc.message}")

        parallelism = int(control["parallelism"])
        active_count = sum(
            not bool(challenge.get("is_completed"))
            and self._container_status(challenge) in ACTIVE_CONTAINER_STATES
            for challenge in challenges
        )
        available_slots = max(0, parallelism - active_count)

        for challenge in challenges:
            if available_slots <= 0:
                break
            code = self._code(challenge)
            if (
                not code
                or bool(challenge.get("is_completed"))
                or self._container_status(challenge) != "stopped"
            ):
                continue
            try:
                result = self.controller.start(code)
                started = result["challenge"]
                challenge.update(started)
                challenge["project_id"] = result["project"]["id"]
                challenge["project_status"] = result["project"]["status"]
                actions.append(
                    {
                        "action": "started",
                        "unique_code": code,
                        "project_id": result["project"]["id"],
                    }
                )
                available_slots -= 1
            except BenchmarkError as exc:
                errors.append(f"start {code}: {exc.code}: {exc.message}")
                if exc.code in {"invalid_state", "resource_unavailable", "task_not_found"}:
                    break

        completed_count = sum(bool(item.get("is_completed")) for item in challenges)
        all_completed = bool(challenges) and completed_count == len(challenges)
        status = "completed" if all_completed else ("degraded" if errors else "running")
        state = self._state(
            challenges,
            actions,
            status=status,
            last_error=" | ".join(errors)[:2000],
        )
        if all_completed:
            self.board.configure_benchmark_automation(
                self.settings.task_key,
                enabled=False,
                parallelism=parallelism,
            )
        saved = self.board.save_benchmark_automation_state(self.settings.task_key, state)
        if actions:
            summary = " ".join(f"{item['action']}:{item['unique_code']}" for item in actions)
            print(f"[benchmark-auto] {summary}", flush=True)
        return saved

    async def serve(self) -> dict[str, Any]:
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                control = await asyncio.to_thread(
                    self.board.get_benchmark_automation,
                    self.settings.task_key,
                )
                if control["enabled"]:
                    try:
                        await asyncio.to_thread(self.tick)
                    except Exception as exc:
                        failed = {
                            **control,
                            "status": "error",
                            "last_error": f"{type(exc).__name__}: {exc}"[:2000],
                            "last_tick_at": time.time(),
                        }
                        await asyncio.to_thread(
                            self.board.save_benchmark_automation_state,
                            self.settings.task_key,
                            failed,
                        )
                        print(
                            f"[benchmark-auto] tick_failed error={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.client.close_client()
        return self.board.get_benchmark_automation(self.settings.task_key)

    def request_stop(self) -> None:
        self._stop_event.set()
