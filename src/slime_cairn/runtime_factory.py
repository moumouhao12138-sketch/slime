from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import subprocess
from urllib.parse import urlparse

from .blackboard import Blackboard
from .execution import CommandExecution
from .models import Project
from .service import ProjectRuntimeBinding
from .worker_manager import (
    LAB_NETWORK_ADMIN_PROFILE,
    RAW_NETWORK_PROFILE,
    STANDARD_PROFILE,
    ContainerResourceLimits,
    WorkerManager,
    WorkerProfile,
)
from .execution import AgentConfigMount
from .workspace import IsolatedWorkspace


PROFILES = {
    profile.name: profile
    for profile in (STANDARD_PROFILE, RAW_NETWORK_PROFILE, LAB_NETWORK_ADMIN_PROFILE)
}


@dataclass(slots=True)
class CommandExecutor:
    timeout: int = 30

    def __call__(self, argv: list[str]) -> CommandExecution:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
            return CommandExecution(completed.returncode, completed.stdout, completed.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandExecution(127, "", f"{type(exc).__name__}: {exc}")


class DockerCairnRuntimeFactory:
    """One persistent Kali runtime per project, matching Cairn's container path.

    Native Agent CLIs execute inside this runtime and directly use Kali shell,
    files, tools and shared project artifacts.  The factory deliberately does
    not expose model-facing function tools or a host HTTP bridge.
    """

    def __init__(
        self,
        board: Blackboard,
        workspaces_root: str | Path,
        profile: str | WorkerProfile = STANDARD_PROFILE,
        docker_binary: str = "docker",
        lab_network: str = "slime-cairn-lab",
        stop_on_cleanup: bool = False,
        executor: CommandExecutor | None = None,
        agent_config_mounts: tuple[AgentConfigMount, ...] = (),
        resource_limits: ContainerResourceLimits | None = None,
    ) -> None:
        self.board = board
        if isinstance(profile, str):
            try:
                profile = PROFILES[profile]
            except KeyError as exc:
                raise ValueError(f"unknown Worker Profile: {profile}") from exc
        self.profile = profile
        self.manager = WorkerManager(
            workspaces_root,
            docker_binary,
            lab_network,
            agent_config_mounts=agent_config_mounts,
            resource_limits=resource_limits,
            verify_image_identity=True,
        )
        self.stop_on_cleanup = stop_on_cleanup
        self.executor = executor or CommandExecutor()

    @staticmethod
    def _host_from_target(target: str) -> str:
        parsed = urlparse(str(target))
        if parsed.hostname:
            return parsed.hostname
        return str(target).split("/")[0].split(":")[0]

    @classmethod
    def _extra_hosts_for_targets(cls, targets: list[str]) -> tuple[str, ...]:
        entries: list[str] = []
        for target in targets:
            host = cls._host_from_target(target).strip()
            if not host or host.replace(".", "").isdigit():
                continue
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                continue
            ips = sorted({info[4][0] for info in infos if info and info[4]})
            if ips:
                entries.append(f"{host}:{ips[0]}")
        return tuple(dict.fromkeys(entries))

    def delete_project(self, project_id: str) -> None:
        """Remove a project runtime after DispatcherService has drained it."""

        self.manager.remove_project(project_id, self.executor, self.profile)

    def __call__(self, project: Project) -> ProjectRuntimeBinding:
        allowed = [str(item) for item in project.scope.get("targets", [project.target])]
        extra_hosts = self._extra_hosts_for_targets(allowed)
        if extra_hosts:
            self.manager.set_extra_hosts(project.id, extra_hosts)
        agent_homes = self.manager.project_agent_homes(project.id, self.profile)
        lifecycle = self.manager.ensure_started(project.id, self.executor, self.profile)
        backend = self.manager.backend_for(project.id, self.profile)
        workspace = IsolatedWorkspace(backend.workspace_root)
        workspace.root.mkdir(parents=True, exist_ok=True)
        (workspace.root / "shared").mkdir(parents=True, exist_ok=True)
        self.board.add_event(
            project.id,
            "cairn_runtime.ready",
            {
                "profile": self.profile.name,
                "lifecycle": lifecycle,
                "container": backend.config.container_name,
                "extra_hosts": extra_hosts,
                "agent_homes": agent_homes,
            },
        )

        def cleanup() -> None:
            # A stopped or completed project must not retain an active Kali
            # process.  Its bind-mounted workspace remains intact, so a later
            # resume recreates only the running container state.
            try:
                project_status = self.board.get_project(project.id).status
            except KeyError:
                project_status = "deleted"
            if self.stop_on_cleanup or project_status in {"stopped", "completed", "deleting", "deleted"}:
                self.executor(backend.stop_command())

        return ProjectRuntimeBinding(workspace=workspace, cleanup=cleanup)
