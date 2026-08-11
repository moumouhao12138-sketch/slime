from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from ..domain.branch_policy import (
    DEFAULT_GROWTH_CONVERGENCE_SELECTIONS,
    DEFAULT_GROWTH_EXPLORATION_MIN_PROBABILITY,
    DEFAULT_GROWTH_EXPLORATION_PROBABILITY,
    DEFAULT_GROWTH_RANDOM_SEED,
    DEFAULT_GROWTH_SELECTION_MODE,
    GROWTH_SELECTION_MODES,
    SmaBranchPolicy,
)
from ..domain.models import Intent, new_id
from ..server.blackboard import Blackboard
from ..workers.health import ModelEndpoint
from ..workers.native import NativeAgentConfig, NativeAgentMind, NativeBackendResolver
from .scheduler import Scheduler


TASK_MODES = frozenset({"bootstrap", "explore", "reason"})
REQUIRED_TASK_MODES = frozenset({"explore", "reason"})
WORKER_TYPE_ALIASES = {
    "claudecode": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
    "pi": "pi-cli",
    "pi-cli": "pi-cli",
}
WORKER_HEALTHCHECK_MODES = frozenset({"disabled", "startup_only", "startup_and_task"})
DEFAULT_MAX_INTENT_ATTEMPTS = 6
DEFAULT_INTENT_FAILURE_BACKOFF_SECONDS = 30.0
DEFAULT_INTENT_FAILURE_BACKOFF_MAX_SECONDS = 1800.0
DEFAULT_WORKER_HEALTHCHECK = "startup_only"
_ENV_REFERENCE = re.compile(
    r"^\$\{([A-Za-z_][A-Za-z0-9_]*(?:\|[A-Za-z_][A-Za-z0-9_]*)*)\}$"
)


@dataclass(frozen=True, slots=True)
class BootstrapTaskConfig:
    """Cairn's bounded bootstrap execution and conclude phases."""

    timeout: int = 300
    conclude_timeout: int = 90

    def __post_init__(self) -> None:
        if isinstance(self.timeout, bool) or self.timeout < 1:
            raise ValueError("tasks.bootstrap.timeout must be a positive integer")
        if isinstance(self.conclude_timeout, bool) or self.conclude_timeout < 1:
            raise ValueError("tasks.bootstrap.conclude_timeout must be a positive integer")


@dataclass(frozen=True, slots=True)
class ReasonTaskConfig:
    """Cairn's bounded Reason turn and Intent expansion limit."""

    timeout: int = 300
    max_intents: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.timeout, bool) or self.timeout < 1:
            raise ValueError("tasks.reason.timeout must be a positive integer")
        if isinstance(self.max_intents, bool) or self.max_intents < 1:
            raise ValueError("tasks.reason.max_intents must be a positive integer")


@dataclass(frozen=True, slots=True)
class ExploreTaskConfig:
    """Cairn's bounded Explore execution and conclude phases."""

    timeout: int = 300
    conclude_timeout: int = 90

    def __post_init__(self) -> None:
        if isinstance(self.timeout, bool) or self.timeout < 1:
            raise ValueError("tasks.explore.timeout must be a positive integer")
        if isinstance(self.conclude_timeout, bool) or self.conclude_timeout < 1:
            raise ValueError("tasks.explore.conclude_timeout must be a positive integer")


@dataclass(frozen=True, slots=True)
class CairnTaskConfig:
    """The explicit ``tasks`` section used by Cairn dispatch configurations."""

    bootstrap: BootstrapTaskConfig = field(default_factory=BootstrapTaskConfig)
    reason: ReasonTaskConfig = field(default_factory=ReasonTaskConfig)
    explore: ExploreTaskConfig = field(default_factory=ExploreTaskConfig)

    @classmethod
    def from_mapping(cls, value: Any) -> "CairnTaskConfig":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("dispatch config tasks must be an object")

        def section(name: str, config_type: type) -> Any:
            raw = value.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"dispatch config tasks.{name} must be an object")
            try:
                return config_type(**raw)
            except TypeError as exc:
                raise ValueError(f"dispatch config tasks.{name} has unsupported fields") from exc

        return cls(
            bootstrap=section("bootstrap", BootstrapTaskConfig),
            reason=section("reason", ReasonTaskConfig),
            explore=section("explore", ExploreTaskConfig),
        )


def load_env_file(path: str | Path, override: bool = False) -> dict[str, str]:
    """Load a simple KEY=VALUE .env file without adding a runtime dependency."""

    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _env_text(name: str) -> str:
    return os.environ.get(name, "").strip()


def _value_from_env(entry: dict[str, Any], key: str, default: str = "") -> str:
    value = str(entry.get(key, default))
    raw_sources = entry.get(f"{key}_env", "")
    sources = raw_sources if isinstance(raw_sources, (list, tuple)) else [raw_sources]
    candidates = [str(source).strip() for source in sources if str(source).strip()]
    if candidates:
        for env_name in candidates:
            env_value = _env_text(env_name)
            if env_value:
                return env_value
        raise RuntimeError(
            f"Worker {entry.get('name')} 缺少环境变量 {' 或 '.join(candidates)}"
        )
    return value


def _config_overrides_from_entry(entry: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    overrides: dict[str, Any] = dict(entry.get("config_overrides") or {})
    for raw_key, raw_source in dict(entry.get("config_overrides_from_env") or {}).items():
        key = str(raw_key).strip()
        source = str(raw_source).strip()
        value = _env_text(source)
        if not value:
            raise RuntimeError(f"Worker {entry.get('name')} 缺少环境变量 {source}")
        overrides[key] = value
    return tuple((str(key), value) for key, value in overrides.items())


def _expand_env_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    """Resolve child environment variables from the host process environment."""

    resolved: dict[str, str] = {}
    for child_name, source_names in dict(mapping or {}).items():
        child = str(child_name).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", child):
            raise ValueError(f"invalid environment variable name: {child}")
        values = source_names if isinstance(source_names, (list, tuple)) else [source_names]
        candidates = [str(value).strip() for value in values if str(value).strip()]
        for source in candidates:
            if source in os.environ:
                resolved[child] = os.environ[source]
                break
        else:
            raise RuntimeError(f"Native Worker is missing environment variable: {' or '.join(candidates)}")
    return resolved


def _expand_cairn_env(mapping: dict[str, Any] | None, *, worker_name: str) -> dict[str, str]:
    """Expand Cairn-style ``env`` values while keeping credentials out of JSON."""

    resolved: dict[str, str] = {}
    for raw_name, raw_value in dict(mapping or {}).items():
        name = str(raw_name).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Worker {worker_name} 的 env 变量名无效: {name}")
        value = str(raw_value)
        match = _ENV_REFERENCE.fullmatch(value.strip())
        if match:
            sources = match.group(1).split("|")
            for source in sources:
                if source in os.environ and os.environ[source].strip():
                    value = os.environ[source]
                    break
            else:
                raise RuntimeError(
                    f"Worker {worker_name} 缺少环境变量 {' 或 '.join(sources)}"
                )
        resolved[name] = value
    return resolved


def _explicit_model_endpoint(
    entry: dict[str, Any],
    worker_type: str,
    environment: dict[str, str],
) -> tuple[str, str, ModelEndpoint | None, dict[str, Any]]:
    """Normalize Cairn's container Worker env contract for native CLIs."""

    if "env" not in entry:
        return (
            _value_from_env(entry, "model"),
            _value_from_env(entry, "provider"),
            None,
            {},
        )

    name = str(entry.get("name", "<unknown>"))

    def required(key: str) -> str:
        value = str(environment.get(key, "")).strip()
        if not value:
            raise ValueError(f"Worker {name} 缺少容器模型配置 {key}")
        return value

    if worker_type == "codex-cli":
        model = required("CODEX_MODEL")
        endpoint = ModelEndpoint(
            base_url=required("CODEX_BASE_URL"),
            api_key=required("OPENAI_API_KEY"),
            protocol="openai-responses",
        )
        provider = str(entry.get("provider", "slime_cairn")).strip() or "slime_cairn"
        overrides = {
            "model_provider": provider,
            f"model_providers.{provider}.name": provider,
            f"model_providers.{provider}.wire_api": "responses",
            f"model_providers.{provider}.base_url": endpoint.base_url,
            f"model_providers.{provider}.env_key": "OPENAI_API_KEY",
        }
        return model, provider, endpoint, overrides

    if worker_type == "claude-code":
        model = required("ANTHROPIC_MODEL")
        endpoint = ModelEndpoint(
            base_url=required("ANTHROPIC_BASE_URL"),
            api_key=required("ANTHROPIC_AUTH_TOKEN"),
            protocol="anthropic-messages",
        )
        return model, "", endpoint, {}

    model = required("PI_MODEL")
    provider_api = required("PI_PROVIDER_API")
    protocol = (
        "anthropic-messages"
        if "anthropic" in provider_api.lower()
        else "openai-responses"
        if "responses" in provider_api.lower()
        else "openai-chat-completions"
    )
    endpoint = ModelEndpoint(
        base_url=required("PI_BASE_URL"),
        api_key=required("PI_API_KEY"),
        protocol=protocol,
    )
    environment["SLIME_PI_MODEL"] = model
    environment["SLIME_PI_BASE_URL"] = endpoint.base_url
    environment["SLIME_PI_PROVIDER_API"] = provider_api
    return model, "slime_cairn", endpoint, {}


@dataclass(slots=True)
class WorkerRuntime:
    """One selectable AI runtime, comparable to a Cairn Worker entry."""

    name: str
    mind: NativeAgentMind
    task_types: tuple[str, ...]
    max_running: int = 1
    priority: int = 0
    healthy: bool = True
    rejected_until: float = 0.0
    running: int = 0
    health_details: dict[str, Any] = field(default_factory=dict)
    execution: str = "native-agent"

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.task_types = tuple(dict.fromkeys(self.task_types))
        if not self.name:
            raise ValueError("Worker name 不能为空")
        if not self.task_types or not set(self.task_types) <= TASK_MODES:
            raise ValueError(f"Worker {self.name} 的 task_types 无效: {self.task_types}")
        if self.max_running < 1:
            raise ValueError("max_running 必须大于 0")
        if self.priority < 0:
            raise ValueError("priority 不能小于 0")
        if self.execution != "native-agent":
            raise ValueError(f"Worker {self.name} 的 execution 无效: {self.execution}")

    def check_health(self) -> dict[str, Any]:
        check = getattr(self.mind, "healthcheck", None)
        if not callable(check):
            result = {"healthy": True, "adapter": type(self.mind).__name__}
        else:
            try:
                result = dict(check() or {})
                result.setdefault("healthy", True)
            except Exception as exc:
                result = {
                    "healthy": False,
                    "adapter": type(self.mind).__name__,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
        self.healthy = bool(result.get("healthy"))
        self.health_details = result
        return result


class WorkerPool:
    """Capacity-aware selection for multiple real models or agent backends."""

    def __init__(self, workers: list[WorkerRuntime]) -> None:
        if not workers:
            raise ValueError("至少需要一个 Worker")
        names = [worker.name for worker in workers]
        if len(names) != len(set(names)):
            raise ValueError("Worker name 必须唯一")
        self._workers = list(workers)
        self._lock = threading.RLock()

    def available_task_types(self, include_reason: bool = True) -> set[str]:
        current = time.monotonic()
        with self._lock:
            modes = {
                mode
                for worker in self._workers
                if worker.healthy
                and worker.rejected_until <= current
                and worker.running < worker.max_running
                for mode in worker.task_types
            }
        if not include_reason:
            modes.discard("reason")
        return modes

    def configured_task_types(self, healthy_only: bool = False) -> set[str]:
        with self._lock:
            return {
                mode
                for worker in self._workers
                if not healthy_only or worker.healthy
                for mode in worker.task_types
            }

    def worker_names(self, task_type: str, *, healthy_only: bool = True) -> set[str]:
        """Return configured Worker names for an Intent mode."""
        with self._lock:
            return {
                worker.name
                for worker in self._workers
                if task_type in worker.task_types
                and (not healthy_only or worker.healthy)
            }

    def try_acquire(
        self,
        task_type: str,
        excluded_worker_names: set[str] | None = None,
    ) -> WorkerRuntime | None:
        current = time.monotonic()
        excluded = excluded_worker_names or set()
        with self._lock:
            candidates = [
                worker
                for worker in self._workers
                if task_type in worker.task_types
                and worker.name not in excluded
                and worker.healthy
                and worker.rejected_until <= current
                and worker.running < worker.max_running
            ]
            candidates.sort(key=lambda item: (item.priority, item.running, item.name))
            if not candidates:
                return None
            selected = candidates[0]
            selected.running += 1
            return selected

    def release(self, worker_name: str) -> None:
        with self._lock:
            worker = self.get(worker_name)
            worker.running = max(0, worker.running - 1)

    def get(self, worker_name: str) -> WorkerRuntime:
        for worker in self._workers:
            if worker.name == worker_name:
                return worker
        raise KeyError(worker_name)

    def any_mind(self) -> NativeAgentMind:
        return self._workers[0].mind

    def reject_temporarily(self, worker_name: str, seconds: float) -> None:
        with self._lock:
            self.get(worker_name).rejected_until = time.monotonic() + max(0.0, seconds)

    def cancel_active(
        self,
        worker_name: str,
        project_id: str | None = None,
        intent_id: str | None = None,
        reason: str = "cancelled",
    ) -> dict[str, Any]:
        """Cancel only the native CLI session that owns this leased task."""

        with self._lock:
            worker = self.get(worker_name)
        cancel = getattr(worker.mind, "cancel_active", None)
        if not callable(cancel):
            return {"cancelled": 0}
        try:
            return dict(
                cancel(
                    project_id=project_id,
                    intent_id=intent_id,
                    reason=reason,
                )
                or {}
            )
        except TypeError:
            # Lightweight test minds from older integrations expose only a
            # zero-argument cancellation hook. NativeAgentMind supports the
            # scoped form above, which is used in production.
            return dict(cancel() or {})

    def set_health(self, worker_name: str, healthy: bool) -> None:
        with self._lock:
            self.get(worker_name).healthy = healthy

    def healthcheck_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            workers = list(self._workers)
        return {worker.name: worker.check_health() for worker in workers}

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": worker.name,
                    "task_types": list(worker.task_types),
                    "execution": worker.execution,
                    "max_running": worker.max_running,
                    "running": worker.running,
                    "priority": worker.priority,
                    "healthy": worker.healthy,
                    "health_details": dict(worker.health_details),
                    "rejected_until": worker.rejected_until,
                }
                for worker in self._workers
            ]


@dataclass(slots=True)
class DispatcherConfig:
    max_workers: int = 8
    max_running_projects: int = 3
    max_project_workers: int = 4
    # Cairn's server treats a worker lease as a short-lived heartbeat claim.
    # The native Blackboard stores an explicit expiration timestamp, so keep
    # the same 15-second lease locally rather than allowing a stuck CLI turn
    # to look active for many minutes.
    lease_seconds: float = 15.0
    reason_lease_seconds: float | None = None
    heartbeat_interval: float = 3.0
    interval: float = 3.0
    # Compatibility for older Slime configs and fast unit tests. New Cairn
    # configs use runtime.interval.
    poll_interval: float | None = None
    reason_batch_size: int = 2
    reason_debounce_seconds: float = 1.0
    # Cairn releases a failed Reason lease and briefly cools the Worker rather
    # than parking the whole project behind a long exponential delay.
    reason_failure_cooldown_seconds: float = 5.0
    reason_failure_backoff_max_seconds: float = 5.0
    # Repeated identical model failures indicate a bad provider response or
    # an oversized task. Pause the project after a short streak instead of
    # spending tokens on the same request every polling cycle.
    reason_failure_pause_threshold: int = 3
    reason_failure_pause_seconds: float = 60.0
    reason_failure_pause_max_seconds: float = 300.0
    worker_rejected_cooldown_seconds: float = 5.0
    # Pause one rotation after every capable Worker fails an Intent. This
    # prevents provider failures from becoming a zero-backoff hot loop.
    intent_worker_cycle_cooldown_seconds: float = 30.0
    growth_selection_mode: str = DEFAULT_GROWTH_SELECTION_MODE
    growth_exploration_probability: float = DEFAULT_GROWTH_EXPLORATION_PROBABILITY
    growth_exploration_min_probability: float = DEFAULT_GROWTH_EXPLORATION_MIN_PROBABILITY
    growth_convergence_selections: int = DEFAULT_GROWTH_CONVERGENCE_SELECTIONS
    growth_random_seed: str = DEFAULT_GROWTH_RANDOM_SEED
    # Worker/provider failures are durable and bounded by default. Operators
    # can still select Cairn's open queue explicitly with max_intent_attempts=0.
    max_intent_attempts: int = DEFAULT_MAX_INTENT_ATTEMPTS
    intent_failure_backoff_seconds: float = DEFAULT_INTENT_FAILURE_BACKOFF_SECONDS
    intent_failure_backoff_max_seconds: float = DEFAULT_INTENT_FAILURE_BACKOFF_MAX_SECONDS
    state_heartbeat_interval: float = 2.0
    execution: str = "container"
    prompt_group: str = "default"
    worker_healthcheck: str = DEFAULT_WORKER_HEALTHCHECK
    healthcheck_timeout: float = 15.0
    tasks: CairnTaskConfig = field(default_factory=CairnTaskConfig)

    @classmethod
    def from_env(cls) -> "DispatcherConfig":
        legacy_interval = os.environ.get("SLIME_DISPATCH_INTERVAL", "").strip()
        interval = float(os.environ.get("SLIME_RUNTIME_INTERVAL", legacy_interval or "3"))
        return cls(
            max_workers=int(os.environ.get("SLIME_MAX_WORKERS", "8")),
            max_running_projects=int(os.environ.get("SLIME_MAX_RUNNING_PROJECTS", "3")),
            max_project_workers=int(os.environ.get("SLIME_MAX_PROJECT_WORKERS", "4")),
            lease_seconds=float(os.environ.get("SLIME_LEASE_SECONDS", "15")),
            reason_lease_seconds=float(
                os.environ.get(
                    "SLIME_REASON_LEASE_SECONDS",
                    os.environ.get("SLIME_LEASE_SECONDS", "15"),
                )
            ),
            heartbeat_interval=float(os.environ.get("SLIME_HEARTBEAT_INTERVAL", str(interval))),
            interval=interval,
            poll_interval=float(legacy_interval) if legacy_interval else None,
            reason_batch_size=int(os.environ.get("SLIME_REASON_BATCH_SIZE", "2")),
            reason_debounce_seconds=float(os.environ.get("SLIME_REASON_DEBOUNCE", "1")),
            reason_failure_cooldown_seconds=float(os.environ.get("SLIME_REASON_FAILURE_COOLDOWN", "5")),
            reason_failure_backoff_max_seconds=float(os.environ.get("SLIME_REASON_FAILURE_BACKOFF_MAX", "5")),
            reason_failure_pause_threshold=int(
                os.environ.get("SLIME_REASON_FAILURE_PAUSE_THRESHOLD", "3")
            ),
            reason_failure_pause_seconds=float(
                os.environ.get("SLIME_REASON_FAILURE_PAUSE_SECONDS", "60")
            ),
            reason_failure_pause_max_seconds=float(
                os.environ.get("SLIME_REASON_FAILURE_PAUSE_MAX_SECONDS", "300")
            ),
            worker_rejected_cooldown_seconds=float(os.environ.get("SLIME_WORKER_REJECTED_COOLDOWN", "5")),
            intent_worker_cycle_cooldown_seconds=float(
                os.environ.get("SLIME_INTENT_WORKER_CYCLE_COOLDOWN", "30")
            ),
            growth_selection_mode=os.environ.get(
                "SLIME_GROWTH_SELECTION_MODE", DEFAULT_GROWTH_SELECTION_MODE
            ),
            growth_exploration_probability=float(
                os.environ.get(
                    "SLIME_GROWTH_EXPLORATION_PROBABILITY",
                    str(DEFAULT_GROWTH_EXPLORATION_PROBABILITY),
                )
            ),
            growth_exploration_min_probability=float(
                os.environ.get(
                    "SLIME_GROWTH_EXPLORATION_MIN_PROBABILITY",
                    str(DEFAULT_GROWTH_EXPLORATION_MIN_PROBABILITY),
                )
            ),
            growth_convergence_selections=int(
                os.environ.get(
                    "SLIME_GROWTH_CONVERGENCE_SELECTIONS",
                    str(DEFAULT_GROWTH_CONVERGENCE_SELECTIONS),
                )
            ),
            growth_random_seed=os.environ.get(
                "SLIME_GROWTH_RANDOM_SEED", DEFAULT_GROWTH_RANDOM_SEED
            ),
            max_intent_attempts=int(
                os.environ.get("SLIME_MAX_INTENT_ATTEMPTS", str(DEFAULT_MAX_INTENT_ATTEMPTS))
            ),
            intent_failure_backoff_seconds=float(
                os.environ.get(
                    "SLIME_INTENT_FAILURE_BACKOFF_SECONDS",
                    str(DEFAULT_INTENT_FAILURE_BACKOFF_SECONDS),
                )
            ),
            intent_failure_backoff_max_seconds=float(
                os.environ.get(
                    "SLIME_INTENT_FAILURE_BACKOFF_MAX_SECONDS",
                    str(DEFAULT_INTENT_FAILURE_BACKOFF_MAX_SECONDS),
                )
            ),
            state_heartbeat_interval=float(os.environ.get("SLIME_STATE_HEARTBEAT_INTERVAL", "2")),
            execution=os.environ.get("SLIME_RUNTIME_EXECUTION", "container"),
            prompt_group=os.environ.get("SLIME_PROMPT_GROUP", "default"),
            worker_healthcheck=os.environ.get(
                "SLIME_WORKER_HEALTHCHECK", DEFAULT_WORKER_HEALTHCHECK
            ),
            healthcheck_timeout=float(os.environ.get("SLIME_HEALTHCHECK_TIMEOUT", "15")),
            tasks=CairnTaskConfig(
                bootstrap=BootstrapTaskConfig(
                    timeout=int(os.environ.get("SLIME_BOOTSTRAP_TIMEOUT", "300")),
                    conclude_timeout=int(os.environ.get("SLIME_BOOTSTRAP_CONCLUDE_TIMEOUT", "90")),
                ),
                reason=ReasonTaskConfig(
                    timeout=int(os.environ.get("SLIME_REASON_TIMEOUT", "300")),
                    max_intents=int(os.environ.get("SLIME_REASON_MAX_INTENTS", "2")),
                ),
                explore=ExploreTaskConfig(
                    timeout=int(os.environ.get("SLIME_EXPLORE_TIMEOUT", "300")),
                    conclude_timeout=int(os.environ.get("SLIME_EXPLORE_CONCLUDE_TIMEOUT", "90")),
                ),
            ),
        )

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        if self.max_running_projects < 1:
            raise ValueError("max_running_projects 必须大于 0")
        if self.max_project_workers < 1:
            raise ValueError("max_project_workers 必须大于 0")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if self.reason_lease_seconds is None:
            self.reason_lease_seconds = self.lease_seconds
        if self.reason_lease_seconds <= 0:
            raise ValueError("reason_lease_seconds 必须大于 0")
        if self.heartbeat_interval <= 0 or self.heartbeat_interval >= self.lease_seconds:
            raise ValueError("heartbeat_interval 必须大于 0 且小于 lease_seconds")
        if self.heartbeat_interval >= self.reason_lease_seconds:
            raise ValueError("heartbeat_interval 必须小于 reason_lease_seconds")
        if self.interval <= 0:
            raise ValueError("runtime.interval 必须大于 0")
        if self.poll_interval is not None and self.poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        if self.reason_batch_size < 1:
            raise ValueError("reason_batch_size 必须大于 0")
        if self.reason_debounce_seconds < 0:
            raise ValueError("reason_debounce_seconds 不能小于 0")
        if self.reason_failure_cooldown_seconds < 0:
            raise ValueError("reason_failure_cooldown_seconds 不能小于 0")
        if self.reason_failure_backoff_max_seconds < self.reason_failure_cooldown_seconds:
            raise ValueError("reason_failure_backoff_max_seconds 必须大于等于 reason_failure_cooldown_seconds")
        if self.reason_failure_pause_threshold < 1:
            raise ValueError("reason_failure_pause_threshold 必须大于 0")
        if self.reason_failure_pause_seconds < 0:
            raise ValueError("reason_failure_pause_seconds 不能小于 0")
        if self.reason_failure_pause_max_seconds < self.reason_failure_pause_seconds:
            raise ValueError(
                "reason_failure_pause_max_seconds 必须大于等于 reason_failure_pause_seconds"
            )
        if self.worker_rejected_cooldown_seconds < 0:
            raise ValueError("worker_rejected_cooldown_seconds 不能小于 0")
        if self.intent_worker_cycle_cooldown_seconds < 0:
            raise ValueError("intent_worker_cycle_cooldown_seconds 不能小于 0")
        if not isinstance(self.growth_selection_mode, str):
            raise ValueError("growth_selection_mode 必须是字符串")
        if self.growth_selection_mode not in GROWTH_SELECTION_MODES:
            raise ValueError(
                "growth_selection_mode 必须是: "
                + ", ".join(sorted(GROWTH_SELECTION_MODES))
            )
        if not (
            0
            <= self.growth_exploration_min_probability
            <= self.growth_exploration_probability
            <= 1
        ):
            raise ValueError(
                "growth exploration probabilities 必须满足 0 <= min <= initial <= 1"
            )
        if self.growth_convergence_selections < 1:
            raise ValueError("growth_convergence_selections 必须大于 0")
        if not isinstance(self.growth_random_seed, str):
            raise ValueError("growth_random_seed 必须是字符串")
        if not self.growth_random_seed.strip():
            raise ValueError("growth_random_seed 不能为空")
        if self.max_intent_attempts < 0:
            raise ValueError("max_intent_attempts 不能小于 0")
        if self.intent_failure_backoff_seconds < 0:
            raise ValueError("intent_failure_backoff_seconds 不能小于 0")
        if self.intent_failure_backoff_max_seconds < self.intent_failure_backoff_seconds:
            raise ValueError(
                "intent_failure_backoff_max_seconds 必须大于等于 intent_failure_backoff_seconds"
            )
        if self.state_heartbeat_interval <= 0:
            raise ValueError("state_heartbeat_interval 必须大于 0")
        if self.execution != "container":
            raise ValueError("Cairn native runtime 仅支持 execution=container")
        if self.worker_healthcheck not in WORKER_HEALTHCHECK_MODES:
            raise ValueError(
                "worker_healthcheck 必须是: "
                + ", ".join(sorted(WORKER_HEALTHCHECK_MODES))
            )
        if self.healthcheck_timeout <= 0:
            raise ValueError("healthcheck_timeout 必须大于 0")
        if not isinstance(self.tasks, CairnTaskConfig):
            raise ValueError("tasks 必须是 CairnTaskConfig")

    @property
    def cycle_interval(self) -> float:
        """Use the Cairn runtime interval, while keeping old test configs valid."""

        return self.poll_interval if self.poll_interval is not None else self.interval


def load_dispatch_config(
    path: str | Path,
    native_backend_resolver: NativeBackendResolver | None = None,
) -> tuple[DispatcherConfig, WorkerPool]:
    """Load Cairn-style project-container Native Agent workers only."""

    config_path = Path(path).resolve()
    load_env_file(config_path.parent / ".env")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("dispatch config 顶层必须是 JSON object")
    runtime_data = dict(data.get("runtime") or {})
    # Cairn has one runtime interval used for scheduling and heartbeat.  The
    # BlackBoard lease uses a separate field internally, but default its
    # renewal cadence from that same explicit setting.
    if "heartbeat_interval" not in runtime_data and "interval" in runtime_data:
        runtime_data["heartbeat_interval"] = runtime_data["interval"]
    tasks = CairnTaskConfig.from_mapping(data.get("tasks"))
    config = DispatcherConfig(**runtime_data, tasks=tasks)
    common_env = _expand_cairn_env(
        dict(data.get("common_env") or {}),
        worker_name="common_env",
    )
    workers: list[WorkerRuntime] = []
    for entry in data.get("workers") or []:
        if not isinstance(entry, dict):
            raise ValueError("Worker 配置必须是 JSON object")
        if entry.get("enabled") is False:
            continue
        if "route_tags" in entry:
            raise ValueError("Worker 配置不支持 route_tags")
        declared_type = str(entry.get("type", "codex-cli")).strip().lower()
        try:
            worker_type = WORKER_TYPE_ALIASES[declared_type]
        except KeyError as exc:
            raise ValueError("native-agent supports claudecode, codex, or pi") from exc
        execution = str(entry.get("execution", "native-agent")).strip().lower()
        environment = {
            str(key): str(value)
            for key, value in dict(entry.get("environment") or {}).items()
        }
        environment = {
            **common_env,
            **environment,
            **_expand_cairn_env(dict(entry.get("env") or {}), worker_name=str(entry.get("name", "<unknown>"))),
        }
        environment.update(_expand_env_mapping(entry.get("env_from")))

        if execution != "native-agent":
            raise ValueError(
                f"Worker {entry.get('name')} must use execution=native-agent; "
                "Cairn runtime only accepts direct container CLI workers"
            )
        if native_backend_resolver is None:
            raise ValueError(
                f"Worker {entry.get('name')} requires a Docker project runtime"
            )
        default_binary = {
            "claude-code": "claude",
            "codex-cli": "codex",
            "pi-cli": "pi",
        }[worker_type]
        model, provider, endpoint, endpoint_overrides = _explicit_model_endpoint(
            entry,
            worker_type,
            environment,
        )
        config_overrides = dict(_config_overrides_from_entry(entry))
        config_overrides.update(endpoint_overrides)
        legacy_timeout = entry.get("timeout")
        if legacy_timeout is not None and data.get("tasks") is None:
            # v0.26 accepted one Worker timeout. Preserve that behavior for
            # old user configs, while new configs use the Cairn tasks section.
            legacy_timeout_value = int(legacy_timeout)
            bootstrap_timeout = legacy_timeout_value
            bootstrap_conclude_timeout = legacy_timeout_value
            reason_timeout = legacy_timeout_value
            explore_timeout = legacy_timeout_value
            explore_conclude_timeout = legacy_timeout_value
        else:
            bootstrap_timeout = config.tasks.bootstrap.timeout
            bootstrap_conclude_timeout = config.tasks.bootstrap.conclude_timeout
            reason_timeout = config.tasks.reason.timeout
            explore_timeout = config.tasks.explore.timeout
            explore_conclude_timeout = config.tasks.explore.conclude_timeout

        mind = NativeAgentMind(
            NativeAgentConfig(
                worker_name=str(entry["name"]),
                adapter=worker_type,
                binary=str(entry.get("binary", default_binary)),
                model=model,
                provider=provider,
                thinking=_value_from_env(entry, "thinking"),
                prompt_group=config.prompt_group,
                timeout=int(legacy_timeout) if legacy_timeout is not None else explore_timeout,
                bootstrap_timeout=bootstrap_timeout,
                bootstrap_conclude_timeout=bootstrap_conclude_timeout,
                reason_timeout=reason_timeout,
                reason_max_intents=config.tasks.reason.max_intents,
                explore_timeout=explore_timeout,
                explore_conclude_timeout=explore_conclude_timeout,
                environment=environment,
                config_overrides=tuple(config_overrides.items()),
                max_report_items=int(entry.get("max_report_items", 20)),
                max_transcript_chars=int(entry.get("max_transcript_chars", 2_000_000)),
                model_endpoint=endpoint,
                model_healthcheck_mode=config.worker_healthcheck,
                container_preflight=bool(entry.get("container_preflight", False)),
                healthcheck_timeout=config.healthcheck_timeout,
            ),
            native_backend_resolver,
        )
        workers.append(
            WorkerRuntime(
                name=str(entry["name"]),
                mind=mind,
                task_types=tuple(str(item) for item in entry["task_types"]),
                max_running=int(entry.get("max_running", 1)),
                priority=int(entry.get("priority", 0)),
                execution=execution,
            )
        )
    pool = WorkerPool(workers)
    missing = REQUIRED_TASK_MODES - {
        task_type
        for worker in workers
        for task_type in worker.task_types
    }
    if missing:
        raise ValueError(f"dispatch config 缺少任务类型 Worker: {sorted(missing)}")
    return config, pool


class GlobalCapacity:
    """One concurrency budget shared by every project Dispatcher in a service."""

    def __init__(self, max_running: int) -> None:
        if max_running < 1:
            raise ValueError("global max_running 必须大于 0")
        self.max_running = max_running
        self.running = 0
        self.max_observed = 0
        self.by_project: dict[str, int] = {}
        self.by_kind: dict[str, int] = {}
        self._lock = threading.RLock()

    def try_acquire(self, project_id: str, kind: str) -> bool:
        with self._lock:
            if self.running >= self.max_running:
                return False
            self.running += 1
            self.max_observed = max(self.max_observed, self.running)
            self.by_project[project_id] = self.by_project.get(project_id, 0) + 1
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
            return True

    def release(self, project_id: str, kind: str) -> None:
        with self._lock:
            if self.running <= 0 or self.by_project.get(project_id, 0) <= 0:
                raise RuntimeError("GlobalCapacity release 与 acquire 不匹配")
            self.running -= 1
            self.by_project[project_id] -= 1
            self.by_kind[kind] = max(0, self.by_kind.get(kind, 0) - 1)

    def available(self) -> bool:
        with self._lock:
            return self.running < self.max_running

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_running": self.max_running,
                "running": self.running,
                "max_observed": self.max_observed,
                "by_project": dict(self.by_project),
                "by_kind": dict(self.by_kind),
            }


@dataclass(slots=True)
class RunningIntent:
    intent_id: str
    owner_token: str
    worker_name: str
    mode: str
    started_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class RunningReason:
    owner_token: str
    worker_name: str
    signals: int
    worker_progress: bool
    global_audit: bool
    intent_id: str = ""
    started_at: float = field(default_factory=time.time)


class AsyncDispatcher:
    """Run many temporary pseudopods concurrently and aggregate Reason work."""

    def __init__(
        self,
        board: Blackboard,
        project_id: str,
        scheduler: Scheduler,
        worker_pool: WorkerPool,
        config: DispatcherConfig | None = None,
        global_capacity: GlobalCapacity | None = None,
    ) -> None:
        self.board = board
        self.project_id = project_id
        self.scheduler = scheduler
        self.worker_pool = worker_pool
        self.config = config or DispatcherConfig()
        self.global_capacity = global_capacity or GlobalCapacity(self.config.max_workers)
        self.id = new_id("dispatcher")
        self._tasks: dict[asyncio.Task, RunningIntent] = {}
        self._reason_task: asyncio.Task | None = None
        self._reason_record: RunningReason | None = None
        self._pending_reason_signals = 0
        self._pending_reason_progress = False
        self._last_reason_signal_at = 0.0
        self._observed_hint_ids: set[str] = set()
        self._stop_event = asyncio.Event()
        self._global_audit_done = False
        self._initialized = False
        self._state = "created"
        self._last_state_heartbeat = 0.0
        self._last_progress_log = 0.0
        self._reason_failures = 0
        self._reason_cooldown_until = 0.0
        self._reason_paused_until = 0.0
        self._reason_failure_signature = ""
        self._last_cooldown_log = 0.0
        self._failed_workers_by_intent: dict[str, set[str]] = {}
        self._intent_worker_cycle_until: dict[str, float] = {}
        self._failed_reason_workers: set[str] = set()
        self._growth_policy = SmaBranchPolicy(
            mode=self.config.growth_selection_mode,
            exploration_probability=self.config.growth_exploration_probability,
            exploration_min_probability=self.config.growth_exploration_min_probability,
            convergence_selections=self.config.growth_convergence_selections,
            random_seed=self.config.growth_random_seed,
        )
        self._growth_selection_count = 0
        self.history: list[dict] = []
        self.reason_history: list[dict] = []
        self.errors: list[dict] = []
        self.recovery = {"recovered": [], "failed": []}

    @staticmethod
    def _intent_mode(intent: Intent) -> str:
        return "bootstrap" if intent.kind == "bootstrap" else "explore"

    def _bootstrap_worker_configured(self) -> bool:
        return "bootstrap" in self.worker_pool.configured_task_types()

    def _bootstrap_path_active(self, project) -> bool:
        return bool(project.scope.get("bootstrap_enabled", True)) and self._bootstrap_worker_configured()

    @staticmethod
    def _bootstrap_archive_reason(project, bootstrap_worker_configured: bool) -> str:
        if not bool(project.scope.get("bootstrap_enabled", True)):
            return "bootstrap_disabled"
        if not bootstrap_worker_configured:
            return "bootstrap_worker_unavailable"
        return "bootstrap_not_selected"

    def _active_count(self) -> int:
        return len(self._tasks) + int(self._reason_task is not None)

    def _acquire_intent_worker(self, mode: str, intent_id: str) -> WorkerRuntime | None:
        """Prefer an untried capable Worker before repeating a failed attempt."""

        attempted = self._failed_workers_by_intent.get(intent_id, set())
        capable = self.worker_pool.worker_names(mode)
        # Do not immediately reset the attempted set after all Workers fail.
        # This is the guard against a zero-backoff provider-error hot loop.
        if capable and capable <= attempted:
            until = self._intent_worker_cycle_until.get(intent_id, 0.0)
            if until > time.monotonic():
                return None
            self._intent_worker_cycle_until.pop(intent_id, None)
            attempted.clear()
        worker = self.worker_pool.try_acquire(mode, attempted)
        if worker is None and attempted:
            worker = self.worker_pool.try_acquire(mode)
        return worker

    def _acquire_reason_worker(self) -> WorkerRuntime | None:
        """Try each healthy Reason Worker once before starting a new cycle."""

        capable = self.worker_pool.worker_names("reason")
        if capable and capable <= self._failed_reason_workers:
            self._failed_reason_workers.clear()
        return self.worker_pool.try_acquire("reason", self._failed_reason_workers)

    @staticmethod
    def _intent_failure_category(message: str) -> tuple[str, float]:
        """Return a stable category and minimum cooldown for common provider failures."""

        text = str(message).lower()
        # Native CLIs may wrap an upstream safety decision in a generic
        # non-zero exit and a reconnect loop. Keep that signal distinct from
        # Docker/runtime failures so the UI and retry policy explain what
        # actually happened.
        if (
            "flagged for possible cybersecurity risk" in text
            or "flagged for cybersecurity risk" in text
            or ("cybersecurity risk" in text and "flagged" in text)
            or ("safety policy" in text and "refus" in text)
        ):
            return "model_policy_filter", 300.0
        if (
            "stream disconnected before completion" in text
            or "upstream request failed" in text
            or ("reconnecting..." in text and "upstream" in text)
        ):
            return "provider_stream_disconnect", 180.0
        if "no assistant event" in text or "没有 assistant 结果" in text:
            return "native_protocol_no_assistant", 900.0
        if "winerror 206" in text or "path too long" in text or "文件名或扩展名太长" in text:
            return "workspace_path_limit", 900.0
        if "cannot fork" in text or "resource temporarily unavailable" in text:
            return "container_resource_exhausted", 300.0
        if any(
            marker in text
            for marker in (
                "authentication",
                "invalid api key",
                "401 unauthorized",
                "403 forbidden",
                "insufficient quota",
                "payment required",
                "预扣费",
                "剩余额度",
            )
        ):
            return "provider_access_denied", 3600.0
        if "429" in text or "rate limit" in text or "too many requests" in text:
            return "provider_rate_limited", 300.0
        if (
            "503" in text
            or "service temporarily unavailable" in text
            or "no available accounts" in text
        ):
            return "provider_unavailable", 120.0
        if "timeout" in text or "timed out" in text:
            return "worker_timeout", 90.0
        return "worker_error", 0.0

    @classmethod
    def _annotate_intent_failure(cls, message: str) -> str:
        """Make actionable provider/runtime categories visible on the Intent."""

        category, _ = cls._intent_failure_category(message)
        if category == "worker_error" or str(message).lstrip().startswith(f"[{category}]"):
            return str(message)[:2000]
        return f"[{category}] {str(message)}"[:2000]

    def _record_intent_worker_failure(
        self,
        intent_id: str,
        mode: str,
        worker_name: str,
        message: str,
    ) -> tuple[str, float] | None:
        attempted = self._failed_workers_by_intent.setdefault(intent_id, set())
        attempted.add(worker_name)
        capable = self.worker_pool.worker_names(mode)
        if not capable or not capable <= attempted:
            return None
        category, minimum_cooldown = self._intent_failure_category(message)
        cooldown = max(
            float(self.config.intent_worker_cycle_cooldown_seconds),
            minimum_cooldown,
        )
        self._intent_worker_cycle_until[intent_id] = time.monotonic() + cooldown
        self.board.add_event(
            self.project_id,
            "dispatcher.intent_worker_cycle_cooldown",
            {
                "dispatcher_id": self.id,
                "intent_id": intent_id,
                "mode": mode,
                "failed_workers": sorted(attempted),
                "failure_category": category,
                "retry_after_seconds": cooldown,
            },
        )
        return category, cooldown

    def _reason_cooldown_remaining(self) -> float:
        durable_remaining = max(0.0, self._reason_paused_until - time.time())
        runtime_remaining = max(0.0, self._reason_cooldown_until - time.monotonic())
        return max(durable_remaining, runtime_remaining)

    @staticmethod
    def _stable_reason_error_signature(message: str) -> str:
        """Remove per-invocation IDs before comparing repeated failures."""

        signature = re.sub(
            r'"(thread_id|session_id|uuid)"\s*:\s*"[^"]+"',
            r'"\1":"<id>"',
            str(message).strip(),
            flags=re.IGNORECASE,
        )
        signature = re.sub(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            "<id>",
            signature,
            flags=re.IGNORECASE,
        )
        return signature[:500]

    def _record_reason_failure_cooldown(self, worker_name: str, message: str) -> float:
        signature = self._stable_reason_error_signature(message)
        same_error = bool(self._reason_failure_signature) and signature == self._reason_failure_signature
        self._reason_failure_signature = signature
        self._reason_failures = self._reason_failures + 1 if same_error else 1
        base = float(self.config.reason_failure_cooldown_seconds)
        if base <= 0:
            retry_after = 0.0
        else:
            retry_after = min(
                float(self.config.reason_failure_backoff_max_seconds),
                base * (2 ** max(0, self._reason_failures - 1)),
            )
        if self._reason_failures >= self.config.reason_failure_pause_threshold:
            pause_level = self._reason_failures - self.config.reason_failure_pause_threshold
            retry_after = max(
                retry_after,
                min(
                    float(self.config.reason_failure_pause_max_seconds),
                    float(self.config.reason_failure_pause_seconds) * (2 ** pause_level),
                ),
            )
        self._reason_cooldown_until = time.monotonic() + retry_after
        self._reason_paused_until = time.time() + retry_after
        self.board.record_reason_failure(
            self.project_id,
            message,
            self._reason_paused_until,
            same_error=same_error,
        )
        self.worker_pool.reject_temporarily(worker_name, retry_after)
        self.board.add_event(
            self.project_id,
            "dispatcher.reason_cooldown",
            {
                "dispatcher_id": self.id,
                "worker_name": worker_name,
                "failures": self._reason_failures,
                "retry_after_seconds": retry_after,
                "paused": self._reason_failures >= self.config.reason_failure_pause_threshold,
                "error": message[:1000],
            },
        )
        return retry_after

    def _queue_hint_reason_signal(self, hints, source: str) -> None:
        """Turn newly persisted human judgment into a durable Reason wake-up."""

        if not hints:
            return
        # Human guidance is an explicit recovery signal: discard a persisted
        # model-error pause so Reason can reconsider the updated graph.
        self._reason_failures = 0
        self._reason_cooldown_until = 0.0
        self._reason_paused_until = 0.0
        self._reason_failure_signature = ""
        self.board.clear_reason_failure(self.project_id)
        hint_ids = [str(hint.id) for hint in hints]
        self._pending_reason_signals += len(hint_ids)
        self._last_reason_signal_at = time.monotonic()
        self.board.add_event(
            self.project_id,
            "dispatcher.hint_signal",
            {
                "dispatcher_id": self.id,
                "source": source,
                "hint_ids": hint_ids,
                "pending_reason_signals": self._pending_reason_signals,
            },
        )

    async def _observe_hint_signals(self) -> int:
        """Poll the shared Blackboard so API writes wake this process too."""

        hints = await asyncio.to_thread(self.board.list_hints, self.project_id)
        newly_observed = [hint for hint in hints if hint.id not in self._observed_hint_ids]
        self._observed_hint_ids.update(hint.id for hint in hints)
        if newly_observed:
            self._queue_hint_reason_signal(newly_observed, "blackboard_poll")
        return len(newly_observed)

    async def _heartbeat(self, intent_id: str, owner_token: str, work: asyncio.Task, worker_name: str) -> None:
        while not work.done():
            await asyncio.sleep(self.config.heartbeat_interval)
            if work.done():
                return
            renewed = await asyncio.to_thread(
                self.board.renew_intent_lease,
                self.project_id,
                intent_id,
                owner_token,
                self.config.lease_seconds,
            )
            if not renewed:
                self.errors.append(
                    {
                        "kind": "lease_lost",
                        "intent_id": intent_id,
                        "owner_token": owner_token,
                    }
                )
                await asyncio.to_thread(
                    self.worker_pool.cancel_active,
                    worker_name,
                    self.project_id,
                    intent_id,
                    "intent_lease_lost",
                )
                work.cancel()
                return

    async def _reason_heartbeat(self, record: RunningReason, work: asyncio.Task) -> None:
        """Maintain the durable project-level Reason lease while its CLI task runs."""

        while not work.done():
            await asyncio.sleep(self.config.heartbeat_interval)
            if work.done():
                return
            renewed = await asyncio.to_thread(
                self.board.renew_reason_lease,
                self.project_id,
                record.owner_token,
                self.config.reason_lease_seconds,
            )
            if renewed:
                continue
            self.errors.append(
                {
                    "kind": "reason_lease_lost",
                    "owner_token": record.owner_token,
                    "worker_name": record.worker_name,
                }
            )
            await asyncio.to_thread(
                self.worker_pool.cancel_active,
                record.worker_name,
                self.project_id,
                record.intent_id,
                "reason_lease_lost",
            )
            work.cancel()
            return

    async def _execute_intent(
        self,
        intent: Intent,
        owner_token: str,
        worker: WorkerRuntime,
    ) -> dict:
        work = asyncio.create_task(
            asyncio.to_thread(
                self.scheduler.process_claimed_intent,
                intent,
                worker.mind,
                worker.name,
                owner_token,
                False,
                True,
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat(intent.id, owner_token, work, worker.name))
        try:
            return await work
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _run_reason(self, record: RunningReason, worker: WorkerRuntime) -> int:
        work = asyncio.create_task(
            asyncio.to_thread(
                self.scheduler.reason,
                record.global_audit,
                record.worker_progress,
                worker.mind,
                worker.name,
                record.owner_token,
                record.intent_id,
            )
        )
        heartbeat = asyncio.create_task(self._reason_heartbeat(record, work))
        try:
            return await work
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _dispatch_available(self, max_dispatch: int | None = None) -> int:
        if self.board.get_project(self.project_id).status != "running":
            return 0
        dispatched = 0
        while self._active_count() < self.config.max_project_workers:
            if max_dispatch is not None and dispatched >= max_dispatch:
                break
            if not self.global_capacity.try_acquire(self.project_id, "intent"):
                break
            candidates = await asyncio.to_thread(
                self.board.list_runnable_intents,
                self.project_id,
            )
            population = await asyncio.to_thread(
                self.board.list_intents,
                self.project_id,
            )
            selection = self._growth_policy.select(
                candidates,
                project_id=self.project_id,
                selection_index=self._growth_selection_count,
                all_intents=population,
            )
            selected: tuple[Intent, str, WorkerRuntime] | None = None
            for candidate in selection.ordered:
                mode = self._intent_mode(candidate)
                worker = self._acquire_intent_worker(mode, candidate.id)
                if worker is None:
                    continue
                owner_token = new_id("lease")
                try:
                    intent = await asyncio.to_thread(
                        self.board.claim_intent,
                        self.project_id,
                        candidate.id,
                        owner_token,
                        self.config.lease_seconds,
                        self.config.max_intent_attempts,
                        self.config.intent_failure_backoff_seconds,
                        self.config.intent_failure_backoff_max_seconds,
                    )
                except Exception:
                    self.worker_pool.release(worker.name)
                    self.global_capacity.release(self.project_id, "intent")
                    raise
                if intent is None:
                    self.worker_pool.release(worker.name)
                    continue
                selected = (intent, owner_token, worker)
                break
            if selected is None:
                self.global_capacity.release(self.project_id, "intent")
                break
            intent, owner_token, worker = selected
            mode = self._intent_mode(intent)
            growth_selection = dict(selection.details)
            policy_selected_intent_id = growth_selection.get("selected_intent_id")
            growth_selection["policy_selected_intent_id"] = policy_selected_intent_id
            growth_selection["selected_intent_id"] = intent.id
            growth_selection["fallback"] = intent.id != policy_selected_intent_id
            self._growth_selection_count += 1
            task = asyncio.create_task(self._execute_intent(intent, owner_token, worker))
            self._tasks[task] = RunningIntent(intent.id, owner_token, worker.name, mode)
            dispatched += 1
            print(
                f"[dispatcher] project={self.project_id} start {mode} "
                f"intent={intent.id} worker={worker.name}",
                flush=True,
            )
            self.board.add_event(
                self.project_id,
                "growth.branch_selected",
                {
                    "dispatcher_id": self.id,
                    "intent_id": intent.id,
                    "worker_name": worker.name,
                    "mode": mode,
                    **growth_selection,
                },
            )
            self.board.add_event(
                self.project_id,
                "dispatcher.intent_started",
                {
                    "dispatcher_id": self.id,
                    "intent_id": intent.id,
                    "owner_token": owner_token,
                    "worker_name": worker.name,
                    "mode": mode,
                    "growth_selection": growth_selection,
                },
            )
        return dispatched

    async def _reap_intents(self) -> int:
        completed = 0
        for task, record in list(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(task, None)
            self.worker_pool.release(record.worker_name)
            self.global_capacity.release(self.project_id, "intent")
            completed += 1
            try:
                result = task.result()
            except asyncio.CancelledError:
                # Cancellation is an expected terminal state during a project
                # stop.  Do not let it escape the reaper and terminate the
                # Dispatcher before its remaining leases and slots are freed.
                message = "worker task cancelled"
                status = await asyncio.to_thread(
                    self.board.release_intent,
                    self.project_id,
                    record.intent_id,
                    record.owner_token,
                    message,
                    self.config.max_intent_attempts,
                    self.config.intent_failure_backoff_seconds,
                    self.config.intent_failure_backoff_max_seconds,
                    penalize=False,
                )
                self.errors.append(
                    {
                        "kind": "worker_cancelled",
                        "intent_id": record.intent_id,
                        "worker_name": record.worker_name,
                        "status": status,
                        "error": message,
                    }
                )
                continue
            except Exception as exc:
                message = self._annotate_intent_failure(f"{type(exc).__name__}: {exc}")
                await asyncio.to_thread(
                    self.board.annotate_latest_worker_run_error,
                    self.project_id,
                    record.intent_id,
                    message,
                )
                failure_cycle = self._record_intent_worker_failure(
                    record.intent_id,
                    record.mode,
                    record.worker_name,
                    message,
                )
                status = await asyncio.to_thread(
                    self.board.release_intent,
                    self.project_id,
                    record.intent_id,
                    record.owner_token,
                    message,
                    self.config.max_intent_attempts,
                    self.config.intent_failure_backoff_seconds,
                    self.config.intent_failure_backoff_max_seconds,
                )
                print(
                    f"[dispatcher] project={self.project_id} fail {record.mode} "
                    f"intent={record.intent_id} worker={record.worker_name} "
                    f"status={status} error={message[:300]}",
                    flush=True,
                )
                self.errors.append(
                    {
                        "kind": "worker_error",
                        "intent_id": record.intent_id,
                        "worker_name": record.worker_name,
                        "status": status,
                        "error": message,
                        "failure_category": failure_cycle[0] if failure_cycle else "",
                        "retry_after_seconds": failure_cycle[1] if failure_cycle else 0.0,
                    }
                )
                self.worker_pool.reject_temporarily(
                    record.worker_name,
                    self.config.worker_rejected_cooldown_seconds,
                )
                continue
            if result.get("lease_retained"):
                message = self._annotate_intent_failure(
                    "; ".join(result["report"].errors) or result["report"].stop_reason
                )
                await asyncio.to_thread(
                    self.board.annotate_latest_worker_run_error,
                    self.project_id,
                    record.intent_id,
                    message,
                )
                failure_cycle = self._record_intent_worker_failure(
                    record.intent_id,
                    record.mode,
                    record.worker_name,
                    message,
                )
                status = await asyncio.to_thread(
                    self.board.release_intent,
                    self.project_id,
                    record.intent_id,
                    record.owner_token,
                    message,
                    self.config.max_intent_attempts,
                    self.config.intent_failure_backoff_seconds,
                    self.config.intent_failure_backoff_max_seconds,
                )
                print(
                    f"[dispatcher] project={self.project_id} fail {record.mode} "
                    f"intent={record.intent_id} worker={record.worker_name} "
                    f"status={status} stop={result['report'].stop_reason} "
                    f"failure_streak=scheduled",
                    flush=True,
                )
                self.errors.append(
                    {
                        "kind": "worker_report_failed",
                        "intent_id": record.intent_id,
                        "worker_name": record.worker_name,
                        "status": status,
                        "error": message,
                        "failure_category": failure_cycle[0] if failure_cycle else "",
                        "retry_backoff_seconds": (
                            failure_cycle[1]
                            if failure_cycle
                            else self.config.intent_failure_backoff_seconds
                        ),
                    }
                )
                self.worker_pool.reject_temporarily(
                    record.worker_name,
                    self.config.worker_rejected_cooldown_seconds,
                )
                continue
            self._failed_workers_by_intent.pop(record.intent_id, None)
            self._intent_worker_cycle_until.pop(record.intent_id, None)
            self.history.append(result)
            progressed = bool(result["accepted_facts"] or result["accepted_hypotheses"])
            print(
                f"[dispatcher] project={self.project_id} finish {record.mode} "
                f"intent={record.intent_id} worker={record.worker_name} "
                f"accepted_facts={len(result['accepted_facts'])} "
                f"accepted_hypotheses={len(result['accepted_hypotheses'])} "
                f"new_intents={result.get('new_intents', 0)}",
                flush=True,
            )
            if progressed:
                self._global_audit_done = False
            self._pending_reason_signals += 1
            self._pending_reason_progress = self._pending_reason_progress or progressed
            self._last_reason_signal_at = time.monotonic()
        return completed

    async def _start_reason_if_ready(
        self,
        force: bool = False,
        global_audit: bool = False,
    ) -> bool:
        if self.board.get_project(self.project_id).status != "running":
            return False
        if self._reason_task is not None or self._active_count() >= self.config.max_project_workers:
            return False
        if not global_audit and self._pending_reason_signals == 0:
            return False
        age = time.monotonic() - self._last_reason_signal_at if self._last_reason_signal_at else 0.0
        ready = (
            force
            or global_audit
            or self._pending_reason_signals >= self.config.reason_batch_size
            or age >= self.config.reason_debounce_seconds
        )
        if not ready:
            return False
        cooldown_remaining = self._reason_cooldown_remaining()
        if cooldown_remaining > 0:
            current = time.monotonic()
            if current - self._last_cooldown_log >= max(5.0, self.config.state_heartbeat_interval):
                self._last_cooldown_log = current
                print(
                    f"[dispatcher] project={self.project_id} reason cooling_down "
                    f"retry_after={cooldown_remaining:.1f}s failures={self._reason_failures}",
                    flush=True,
                )
            return False
        if not self.global_capacity.try_acquire(self.project_id, "reason"):
            return False
        worker = self._acquire_reason_worker()
        if worker is None:
            self.global_capacity.release(self.project_id, "reason")
            return False
        reason_intent_id = new_id("reason")
        owner_token = new_id("reason-lease")
        trigger = "global_audit" if global_audit else "worker_signals"
        lease = await asyncio.to_thread(
            self.board.claim_reason_lease,
            self.project_id,
            owner_token,
            worker.name,
            trigger,
            # A freshly claimed Reason task still has to cross the asyncio ->
            # thread-pool boundary before Scheduler can verify ownership. On a
            # busy host that handoff may exceed a deliberately tiny test lease.
            # Keep a short startup grace; the heartbeat loop renews with the
            # configured duration as soon as work is running.
            max(self.config.reason_lease_seconds, 1.0),
            reason_intent_id,
        )
        if lease is None:
            self.worker_pool.release(worker.name)
            self.global_capacity.release(self.project_id, "reason")
            return False
        record = RunningReason(
            owner_token=owner_token,
            worker_name=worker.name,
            signals=self._pending_reason_signals,
            worker_progress=self._pending_reason_progress,
            global_audit=global_audit,
            intent_id=reason_intent_id,
        )
        self._pending_reason_signals = 0
        self._pending_reason_progress = False
        self._reason_record = record
        try:
            self._reason_task = asyncio.create_task(self._run_reason(record, worker))
        except Exception:
            self._reason_record = None
            await asyncio.to_thread(
                self.board.release_reason_lease,
                self.project_id,
                owner_token,
                "reason_task_submit_failed",
            )
            self.worker_pool.release(worker.name)
            self.global_capacity.release(self.project_id, "reason")
            raise
        print(
            f"[dispatcher] project={self.project_id} start reason "
            f"worker={worker.name} signals={record.signals} global_audit={global_audit}",
            flush=True,
        )
        self.board.add_event(
            self.project_id,
            "dispatcher.reason_started",
            {
                    "dispatcher_id": self.id,
                    "intent_id": reason_intent_id,
                    "worker_name": worker.name,
                    "owner_token": owner_token,
                    "trigger": trigger,
                    "lease_expires_at": lease["lease_expires_at"],
                    "signals": record.signals,
                    "global_audit": global_audit,
            },
        )
        return True

    async def _reap_reason(self) -> bool:
        if self._reason_task is None or not self._reason_task.done():
            return False
        task = self._reason_task
        record = self._reason_record
        self._reason_task = None
        self._reason_record = None
        if record is None:
            return False
        self.worker_pool.release(record.worker_name)
        self.global_capacity.release(self.project_id, "reason")
        lease_released = await asyncio.to_thread(
            self.board.release_reason_lease,
            self.project_id,
            record.owner_token,
            "reason_finished",
        )
        try:
            created = task.result()
        except asyncio.CancelledError:
            # A stop cancels the in-flight Reason task.  Its pending signal is
            # retained only for a still-running project, where a later
            # Dispatcher may legitimately resume planning.
            if self.board.get_project(self.project_id).status == "running":
                self._pending_reason_signals += max(1, record.signals)
                self._pending_reason_progress = self._pending_reason_progress or record.worker_progress
                self._last_reason_signal_at = time.monotonic()
            self.errors.append(
                {
                    "kind": "reason_cancelled",
                    "worker_name": record.worker_name,
                    "signals": record.signals,
                }
            )
            return True
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._failed_reason_workers.add(record.worker_name)
            self._pending_reason_signals += max(1, record.signals)
            self._pending_reason_progress = self._pending_reason_progress or record.worker_progress
            self._last_reason_signal_at = time.monotonic()
            retry_after = self._record_reason_failure_cooldown(record.worker_name, message)
            print(
                f"[dispatcher] project={self.project_id} fail reason "
                f"worker={record.worker_name} failures={self._reason_failures} "
                f"retry_after={retry_after:.1f}s error={message[:300]}",
                flush=True,
            )
            self.errors.append(
                {
                    "kind": "reason_error",
                    "worker_name": record.worker_name,
                    "error": message,
                    "failures": self._reason_failures,
                    "retry_after_seconds": retry_after,
                }
            )
            return True
        self._reason_failures = 0
        self._reason_cooldown_until = 0.0
        self._reason_paused_until = 0.0
        self._reason_failure_signature = ""
        self._failed_reason_workers.clear()
        await asyncio.to_thread(self.board.clear_reason_failure, self.project_id)
        self._global_audit_done = self._global_audit_done or record.global_audit
        self.reason_history.append(
            {
                "worker_name": record.worker_name,
                "signals": record.signals,
                "worker_progress": record.worker_progress,
                "global_audit": record.global_audit,
                "created_intents": created,
                "completion": self.scheduler.last_reason_completion,
                "report": self.scheduler.last_reason_report,
                "reason_kind": self.scheduler.last_reason_kind,
                "rejections": list(self.scheduler.last_reason_rejections),
                "lease_released": lease_released,
            }
        )
        print(
            f"[dispatcher] project={self.project_id} finish reason "
            f"worker={record.worker_name} created_intents={created} "
            f"completion={self.scheduler.last_reason_completion is not None} "
            f"kind={self.scheduler.last_reason_kind}",
            flush=True,
        )
        return True

    async def _abort_active_work(self, stop_reason: str) -> dict[str, list[dict[str, Any]]]:
        """Cancel all active workers and fence their leases before shutdown."""

        aborted: dict[str, list[dict[str, Any]]] = {"intents": [], "reason": []}
        await self._reap_intents()
        await self._reap_reason()
        cancelled_tasks: list[asyncio.Task] = []
        for task, record in list(self._tasks.items()):
            self._tasks.pop(task, None)
            try:
                await asyncio.to_thread(
                    self.worker_pool.cancel_active,
                    record.worker_name,
                    self.project_id,
                    record.intent_id,
                    stop_reason,
                )
            except Exception as exc:
                self.errors.append(
                    {
                        "kind": "worker_cancel_error",
                        "intent_id": record.intent_id,
                        "worker_name": record.worker_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            task.cancel()
            cancelled_tasks.append(task)
            self.worker_pool.release(record.worker_name)
            self.global_capacity.release(self.project_id, "intent")
            message = f"{stop_reason}: dispatcher stopped before worker completed"
            run_ids = await asyncio.to_thread(
                self.board.fail_running_worker_runs_for_owner,
                self.project_id,
                record.intent_id,
                record.owner_token,
                message,
                stop_reason,
            )
            status = await asyncio.to_thread(
                self.board.release_intent,
                self.project_id,
                record.intent_id,
                record.owner_token,
                message,
                self.config.max_intent_attempts,
                self.config.intent_failure_backoff_seconds,
                self.config.intent_failure_backoff_max_seconds,
                penalize=False,
            )
            item = {
                "intent_id": record.intent_id,
                "worker_name": record.worker_name,
                "owner_token": record.owner_token,
                "status": status,
                "run_ids": run_ids,
            }
            aborted["intents"].append(item)
            self.errors.append({"kind": "worker_aborted", **item, "error": message})
        if self._reason_task is not None and self._reason_record is not None:
            task = self._reason_task
            record = self._reason_record
            self._reason_task = None
            self._reason_record = None
            try:
                await asyncio.to_thread(
                    self.worker_pool.cancel_active,
                    record.worker_name,
                    self.project_id,
                    record.intent_id,
                    stop_reason,
                )
            except Exception as exc:
                self.errors.append(
                    {
                        "kind": "reason_cancel_error",
                        "worker_name": record.worker_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            task.cancel()
            cancelled_tasks.append(task)
            self.worker_pool.release(record.worker_name)
            self.global_capacity.release(self.project_id, "reason")
            self._pending_reason_signals += max(1, record.signals)
            self._pending_reason_progress = self._pending_reason_progress or record.worker_progress
            self._last_reason_signal_at = time.monotonic()
            message = f"{stop_reason}: dispatcher stopped before reason completed"
            lease_released = await asyncio.to_thread(
                self.board.release_reason_lease,
                self.project_id,
                record.owner_token,
                stop_reason,
            )
            run_ids = await asyncio.to_thread(
                self.board.fail_running_worker_runs_for_owner,
                self.project_id,
                None,
                record.owner_token,
                message,
                stop_reason,
            )
            item = {
                "worker_name": record.worker_name,
                "owner_token": record.owner_token,
                "run_ids": run_ids,
                "signals": record.signals,
                "global_audit": record.global_audit,
                "lease_released": lease_released,
            }
            aborted["reason"].append(item)
            self.errors.append({"kind": "reason_aborted", **item, "error": message})
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)
        if aborted["intents"] or aborted["reason"]:
            self.board.add_event(
                self.project_id,
                "dispatcher.active_work_aborted",
                {"dispatcher_id": self.id, "stop_reason": stop_reason, "aborted": aborted},
            )
        return aborted

    async def initialize(self) -> None:
        if self._initialized:
            return
        imported_scope_hints = await asyncio.to_thread(
            self.board.ensure_scope_hints,
            self.project_id,
        )
        # Older Slime releases could permanently mark an Intent failed after a
        # fixed retry cap. When the active configuration selects Cairn's open
        # queue semantics, restore those rows to pending without erasing their
        # attempts, failure streak, last error, or Worker Run history.
        if self.config.max_intent_attempts == 0:
            reopened_failed_intents = await asyncio.to_thread(
                self.board.reopen_failed_intents,
                self.project_id,
                "cairn_open_queue",
            )
        else:
            reopened_failed_intents = []
        self.recovery = await asyncio.to_thread(
            self.board.recover_expired_intents,
            self.project_id,
            self.config.max_intent_attempts,
            self.config.intent_failure_backoff_seconds,
            self.config.intent_failure_backoff_max_seconds,
        )
        if reopened_failed_intents:
            self.recovery["reopened_failed_intents"] = reopened_failed_intents
        expired_reason_leases = await asyncio.to_thread(
            self.board.expire_reason_leases,
            self.project_id,
        )
        if expired_reason_leases:
            self.recovery["expired_reason_leases"] = expired_reason_leases
            expired_reason_run_ids: list[str] = []
            for lease in expired_reason_leases:
                expired_reason_run_ids.extend(
                    await asyncio.to_thread(
                        self.board.fail_running_worker_runs_for_owner,
                        self.project_id,
                        None,
                        lease["owner_token"],
                        "dispatcher startup recovered an expired Reason lease",
                        "reason_lease_expired",
                    )
                )
            if expired_reason_run_ids:
                self.recovery["expired_reason_worker_runs"] = expired_reason_run_ids
        stale_worker_runs = await asyncio.to_thread(
            self.board.fail_stale_worker_runs,
            self.project_id,
            time.time() + 1.0,
            "dispatcher startup found an orphaned running worker row with no tracked task",
            "dispatcher_startup_orphaned_run",
        )
        if stale_worker_runs:
            self.recovery["stale_worker_runs"] = stale_worker_runs
        project = self.board.get_project(self.project_id)
        bootstrap_worker_configured = self._bootstrap_worker_configured()
        bootstrap_active = self._bootstrap_path_active(project)
        if not bootstrap_active:
            archived_bootstrap_intents = await asyncio.to_thread(
                self.board.archive_pending_bootstrap_intents,
                self.project_id,
                self._bootstrap_archive_reason(project, bootstrap_worker_configured),
            )
            if archived_bootstrap_intents:
                self.recovery["archived_bootstrap_intents"] = archived_bootstrap_intents
        reason_state = self.board.get_reason_state(self.project_id)
        self._reason_failures = int(reason_state.get("failure_streak", 0) or 0)
        self._reason_failure_signature = self._stable_reason_error_signature(
            str(reason_state.get("failure_last_error", "") or "")
        )
        self._reason_paused_until = float(reason_state.get("failure_paused_until", 0.0) or 0.0)
        self._reason_cooldown_until = time.monotonic() + max(
            0.0, self._reason_paused_until - time.time()
        )
        hints = await asyncio.to_thread(self.board.list_hints, self.project_id)
        self._observed_hint_ids = {hint.id for hint in hints}
        pending_hints = [
            hint
            for hint in hints
            if hint.created_at > float(reason_state.get("last_hint_created_at", 0.0))
        ]
        if pending_hints:
            self._queue_hint_reason_signal(pending_hints, "dispatcher_startup")
        if imported_scope_hints:
            self.recovery["imported_scope_hints"] = [hint.id for hint in imported_scope_hints]
        # Bootstrap projects keep Origin/Goal as pinned context until a Worker
        # reports evidence.  Reason-first projects deliberately use the same
        # anchors as their initial planning signal.
        meaningful_facts = [
            fact
            for fact in self.board.list_facts(self.project_id)
            if not bool(fact.attributes.get("seed"))
        ]
        unreasoned = self.scheduler._recent_fact_ids(meaningful_facts, reason_state)
        needs_initial_reason = (
            not bootstrap_active
            and int(reason_state.get("incremental_runs", 0)) == 0
        )
        if unreasoned or needs_initial_reason:
            self._pending_reason_signals = max(1, self._pending_reason_signals)
            self._pending_reason_progress = True
            self._last_reason_signal_at = time.monotonic()
        else:
            recent_runs = await asyncio.to_thread(self.board.list_worker_runs, self.project_id, 20)
            last_reason_state_update = float(reason_state.get("updated_at", 0.0) or 0.0)
            reason_runs = [run for run in recent_runs if run.get("mode") == "reason"]
            latest_reason_run = reason_runs[0] if reason_runs else None
            failed_reason_after_cursor = latest_reason_run is not None and (
                latest_reason_run.get("status") == "failed"
                and float(latest_reason_run.get("finished_at") or latest_reason_run.get("started_at") or 0.0)
                >= last_reason_state_update - 5.0
            )
            failed_reason_after_state = [
                run
                for run in reason_runs
                if run is not latest_reason_run
                and run.get("status") == "failed"
                and float(run.get("finished_at") or 0.0) > last_reason_state_update
            ]
            if failed_reason_after_cursor or failed_reason_after_state:
                self._pending_reason_signals = max(1, self._pending_reason_signals)
                self._pending_reason_progress = True
                self._last_reason_signal_at = time.monotonic()
        if expired_reason_leases:
            self._pending_reason_signals = max(1, self._pending_reason_signals)
            self._pending_reason_progress = True
            self._last_reason_signal_at = time.monotonic()
        self._initialized = True
        self._state = "idle"

    async def cycle(self, max_dispatch: int | None = 1) -> dict[str, int | bool]:
        """Run one fair scheduling cycle; service mode uses one dispatch per project."""

        await self.initialize()
        project_status = self.board.get_project(self.project_id).status
        if project_status != "running":
            self._state = f"project_{project_status}"
            self._stop_event.set()
            await self.persist_state(force=True)
            return {
                "reaped_intents": 0,
                "reaped_reason": False,
                "reason_started": False,
                "dispatched": 0,
            }
        reaped_intents = await self._reap_intents()
        reaped_reason = await self._reap_reason()
        project_status = self.board.get_project(self.project_id).status
        if project_status != "running":
            self._state = f"project_{project_status}"
            self._stop_event.set()
            await self.persist_state(force=True)
            return {
                "reaped_intents": reaped_intents,
                "reaped_reason": reaped_reason,
                "reason_started": False,
                "dispatched": 0,
            }
        await self._observe_hint_signals()
        reason_started = await self._start_reason_if_ready()
        dispatched = await self._dispatch_available(max_dispatch=max_dispatch)

        if self._active_count() == 0 and self._pending_reason_signals:
            reason_started = await self._start_reason_if_ready(force=True) or reason_started
        await self.persist_state()
        return {
            "reaped_intents": reaped_intents,
            "reaped_reason": reaped_reason,
            "reason_started": reason_started,
            "dispatched": dispatched,
        }

    async def persist_state(self, force: bool = False) -> None:
        current = time.monotonic()
        if not force and current - self._last_state_heartbeat < self.config.state_heartbeat_interval:
            return
        status = self.status()
        # Shared WorkerPool details are useful in live API responses, but complete
        # histories remain in worker_runs and are not copied into this heartbeat.
        await asyncio.to_thread(
            self.board.save_dispatcher_state,
            self.project_id,
            self.id,
            self._state,
            status,
        )
        self._last_state_heartbeat = current
        self._log_progress(current, status)

    def _log_progress(self, current: float, status: dict[str, Any]) -> None:
        interval = max(5.0, float(os.environ.get("SLIME_PROGRESS_LOG_INTERVAL", "30")))
        if current - self._last_progress_log < interval:
            return
        self._last_progress_log = current
        running = []
        for item in status.get("running_intents", []):
            elapsed = max(0, int(time.time() - float(item.get("started_at") or time.time())))
            running.append(
                f"{item.get('mode')}:{item.get('intent_id')}:{item.get('worker_name')}:{elapsed}s"
            )
        reason = status.get("reason_running")
        if reason:
            elapsed = max(0, int(time.time() - float(reason.get("started_at") or time.time())))
            running.append(
                f"reason:{reason.get('worker_name')}:signals={reason.get('signals')}:"
                f"global={reason.get('global_audit')}:{elapsed}s"
            )
        if running:
            print(
                f"[dispatcher] project={self.project_id} running "
                + " ".join(running),
                flush=True,
            )
        elif status.get("pending_reason_signals") and status.get("reason_cooldown_remaining", 0) > 0:
            print(
                f"[dispatcher] project={self.project_id} reason cooling_down "
                f"retry_after={float(status['reason_cooldown_remaining']):.1f}s "
                f"signals={status.get('pending_reason_signals')}",
                flush=True,
            )

    async def _wait_one_cycle(self) -> None:
        active_tasks = list(self._tasks)
        if self._reason_task is not None:
            active_tasks.append(self._reason_task)
        stop_wait = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                [*active_tasks, stop_wait],
                timeout=self.config.cycle_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)

    async def serve(self) -> dict[str, Any]:
        """Continuously discover API-created Intents until a hard stop is requested."""

        await self.initialize()
        # A Dispatcher instance is a one-shot runtime.  Do not clear here: the
        # service may request shutdown after create_task() but before this
        # coroutine gets its first turn, and clearing would lose that signal.
        self._state = "running"
        self.board.add_event(
            self.project_id,
            "dispatcher.started",
            {"dispatcher_id": self.id},
        )
        print(
            f"[dispatcher] project={self.project_id} dispatcher_started id={self.id}",
            flush=True,
        )
        await self.persist_state(force=True)
        while not self._stop_event.is_set():
            result = await self.cycle(max_dispatch=1)
            # Match Cairn's round-robin fill behavior. Each project submits one
            # task per turn, yields to peer projects, then immediately takes
            # another turn while both global and project capacity remain.
            can_refill = (
                bool(result["dispatched"])
                and self.global_capacity.available()
                and self._active_count() < self.config.max_project_workers
            )
            if can_refill:
                await asyncio.sleep(0)
            else:
                await self._wait_one_cycle()

        project_status = self.board.get_project(self.project_id).status
        stop_reason = "project_stopped" if project_status != "running" else "dispatcher_stopped"
        self._state = "stopping"
        await self.persist_state(force=True)
        aborted = await self._abort_active_work(stop_reason)
        project_status = self.board.get_project(self.project_id).status
        self._state = "stopped" if project_status == "running" else f"project_{project_status}"
        self.board.add_event(
            self.project_id,
            "dispatcher.stopped",
            {"dispatcher_id": self.id, "stop_reason": stop_reason, "aborted": aborted},
        )
        print(
            f"[dispatcher] project={self.project_id} dispatcher_stopped state={self._state}",
            flush=True,
        )
        await self.persist_state(force=True)
        return self.status()

    def status(self, state: str | None = None) -> dict[str, Any]:
        return {
            "dispatcher_id": self.id,
            "project_id": self.project_id,
            "project_status": self.board.get_project(self.project_id).status,
            "state": state or self._state,
            "running_intents": [asdict(record) for record in self._tasks.values()],
            "reason_running": asdict(self._reason_record) if self._reason_record else None,
            "reason_lease": self.board.get_reason_lease(self.project_id),
            "pending_reason_signals": self._pending_reason_signals,
            "reason_failures": self._reason_failures,
            "reason_cooldown_until": self._reason_cooldown_until,
            "reason_cooldown_remaining": self._reason_cooldown_remaining(),
            "reason_paused": self._reason_cooldown_remaining() > 0,
            "reason_failure_signature": self._reason_failure_signature,
            "workers": self.worker_pool.snapshot(),
            "global_capacity": self.global_capacity.snapshot(),
            "completed_workers": len(self.history),
            "completed_reason_runs": len(self.reason_history),
            "errors": list(self.errors),
            "recovery": self.recovery,
        }

    async def run_until_idle(self, max_cycles: int = 10_000) -> dict[str, Any]:
        """Drain the project queue while keeping Explore parallel and Reason single-instance."""

        await self.initialize()
        self._state = "running_until_idle"
        state = "idle"
        aborted: dict[str, list[dict[str, Any]]] = {"intents": [], "reason": []}
        for _ in range(max_cycles):
            await self._reap_intents()
            await self._reap_reason()
            project_status = self.board.get_project(self.project_id).status
            if project_status == "running":
                await self._observe_hint_signals()
                await self._start_reason_if_ready()
                await self._dispatch_available()

            pending = self.board.list_intents(self.project_id, "pending")
            active_tasks = list(self._tasks)
            if self._reason_task is not None:
                active_tasks.append(self._reason_task)

            if project_status != "running":
                aborted = await self._abort_active_work("project_stopped")
                state = f"project_{project_status}"
                break

            if not active_tasks:
                if self._pending_reason_signals:
                    if await self._start_reason_if_ready(force=True):
                        continue
                    if "reason" in self.worker_pool.configured_task_types(healthy_only=True):
                        await asyncio.sleep(self.config.cycle_interval)
                        continue
                    state = "blocked_reason_worker"
                    break
                if pending:
                    pending_modes = {self._intent_mode(intent) for intent in pending}
                    healthy_modes = self.worker_pool.configured_task_types(healthy_only=True)
                    if pending_modes <= healthy_modes:
                        await asyncio.sleep(self.config.cycle_interval)
                        continue
                    state = "blocked_worker_availability"
                    break
                state = "idle"
                break

            await asyncio.wait(
                active_tasks,
                timeout=self.config.cycle_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            state = "cycle_limit"
            aborted = await self._abort_active_work("cycle_limit")

        result = self.status(state)
        self._state = state
        await self.persist_state(force=True)
        result["history"] = list(self.history)
        result["reason_history"] = list(self.reason_history)
        result["aborted"] = aborted
        return result

    def request_stop(self) -> None:
        self._stop_event.set()
