from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Callable

from .execution import (
    DEFAULT_CONTAINER_CPUS,
    DEFAULT_CONTAINER_MEMORY,
    DEFAULT_CONTAINER_PIDS_LIMIT,
    DEFAULT_WORKER_IMAGE,
    AgentConfigMount,
    CommandExecution,
    PersistentDockerBackend,
    PersistentDockerConfig,
)


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    name: str
    image: str
    network: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContainerResourceLimits:
    """Docker cgroup limits shared by every project runtime."""

    pids_limit: int | None = DEFAULT_CONTAINER_PIDS_LIMIT
    memory: str | None = DEFAULT_CONTAINER_MEMORY
    cpus: str | None = DEFAULT_CONTAINER_CPUS
    init: bool = True

    def __post_init__(self) -> None:
        if self.pids_limit is not None and (
            isinstance(self.pids_limit, bool) or self.pids_limit < 1
        ):
            raise ValueError("container.pids_limit must be a positive integer or null")
        if self.memory is not None and re.fullmatch(
            r"\s*\d+(?:\.\d+)?\s*[kmgt]?b?\s*",
            str(self.memory),
            re.IGNORECASE,
        ) is None:
            raise ValueError("container.memory must be a Docker memory value or null")
        if self.cpus is not None:
            try:
                cpus = float(self.cpus)
            except (TypeError, ValueError) as exc:
                raise ValueError("container.cpus must be positive or null") from exc
            if not math.isfinite(cpus) or cpus <= 0:
                raise ValueError("container.cpus must be positive or null")
        if not isinstance(self.init, bool):
            raise ValueError("container.init must be boolean")

    @classmethod
    def from_mapping(cls, value: object) -> "ContainerResourceLimits":
        data = dict(value or {}) if isinstance(value, dict) else {}
        raw_pids = data.get("pids_limit", DEFAULT_CONTAINER_PIDS_LIMIT)
        if raw_pids is not None and isinstance(raw_pids, bool):
            raise ValueError("container.pids_limit must be a positive integer or null")
        raw_memory = data.get("memory", DEFAULT_CONTAINER_MEMORY)
        raw_cpus = data.get("cpus", DEFAULT_CONTAINER_CPUS)
        return cls(
            pids_limit=None if raw_pids is None else int(raw_pids),
            memory=None if raw_memory is None else str(raw_memory),
            cpus=None if raw_cpus is None else str(raw_cpus),
            init=data.get("init", True),
        )


STANDARD_PROFILE = WorkerProfile("standard", DEFAULT_WORKER_IMAGE, "bridge")
# Native model CLIs and remote CTF fixtures both need ordinary outbound
# connectivity.  The private ``slime-lab`` network is intentionally
# internal, so it belongs only to the explicit lab-admin profile below.
RAW_NETWORK_PROFILE = WorkerProfile("raw-network", DEFAULT_WORKER_IMAGE, "bridge", ("NET_RAW",))
LAB_NETWORK_ADMIN_PROFILE = WorkerProfile(
    "lab-network-admin",
    DEFAULT_WORKER_IMAGE,
    "lab-only",
    ("NET_RAW", "NET_ADMIN"),
)

_AGENT_TYPE_ALIASES = {
    "codex": "codex",
    "codex-cli": "codex",
    "claude": "claude",
    "claude-code": "claude",
    "claudecode": "claude",
    "pi": "pi",
    "pi-cli": "pi",
}


def active_agent_config_mounts(value: object, workers: object) -> object:
    """Drop host-config mounts for disabled or absent known Agent types."""

    enabled: set[str] = set()
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict) or worker.get("enabled", True) is False:
                continue
            agent = _AGENT_TYPE_ALIASES.get(str(worker.get("type", "")).strip().lower())
            if agent:
                enabled.add(agent)

    def keep(agent: object) -> bool:
        raw = str(agent).strip().lower()
        canonical = _AGENT_TYPE_ALIASES.get(raw)
        return canonical is None or canonical in enabled

    if isinstance(value, dict):
        if {"agent", "source", "target"} & set(value):
            return value if keep(value.get("agent", "")) else None
        return {agent: item for agent, item in value.items() if keep(agent)}
    if isinstance(value, list):
        return [
            item
            for item in value
            if not isinstance(item, dict) or keep(item.get("agent", ""))
        ]
    return value


def parse_agent_config_mounts(
    value: object,
    *,
    base_directory: str | Path | None = None,
    require_existing_source: bool = True,
) -> tuple[AgentConfigMount, ...]:
    """Parse JSON-friendly Codex/Claude/Pi read-only bind-mount entries.

    Accepted forms are either a keyed object::

        {"codex": {"source": "...", "target": "/host-config/codex"}}

    or a list of objects with an explicit ``agent`` field.  Relative sources
    resolve from ``base_directory`` so a dispatch configuration can keep paths
    local to itself.
    """

    if value is None or value == {} or value == []:
        return ()
    root = Path(base_directory).expanduser().resolve() if base_directory is not None else None

    entries: list[tuple[str, object]] = []
    if isinstance(value, dict):
        if {"agent", "source", "target"} & set(value):
            entries.append((str(value.get("agent", "")), value))
        else:
            entries.extend((str(agent), item) for agent, item in value.items())
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("agent_config_mounts 列表项必须是 object")
            entries.append((str(item.get("agent", "")), item))
    else:
        raise ValueError("agent_config_mounts 必须是 object 或 array")

    mounts: list[AgentConfigMount] = []
    for agent, raw in entries:
        if not isinstance(raw, dict):
            raise ValueError(f"Agent {agent or '<unknown>'} 的配置挂载必须是 object")
        source_value = raw.get("source")
        target_value = raw.get("target")
        if source_value is None or target_value is None:
            raise ValueError(f"Agent {agent or '<unknown>'} 的配置挂载需要 source 和 target")
        source_text = os.path.expandvars(str(source_value).strip())
        source = Path(source_text).expanduser()
        if not source.is_absolute() and root is not None:
            source = root / source
        mount = AgentConfigMount(agent, source, str(target_value))
        if require_existing_source and not mount.source.exists():
            raise FileNotFoundError(f"Agent {mount.agent} 配置挂载 source 不存在: {mount.source}")
        mounts.append(mount)

    targets = [mount.target for mount in mounts]
    if len(targets) != len(set(targets)):
        raise ValueError("agent_config_mounts 中存在重复 target")
    return tuple(mounts)


class WorkerManager:
    """One persistent, broad-tool Kali worker per project; pseudopods stay temporary."""

    def __init__(
        self,
        workspaces_root: str | Path,
        docker_binary: str = "docker",
        lab_network: str = "slime-lab",
        workspace_volume: str = "",
        agent_config_mounts: tuple[AgentConfigMount, ...] | list[AgentConfigMount] = (),
        resource_limits: ContainerResourceLimits | None = None,
        verify_image_identity: bool = False,
    ) -> None:
        self.workspaces_root = Path(workspaces_root).resolve()
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.docker_binary = docker_binary
        self.lab_network = lab_network
        self.workspace_volume = str(workspace_volume).strip()
        self.resource_limits = resource_limits or ContainerResourceLimits()
        self.verify_image_identity = bool(verify_image_identity)
        self.backends: dict[str, PersistentDockerBackend] = {}
        self.profiles: dict[str, WorkerProfile] = {}
        self.extra_hosts: dict[str, tuple[str, ...]] = {}
        self.agent_config_mounts = self._normalize_agent_config_mounts(agent_config_mounts)

    @staticmethod
    def _normalize_agent_config_mounts(
        mounts: tuple[AgentConfigMount, ...] | list[AgentConfigMount],
    ) -> tuple[AgentConfigMount, ...]:
        normalized = tuple(mounts)
        if any(not isinstance(mount, AgentConfigMount) for mount in normalized):
            raise TypeError("agent_config_mounts 必须由 AgentConfigMount 组成")
        targets = [mount.target for mount in normalized]
        if len(targets) != len(set(targets)):
            raise ValueError("Agent 配置挂载 target 不能重复")
        return normalized

    def set_agent_config_mounts(
        self,
        mounts: tuple[AgentConfigMount, ...] | list[AgentConfigMount],
    ) -> None:
        """Set shared host Agent config mounts before any project Worker exists."""

        normalized = self._normalize_agent_config_mounts(mounts)
        if self.backends and normalized != self.agent_config_mounts:
            raise ValueError("项目 Kali Worker 已创建，Agent 配置挂载需要在创建前设置")
        self.agent_config_mounts = normalized

    def set_extra_hosts(self, project_id: str, hosts: tuple[str, ...] | list[str]) -> None:
        cleaned = tuple(dict.fromkeys(str(item).strip() for item in hosts if str(item).strip()))
        if project_id in self.backends and self.extra_hosts.get(project_id, ()) != cleaned:
            raise ValueError(
                f"项目 {project_id} 的 Worker 已创建，extra_hosts 需要在创建前设置"
            )
        self.extra_hosts[project_id] = cleaned

    def project_agent_homes(
        self,
        project_id: str,
        profile: WorkerProfile = STANDARD_PROFILE,
    ) -> dict[str, str]:
        """Copy the small, stable part of each host CLI home into project state.

        The host tree stays read-only at container creation time.  CLI caches
        and task sessions belong in the project's writable home, which mirrors
        Cairn's persistent per-project environment and avoids cross-project
        state collisions.
        """

        backend = self.backend_for(project_id, profile)
        homes_root = backend.workspace_root / "shared" / "agent-homes"
        copied: dict[str, str] = {}
        allowed_names = {
            "codex": {"config.toml", "auth.json"},
            "claude": {"settings.json", "settings.local.json", ".credentials.json"},
            "pi": {"auth.json", "models.json", "settings.json"},
        }
        for mount in self.agent_config_mounts:
            destination = homes_root / mount.agent
            destination.mkdir(parents=True, exist_ok=True)
            names = set(allowed_names[mount.agent])
            if mount.agent == "codex":
                names.update(path.name for path in mount.source.glob("*.md") if path.is_file())
            copied_count = 0
            for name in sorted(names):
                source = mount.source / name
                if not source.is_file():
                    continue
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied_count += 1
            copied[mount.agent] = str(destination.relative_to(backend.workspace_root)).replace("\\", "/")
            if copied_count == 0:
                raise RuntimeError(f"Agent {mount.agent} host config contains no usable files")
        return copied

    @staticmethod
    def _safe_project_name(project_id: str) -> str:
        safe_project = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(project_id))[:48]
        if not safe_project or safe_project in {".", ".."}:
            raise ValueError("project_id cannot resolve to a workspace root")
        return safe_project

    def project_workspace_root(self, project_id: str) -> Path:
        """Return the project's only permitted directory beneath workspaces_root."""

        root = (self.workspaces_root / self._safe_project_name(project_id)).resolve()
        try:
            root.relative_to(self.workspaces_root)
        except ValueError as exc:
            raise ValueError("project workspace escaped workspaces_root") from exc
        return root

    def backend_for(self, project_id: str, profile: WorkerProfile = STANDARD_PROFILE) -> PersistentDockerBackend:
        if project_id in self.backends:
            if self.profiles[project_id] != profile:
                raise ValueError(
                    f"项目 {project_id} 已使用 {self.profiles[project_id].name} Profile；"
                    "运行中的 Worker 不允许静默切换权限"
                )
            return self.backends[project_id]
        safe_project = self._safe_project_name(project_id)
        root = self.project_workspace_root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        network = self.lab_network if profile.network == "lab-only" else profile.network
        config = PersistentDockerConfig(
            image=profile.image,
            docker_binary=self.docker_binary,
            network=network,
            container_name=f"slime-{safe_project}",
            cap_add=profile.capabilities,
            extra_hosts=self.extra_hosts.get(project_id, ()),
            agent_config_mounts=self.agent_config_mounts,
            pids_limit=self.resource_limits.pids_limit,
            memory=self.resource_limits.memory,
            cpus=self.resource_limits.cpus,
            init=self.resource_limits.init,
            workspace_volume=self.workspace_volume,
            workspace_volume_subpath=safe_project if self.workspace_volume else "",
        )
        backend = PersistentDockerBackend(root, config)
        self.backends[project_id] = backend
        self.profiles[project_id] = profile
        return backend

    @staticmethod
    def _container_was_already_removed(result: CommandExecution) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return any(marker in text for marker in ("no such container", "no such object", "not found"))

    def remove_project(
        self,
        project_id: str,
        executor: Callable[[list[str]], CommandExecution],
        profile: WorkerProfile = STANDARD_PROFILE,
    ) -> None:
        """Remove the persistent container and its project-only workspace.

        This is used only after the Blackboard has fenced a project in
        ``deleting`` state.  A failed container removal leaves the workspace
        intact so the service can retry without losing the deletion record.
        """

        root = self.project_workspace_root(project_id)
        backend = self.backends.get(project_id)
        if backend is None:
            safe_project = self._safe_project_name(project_id)
            backend = PersistentDockerBackend(
                root,
                PersistentDockerConfig(
                    image=profile.image,
                    docker_binary=self.docker_binary,
                    container_name=f"slime-{safe_project}",
                    pids_limit=self.resource_limits.pids_limit,
                    memory=self.resource_limits.memory,
                    cpus=self.resource_limits.cpus,
                    init=self.resource_limits.init,
                    workspace_volume=self.workspace_volume,
                    workspace_volume_subpath=safe_project if self.workspace_volume else "",
                ),
            )

        removed = executor(backend.remove_command(force=True))
        if removed.exit_code != 0 and not self._container_was_already_removed(removed):
            detail = (removed.stderr or removed.stdout).strip()
            raise RuntimeError(f"failed to remove project Kali Worker: {detail}")

        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError as exc:
            raise RuntimeError(f"failed to remove project workspace {root}: {exc}") from exc

        self.backends.pop(project_id, None)
        self.profiles.pop(project_id, None)
        self.extra_hosts.pop(project_id, None)

    def lifecycle_plan(self, project_id: str, profile: WorkerProfile = STANDARD_PROFILE) -> dict[str, list[str]]:
        backend = self.backend_for(project_id, profile)
        return {
            "inspect": backend.inspect_command(),
            "inspect_image": backend.inspect_image_command(),
            "inspect_container_image_id": backend.inspect_container_image_id_command(),
            "inspect_image_id": backend.inspect_image_id_command(),
            "inspect_network": backend.inspect_network_command(),
            "inspect_runtime": backend.inspect_runtime_command(),
            "create": backend.create_command(),
            "start": backend.start_command(),
            "stop": backend.stop_command(),
            "remove": backend.remove_command(),
        }

    @staticmethod
    def _memory_bytes(value: str | None) -> int:
        if value is None:
            return 0
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", str(value), re.IGNORECASE)
        if match is None:
            raise ValueError(f"invalid Docker memory value: {value}")
        scales = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
        return int(float(match.group(1)) * scales[match.group(2).lower()])

    @classmethod
    def _runtime_matches(cls, backend: PersistentDockerBackend, raw_host_config: str) -> bool:
        try:
            host = json.loads(raw_host_config)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(host, dict):
            return False
        return (
            bool(host.get("Init")) == bool(backend.config.init)
            and int(host.get("PidsLimit") or 0) == int(backend.config.pids_limit or 0)
            and int(host.get("Memory") or 0) == cls._memory_bytes(backend.config.memory)
            and int(host.get("NanoCpus") or 0)
            == (0 if backend.config.cpus is None else int(float(backend.config.cpus) * 1_000_000_000))
            # Cairn uses the container overlay for /tmp. Containers created by
            # older Slime releases carried a 128MB tmpfs and must be recreated
            # once so provider CLIs cannot keep hitting ENOSPC.
            and "/tmp" not in dict(host.get("Tmpfs") or {})
        )

    @staticmethod
    def _create_and_start(
        plan: dict[str, list[str]],
        executor: Callable[[list[str]], CommandExecution],
        result: str,
    ) -> str:
        created = executor(plan["create"])
        if created.exit_code != 0:
            raise RuntimeError(f"创建项目 Kali Worker 失败: {created.stderr.strip()}")
        started = executor(plan["start"])
        if started.exit_code != 0:
            raise RuntimeError(f"启动项目 Kali Worker 失败: {started.stderr.strip()}")
        return result

    def ensure_started(
        self,
        project_id: str,
        executor: Callable[[list[str]], CommandExecution],
        profile: WorkerProfile = STANDARD_PROFILE,
    ) -> str:
        """Idempotently reuse, start, or create the persistent project container."""

        backend = self.backend_for(project_id, profile)
        plan = self.lifecycle_plan(project_id, profile)
        inspected = executor(plan["inspect"])
        if inspected.exit_code == 0:
            existing_image = executor(plan["inspect_image"])
            if existing_image.exit_code != 0:
                raise RuntimeError(
                    f"读取已有项目 Kali Worker 镜像失败: {existing_image.stderr.strip()}"
                )
            existing_network = executor(plan["inspect_network"])
            if existing_network.exit_code != 0:
                raise RuntimeError(
                    f"读取已有项目 Kali Worker 网络失败: {existing_network.stderr.strip()}"
                )
            image_matches = existing_image.stdout.strip() == profile.image
            if image_matches and self.verify_image_identity:
                container_image_id = executor(plan["inspect_container_image_id"])
                desired_image_id = executor(plan["inspect_image_id"])
                if container_image_id.exit_code != 0:
                    raise RuntimeError(
                        "读取已有项目 Kali Worker 实际镜像 ID 失败: "
                        f"{container_image_id.stderr.strip()}"
                    )
                if desired_image_id.exit_code != 0:
                    raise RuntimeError(
                        "解析当前 Kali Worker 镜像 ID 失败: "
                        f"{desired_image_id.stderr.strip()}"
                    )
                current_id = container_image_id.stdout.strip()
                expected_id = desired_image_id.stdout.strip()
                # Empty output is never treated as a match. A real Docker
                # inspect always returns a non-empty sha256 image ID.
                image_matches = bool(current_id and expected_id and current_id == expected_id)
            network_matches = existing_network.stdout.strip() == backend.config.network
            runtime_matches = False
            if image_matches and network_matches:
                existing_runtime = executor(plan["inspect_runtime"])
                if existing_runtime.exit_code != 0:
                    raise RuntimeError(
                        f"读取已有项目 Kali Worker 运行时配置失败: {existing_runtime.stderr.strip()}"
                    )
                runtime_matches = self._runtime_matches(backend, existing_runtime.stdout.strip())
            if (
                not image_matches
                or not network_matches
                or not runtime_matches
            ):
                # Image, network, init and cgroup limits are fixed at create
                # time. Recreate only the container; its project workspace
                # remains the same host bind mount.
                if inspected.stdout.strip().lower() == "true":
                    stopped = executor(plan["stop"])
                    if stopped.exit_code != 0:
                        raise RuntimeError(f"停止旧版项目 Kali Worker 失败: {stopped.stderr.strip()}")
                removed = executor(plan["remove"])
                if removed.exit_code != 0:
                    raise RuntimeError(f"移除旧版项目 Kali Worker 失败: {removed.stderr.strip()}")
                return self._create_and_start(plan, executor, "recreated")
            if inspected.stdout.strip().lower() == "true":
                return "reused"
            started = executor(plan["start"])
            if started.exit_code != 0:
                raise RuntimeError(f"启动已有 Kali Worker 失败: {started.stderr.strip()}")
            return "started"
        return self._create_and_start(plan, executor, "created")
