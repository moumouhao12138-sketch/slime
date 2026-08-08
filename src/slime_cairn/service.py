from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Callable

from .blackboard import Blackboard
from .dispatcher import (
    AsyncDispatcher,
    DispatcherConfig,
    GlobalCapacity,
    REQUIRED_TASK_MODES,
    WorkerPool,
)
from .models import Project, new_id
from .scheduler import Scheduler
from .seeding import seed_project_context_facts
from .workspace import IsolatedWorkspace


@dataclass(slots=True)
class ProjectRuntimeBinding:
    workspace: IsolatedWorkspace | None = None
    cleanup: Callable[[], None] | None = None


RuntimeFactory = Callable[[Project], ProjectRuntimeBinding]


class DispatcherService:
    """Discover projects and keep one fair Dispatcher loop alive for each of them."""

    def __init__(
        self,
        board: Blackboard,
        worker_pool: WorkerPool,
        runtime_factory: RuntimeFactory,
        config: DispatcherConfig | None = None,
        discovery_interval: float = 0.25,
    ) -> None:
        if discovery_interval <= 0:
            raise ValueError("discovery_interval 必须大于 0")
        self.board = board
        self.worker_pool = worker_pool
        self.runtime_factory = runtime_factory
        self.config = config or DispatcherConfig()
        self.discovery_interval = discovery_interval
        self.id = new_id("dispatcher-service")
        self.capacity = GlobalCapacity(self.config.max_workers)
        self.dispatchers: dict[str, AsyncDispatcher] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.bindings: dict[str, ProjectRuntimeBinding] = {}
        self.errors: list[dict] = []
        self._reported_task_failures: set[str] = set()
        self._stop_event = asyncio.Event()
        self._state = "created"
        self._started_at = 0.0

    async def register_project(self, project_id: str) -> AsyncDispatcher | None:
        if project_id in self.dispatchers:
            return self.dispatchers[project_id]
        project = self.board.get_project(project_id)
        # ``discover_projects`` only supplies running projects, but an API
        # deletion can land between discovery and this point.
        if project.status != "running":
            return None
        imported_hints = self.board.ensure_scope_hints(project_id)
        print(
            f"[service] register_project id={project_id} name={project.name} target={project.target}",
            flush=True,
        )
        binding = self.runtime_factory(project)
        if binding.workspace is None:
            raise RuntimeError("Cairn project runtime must provide a workspace")
        try:
            current_status = self.board.get_project(project_id).status
        except KeyError:
            current_status = "deleted"
        if current_status != "running":
            await self._cleanup_binding(project_id, binding)
            return None
        try:
            scheduler = Scheduler(
                self.board,
                project_id,
                mind=self.worker_pool.any_mind(),
                workspace=binding.workspace,
            )
            # API-created projects already have these anchors. Calling the helper
            # here is idempotent and also covers projects inserted directly into
            # the Blackboard before the service starts.
            seed_project_context_facts(self.board, project)
            bootstrap_worker_configured = "bootstrap" in self.worker_pool.configured_task_types()
            bootstrap_active = bool(project.scope.get("bootstrap_enabled", True)) and bootstrap_worker_configured
            if bootstrap_active:
                if not self.board.list_intents(project_id):
                    scheduler.seed(project.target)
            else:
                archive_reason = (
                    "bootstrap_disabled"
                    if not bool(project.scope.get("bootstrap_enabled", True))
                    else "bootstrap_worker_unavailable"
                )
                self.board.archive_pending_bootstrap_intents(project_id, archive_reason)
        except (KeyError, ValueError):
            try:
                raced_status = self.board.get_project(project_id).status
            except KeyError:
                raced_status = "deleted"
            if raced_status != "running":
                await self._cleanup_binding(project_id, binding)
                return None
            raise
        dispatcher = AsyncDispatcher(
            self.board,
            project_id,
            scheduler,
            self.worker_pool,
            self.config,
            global_capacity=self.capacity,
        )
        self.bindings[project_id] = binding
        self.dispatchers[project_id] = dispatcher
        self.tasks[project_id] = asyncio.create_task(
            dispatcher.serve(),
            name=f"slime-dispatch-{project_id}",
        )
        self.board.add_event(
            project_id,
            "dispatcher_service.project_registered",
            {
                "service_id": self.id,
                "dispatcher_id": dispatcher.id,
                "imported_scope_hint_ids": [hint.id for hint in imported_hints],
            },
        )
        return dispatcher

    async def discover_projects(self) -> list[str]:
        registered: list[str] = []
        active_slots = sum(
            1
            for project_id, task in self.tasks.items()
            if project_id in self.dispatchers and not task.done()
        )
        for project in self.board.list_projects("running"):
            if project.id not in self.dispatchers:
                if active_slots >= self.config.max_running_projects:
                    break
                dispatcher = await self.register_project(project.id)
                if dispatcher is not None:
                    registered.append(project.id)
                    active_slots += 1
        return registered

    def _record_cleanup_error(self, project_id: str, exc: Exception) -> None:
        self.errors.append(
            {
                "kind": "project_cleanup_error",
                "project_id": project_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    async def _cleanup_binding(self, project_id: str, binding: ProjectRuntimeBinding | None) -> bool:
        if binding is None or binding.cleanup is None:
            return True
        try:
            await asyncio.to_thread(binding.cleanup)
            return True
        except Exception as exc:
            self._record_cleanup_error(project_id, exc)
            return False

    async def _remove_deleted_runtime(self, project_id: str) -> bool:
        """Invoke the optional persistent-runtime removal hook.

        Test runtime factories only need their binding cleanup.  The native
        Docker factory additionally exposes ``delete_project`` to remove a
        stopped container and its bind-mounted workspace without creating a
        new container for an unregistered project.
        """

        remover = getattr(self.runtime_factory, "delete_project", None)
        if not callable(remover):
            return True
        try:
            await asyncio.to_thread(remover, project_id)
            return True
        except Exception as exc:
            self._record_cleanup_error(project_id, exc)
            return False

    async def _finalize_deleted_project(
        self,
        project_id: str,
        binding: ProjectRuntimeBinding | None = None,
    ) -> bool:
        """Remove runtime resources, then make the Blackboard deletion final."""

        if not await self._cleanup_binding(project_id, binding):
            return False
        if not await self._remove_deleted_runtime(project_id):
            return False
        try:
            await asyncio.to_thread(self.board.delete_project, project_id)
            return True
        except KeyError:
            return True
        except Exception as exc:
            self._record_cleanup_error(project_id, exc)
            return False

    async def _finalize_unregistered_deletions(self) -> None:
        """Clean deletion requests made before a project acquired a dispatcher."""

        for project in self.board.list_projects("deleting"):
            project_id = project.id
            if project_id in self.tasks or project_id in self.dispatchers:
                continue
            await self._finalize_deleted_project(project_id)

    async def _reconcile_dispatchers(self) -> None:
        """Stop terminal projects, reap their runtime, and permit a later reopen."""

        for project_id, dispatcher in list(self.dispatchers.items()):
            try:
                status = self.board.get_project(project_id).status
            except KeyError:
                status = "deleted"
            if status != "running":
                dispatcher.request_stop()

        for project_id, task in list(self.tasks.items()):
            if not task.done() or self._stop_event.is_set():
                continue
            if project_id not in self._reported_task_failures:
                self._reported_task_failures.add(project_id)
                try:
                    task.result()
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    self.errors.append(
                        {
                            "kind": "project_dispatcher_crashed",
                            "project_id": project_id,
                            "error": message,
                        }
                    )
                    # A project may have been hard-deleted by an older server
                    # process. Event logging intentionally becomes a no-op in
                    # that case.
                    self.board.add_event(
                        project_id,
                        "dispatcher_service.project_crashed",
                        {"service_id": self.id, "error": message},
                    )
            binding = self.bindings.get(project_id)
            try:
                status = self.board.get_project(project_id).status
            except KeyError:
                status = "deleted"
            if status == "deleting":
                if not await self._finalize_deleted_project(project_id, binding):
                    continue
            elif not await self._cleanup_binding(project_id, binding):
                continue
            self.tasks.pop(project_id, None)
            self.dispatchers.pop(project_id, None)
            self.bindings.pop(project_id, None)
            self._reported_task_failures.discard(project_id)

    async def serve(self) -> dict:
        self._stop_event.clear()
        self._state = "running"
        self._started_at = time.time()
        print(f"[service] started id={self.id}", flush=True)
        health = await asyncio.to_thread(self.worker_pool.healthcheck_all)
        healthy_modes = self.worker_pool.configured_task_types(healthy_only=True)
        missing = REQUIRED_TASK_MODES - healthy_modes
        for worker_name, result in health.items():
            status = result.get("status")
            detail = str(result.get("detail", "")).replace("\n", " ")[:300]
            print(
                f"[service] worker_health worker={worker_name} "
                f"healthy={bool(result.get('healthy'))} "
                f"status={status if status is not None else '-'} detail={detail or '-'}",
                flush=True,
            )
        if self.config.worker_healthcheck != "disabled" and missing:
            message = (
                "worker startup healthcheck left required task types unavailable: "
                f"{sorted(missing)}"
            )
            self.errors.append(
                {
                    "kind": "worker_startup_health_failed",
                    "missing_task_types": sorted(missing),
                    "workers": health,
                }
            )
            self._state = "failed"
            print(f"[service] startup_failed error={message}", flush=True)
            raise RuntimeError(message)
        while not self._stop_event.is_set():
            await self.discover_projects()
            await self._reconcile_dispatchers()
            await self._finalize_unregistered_deletions()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.discovery_interval,
                )
            except asyncio.TimeoutError:
                pass

        self._state = "draining"
        for dispatcher in self.dispatchers.values():
            dispatcher.request_stop()
        if self.tasks:
            results = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            for project_id, result in zip(self.tasks, results):
                if isinstance(result, Exception):
                    self.errors.append(
                        {
                            "kind": "project_dispatcher_stop_error",
                            "project_id": project_id,
                            "error": f"{type(result).__name__}: {result}",
                        }
                    )
        for project_id, binding in self.bindings.items():
            if binding.cleanup is None:
                continue
            try:
                await asyncio.to_thread(binding.cleanup)
            except Exception as exc:
                self.errors.append(
                    {
                        "kind": "project_cleanup_error",
                        "project_id": project_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        self._state = "stopped"
        print(f"[service] stopped id={self.id}", flush=True)
        return self.status()

    def request_stop(self) -> None:
        self._stop_event.set()
        for dispatcher in self.dispatchers.values():
            dispatcher.request_stop()

    def status(self) -> dict:
        return {
            "service_id": self.id,
            "state": self._state,
            "started_at": self._started_at,
            "global_capacity": self.capacity.snapshot(),
            "worker_pool": self.worker_pool.snapshot(),
            "projects": {
                project_id: dispatcher.status()
                for project_id, dispatcher in self.dispatchers.items()
            },
            "errors": list(self.errors),
        }
