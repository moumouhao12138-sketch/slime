from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from .cairn_contracts import (
    extract_explore_submissions,
    extract_submission_candidates,
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)
from .execution import CommandExecution, InterruptibleCommand
from .errors import ModelInvocationCancelled, ModelInvocationError, ModelInvocationTimeout
from .model_health import ModelEndpoint
from .models import (
    CompletionProposal,
    Fact,
    FactCandidate,
    HypothesisCandidate,
    Intent,
    IntentProposal,
    PseudopodReport,
    WorkerTask,
    coerce_unit_score,
    new_id,
    now,
)
from .prompting import load_prompt, render_prompt, validate_prompt_group
from .slime_layer import SlimeGrowthLayer


class NativeAgentBackend(Protocol):
    """The small part of PersistentDockerBackend required by a native Agent."""

    workspace_root: Path

    def execute_with_environment(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        environment: dict[str, str] | None = None,
    ) -> CommandExecution: ...

    def start_with_environment(
        self,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> InterruptibleCommand: ...


NativeBackendResolver = Callable[[str], NativeAgentBackend]


class NativeConcludeFallbackError(ModelInvocationError):
    """A failed execute pass whose same-session conclude pass also failed."""


@dataclass(frozen=True, slots=True)
class NativeAgentConfig:
    worker_name: str
    adapter: str
    binary: str
    model: str = ""
    provider: str = ""
    thinking: str = ""
    prompt_group: str = "default"
    # ``timeout`` remains a compatibility fallback for hand-built Worker
    # configs. Cairn dispatch configs set the explicit per-task values below.
    timeout: int = 300
    bootstrap_timeout: int | None = None
    bootstrap_conclude_timeout: int | None = None
    reason_timeout: int | None = None
    reason_max_intents: int = 2
    explore_timeout: int | None = None
    explore_conclude_timeout: int | None = None
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    config_overrides: tuple[tuple[str, Any], ...] = ()
    max_report_items: int = 20
    max_transcript_chars: int = 2_000_000
    healthcheck_timeout: float = 15.0
    healthcheck_cache_seconds: float = 60.0
    healthcheck_failure_cache_seconds: float = 15.0
    model_endpoint: ModelEndpoint | None = None
    model_healthcheck_mode: str = "disabled"
    # Hand-built integrations retain the explicit probe; dispatch configs
    # default this off to match Cairn's startup-only container contract.
    container_preflight: bool = True

    def __post_init__(self) -> None:
        if self.adapter not in {"claude-code", "codex-cli", "pi-cli"}:
            raise ValueError(f"native-agent 不支持 Adapter: {self.adapter}")
        if not self.worker_name.strip() or not self.binary.strip():
            raise ValueError("native-agent 需要 worker_name 和 binary")
        validate_prompt_group(self.prompt_group)
        if self.timeout < 1 or self.max_report_items < 1 or self.max_transcript_chars < 10_000:
            raise ValueError("native-agent 的 timeout/report/transcript 限制无效")
        for name, value in (
            ("bootstrap_timeout", self.bootstrap_timeout),
            ("bootstrap_conclude_timeout", self.bootstrap_conclude_timeout),
            ("reason_timeout", self.reason_timeout),
            ("explore_timeout", self.explore_timeout),
            ("explore_conclude_timeout", self.explore_conclude_timeout),
        ):
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ValueError(f"native-agent {name} must be a positive integer")
        if isinstance(self.reason_max_intents, bool) or self.reason_max_intents < 1:
            raise ValueError("native-agent reason_max_intents must be a positive integer")
        if (
            self.healthcheck_timeout < 1
            or self.healthcheck_cache_seconds <= 0
            or self.healthcheck_failure_cache_seconds <= 0
        ):
            raise ValueError("native-agent healthcheck configuration is invalid")
        if self.model_healthcheck_mode not in {"disabled", "startup_only", "startup_and_task"}:
            raise ValueError("native-agent model healthcheck mode is invalid")
        if not isinstance(self.container_preflight, bool):
            raise ValueError("native-agent container_preflight must be boolean")
        if self.model_endpoint is not None and not self.model.strip():
            raise ValueError("native-agent model endpoint requires model")
        for raw_key, _ in self.config_overrides:
            key = str(raw_key).strip()
            if not key or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                raise ValueError(f"native-agent config override key 无效: {raw_key}")

    def timeout_for(self, mode: str, *, conclude: bool = False) -> int:
        """Return the Cairn phase timeout for a native CLI invocation."""

        if mode == "bootstrap":
            value = self.bootstrap_conclude_timeout if conclude else self.bootstrap_timeout
        elif mode == "explore":
            value = self.explore_conclude_timeout if conclude else self.explore_timeout
        elif mode == "reason":
            if conclude:
                raise ValueError("Reason has no Cairn conclude phase")
            value = self.reason_timeout
        else:
            raise ValueError(f"native-agent unsupported task mode: {mode}")
        return self.timeout if value is None else value


@dataclass(slots=True)
class NativeSessionState:
    project_id: str
    intent_id: str
    mode: str
    session_id: str
    workspace_root: Path
    directory: Path
    container_directory: str
    backend: NativeAgentBackend
    resource_token: str
    port_range: tuple[int, int]
    turns: int = 0
    resumed: bool = False
    # ``resumed`` records whether this task started from a persisted session.
    # ``resume_session`` controls the next CLI invocation and becomes true only
    # after this process has a session identifier the CLI can actually resume.
    resume_session: bool = False
    resume_allowed: bool = True
    status: str = "active"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: str = ""
    transcript_refs: list[str] = field(default_factory=list)
    fallback: dict[str, Any] = field(default_factory=dict)
    command_count: int = 0
    health: dict[str, Any] = field(default_factory=dict)
    report_usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_calls": 0,
        }
    )


@dataclass(frozen=True, slots=True)
class NativeTaskHealth:
    project_id: str
    worker_name: str
    checked_at: float
    expires_at: float
    healthy: bool
    binary: str
    config_directory: str
    detail: str


class NativeAgentMind:
    """Run a full autonomous CLI Agent task inside the project's Kali container.

    The Agent can use its native shell/file tools. It returns one bounded JSON
    report, and Scheduler performs the only Blackboard write-back.
    """

    execution_mode = "native-agent"
    cairn_native_runner = True
    recoverable_statuses = frozenset({"active", "failed", "interrupted"})
    _resource_lock = threading.RLock()
    _active_port_blocks: dict[str, dict[int, str]] = {}

    def __init__(self, config: NativeAgentConfig, backend_resolver: NativeBackendResolver) -> None:
        self.config = config
        self.backend_resolver = backend_resolver
        self._local = threading.local()
        self._manifest_lock = threading.RLock()
        self._active_lock = threading.RLock()
        self._active_commands: dict[str, tuple[NativeSessionState, InterruptibleCommand | None]] = {}
        self._cancelled_commands: dict[str, str] = {}
        self._health_lock = threading.RLock()
        self._health_cache: dict[str, NativeTaskHealth] = {}
        self._health_key_locks: dict[str, threading.Lock] = {}

    def _state(self) -> NativeSessionState:
        state = getattr(self._local, "state", None)
        if state is None:
            raise RuntimeError("native-agent 尚未 begin_session")
        return state

    def _register_active_command(self, state: NativeSessionState, command: InterruptibleCommand) -> None:
        with self._active_lock:
            self._active_commands[state.resource_token] = (state, command)
            reason = self._cancelled_commands.get(state.resource_token, "")
        if reason:
            command.cancel(reason)

    def _register_pending_session(self, state: NativeSessionState) -> None:
        """Make a just-created session visible to cancellation before CLI launch."""

        with self._active_lock:
            self._active_commands[state.resource_token] = (state, None)

    def _unregister_active_command(self, state: NativeSessionState) -> None:
        with self._active_lock:
            self._active_commands.pop(state.resource_token, None)
            self._cancelled_commands.pop(state.resource_token, None)

    def _release_active_command_handle(self, state: NativeSessionState) -> None:
        """Keep the session cancellable while it imports the command result."""

        with self._active_lock:
            current = self._active_commands.get(state.resource_token)
            if current is not None and current[0] is state:
                self._active_commands[state.resource_token] = (state, None)

    def _cancel_reason_for(self, state: NativeSessionState) -> str:
        with self._active_lock:
            return self._cancelled_commands.get(state.resource_token, "")

    def _raise_if_cancelled(self, state: NativeSessionState, stage: str) -> None:
        reason = self._cancel_reason_for(state)
        if reason:
            raise ModelInvocationCancelled(
                f"native {self.config.adapter} cancelled {stage}: {reason}"
            )

    def cancel_active(
        self,
        *,
        project_id: str | None = None,
        intent_id: str | None = None,
        reason: str = "cancelled",
    ) -> dict[str, Any]:
        """Interrupt active CLI processes for this Worker instance.

        ``project_id`` and ``intent_id`` are optional so callers that know the
        exact lease can target one task.  The existing WorkerPool API supplies
        only a Worker name, in which case every active task for that Worker is
        cancelled before its project runtime is drained.
        """

        text = str(reason).strip() or "cancelled"
        with self._active_lock:
            selected = [
                (token, state, command)
                for token, (state, command) in self._active_commands.items()
                if (project_id is None or state.project_id == project_id)
                and (intent_id is None or state.intent_id == intent_id)
            ]
            newly_cancelled = [
                token for token, _, _ in selected if token not in self._cancelled_commands
            ]
            for token in newly_cancelled:
                self._cancelled_commands[token] = text
        tasks: list[dict[str, str]] = []
        for _, state, command in selected:
            if command is not None:
                command.cancel(text)
            tasks.append({"project_id": state.project_id, "intent_id": state.intent_id, "mode": state.mode})
        return {
            "worker_name": self.config.worker_name,
            "cancelled": len(newly_cancelled),
            "active_matches": len(selected),
            "tasks": tasks,
        }

    @staticmethod
    def _safe_name(value: str, limit: int = 48) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:limit] or "agent"

    @staticmethod
    def _manifest_path(directory: Path) -> Path:
        return directory / "session.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        with self._manifest_lock:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)

    def _install_resident_instructions(self, workspace_root: Path) -> None:
        """Install the selected prompt group's persistent CLI instructions."""

        instructions = load_prompt(self.config.prompt_group, "AGENTS.md")
        with self._resource_lock:
            for name in ("AGENTS.md", "CLAUDE.md"):
                path = workspace_root / name
                if path.is_file() and path.read_text(encoding="utf-8") == instructions:
                    continue
                temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
                try:
                    temporary.write_text(instructions, encoding="utf-8")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)

    def _write_prompt_file(self, state: NativeSessionState, prompt: str) -> str:
        """Persist a model prompt so it never crosses the Windows host argv boundary."""

        path = state.directory / "prompt.txt"
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        resolved = path
        try:
            with self._manifest_lock:
                temporary.write_text(prompt, encoding="utf-8")
                try:
                    temporary.replace(path)
                except PermissionError:
                    # Docker Desktop may still hold the previous prompt.txt
                    # open while a resumed CLI task is reading it. A unique
                    # path avoids replacing that locked file and is safe to
                    # pass to the next native invocation.
                    resolved = path.with_name(f"prompt-{uuid4().hex}.txt")
                    temporary.replace(resolved)
        finally:
            temporary.unlink(missing_ok=True)
        return f"{state.container_directory}/{resolved.name}"

    def _write_manifest(self, state: NativeSessionState) -> None:
        state.updated_at = time.time()
        self._write_json_atomic(
            self._manifest_path(state.directory),
            {
                "execution": self.execution_mode,
                "adapter": self.config.adapter,
                "worker_name": self.config.worker_name,
                "project_id": state.project_id,
                "intent_id": state.intent_id,
                "mode": state.mode,
                "session_id": state.session_id,
                "directory": state.directory.relative_to(state.workspace_root).as_posix(),
                "container_directory": state.container_directory,
                "turns": state.turns,
                "resumed": state.resumed,
                "resume_session": state.resume_session,
                "resume_allowed": state.resume_allowed,
                "status": state.status,
                "started_at": state.started_at,
                "updated_at": state.updated_at,
                "last_error": state.last_error,
                "transcript_refs": list(state.transcript_refs),
                "fallback": dict(state.fallback),
                "command_count": state.command_count,
                "health": dict(state.health),
                "reserved_port_range": list(state.port_range),
            },
        )

    @classmethod
    def _reserve_port_range(cls, project_id: str, token: str) -> tuple[int, int]:
        block_size = 16
        block_count = 2048
        preferred = int(sha256(token.encode("utf-8")).hexdigest()[:8], 16) % block_count
        with cls._resource_lock:
            blocks = cls._active_port_blocks.setdefault(project_id, {})
            for offset in range(block_count):
                index = (preferred + offset) % block_count
                owner = blocks.get(index)
                if owner is None or owner == token:
                    blocks[index] = token
                    start = 20_000 + index * block_size
                    return start, start + block_size - 1
        raise RuntimeError(f"项目 {project_id} 没有可分配的 native-agent 端口块")

    @classmethod
    def _release_port_range(cls, project_id: str, token: str) -> None:
        with cls._resource_lock:
            blocks = cls._active_port_blocks.get(project_id, {})
            for index, owner in list(blocks.items()):
                if owner == token:
                    blocks.pop(index, None)
            if not blocks:
                cls._active_port_blocks.pop(project_id, None)

    def _manifest_has_invalid_resume(self, manifest: dict[str, Any]) -> bool:
        """Recognize old session records that a native CLI has rejected.

        The per-pod manifest long outlives a CLI process.  When a provider has
        discarded a remote thread, retrying ``resume`` with the same ID creates
        a permanent failure loop.  Keep this narrowly scoped to each CLI's
        explicit missing-session diagnostics so ordinary task failures can
        still retain their conversation.
        """

        detail = "\n".join(
            (
                str(manifest.get("last_error", "")),
                json.dumps(manifest.get("fallback") or {}, ensure_ascii=False),
            )
        ).lower()
        markers = {
            "codex-cli": (
                "thread/resume failed",
                "no rollout found for thread id",
            ),
            "claude-code": (
                "failed to resume",
                "session not found",
                "conversation not found",
                "could not find session",
            ),
            "pi-cli": ("no session found matching",),
        }
        return any(marker in detail for marker in markers[self.config.adapter])

    @staticmethod
    def _mark_session_unusable(state: NativeSessionState) -> None:
        """Force the next dispatcher retry to create a new CLI session."""

        state.resume_allowed = False
        state.resume_session = False

    def _failed_execute_session_unusable(self, result: CommandExecution) -> bool:
        """Identify failures where a same-session conclude call is known to be futile.

        A ``thread.started`` event only proves that Codex assigned a resumable
        thread ID.  It is emitted during ordinary turns too, including turns
        that later end because a tool call or model response was rejected.  Do
        not discard that session unless the CLI reports an explicit missing or
        invalid-session diagnostic.
        """

        detail = f"{result.stdout}\n{result.stderr}".lower()
        invalid_markers = (
            "no rollout found for thread id",
            "thread/resume failed",
            "session not found",
            "no session found matching",
        )
        if any(marker in detail for marker in invalid_markers):
            return True
        return False

    def begin_session(self, task: WorkerTask, intent: Intent) -> dict[str, Any]:
        backend = self.backend_resolver(intent.project_id)
        workspace_root = backend.workspace_root.resolve()
        self._install_resident_instructions(workspace_root)
        digest = sha256(
            f"{self.config.worker_name}:{task.mode}:{intent.id}".encode("utf-8")
        ).hexdigest()[:24]
        relative = Path("pods") / self._safe_name(self.config.worker_name) / digest
        directory = (workspace_root / relative).resolve()
        directory.relative_to(workspace_root)
        directory.mkdir(parents=True, exist_ok=True)
        for child in ("evidence", "transcripts", "results", "sessions"):
            (directory / child).mkdir(parents=True, exist_ok=True)
        (workspace_root / "shared").mkdir(parents=True, exist_ok=True)

        manifest = self._read_json(self._manifest_path(directory))
        prior_turns = int(manifest.get("turns", 0) or 0)
        resumable = (
            manifest.get("adapter") == self.config.adapter
            and manifest.get("worker_name") == self.config.worker_name
            and manifest.get("status") in self.recoverable_statuses
            and prior_turns > 0
            and bool(manifest.get("session_id"))
            and manifest.get("resume_allowed") is not False
            and not self._manifest_has_invalid_resume(manifest)
        )
        container_directory = str(PurePosixPath("/workspace", *relative.parts))
        resource_token = f"{intent.project_id}:{self.config.worker_name}:{task.mode}:{intent.id}"
        port_range = self._reserve_port_range(intent.project_id, resource_token)
        try:
            state = NativeSessionState(
                project_id=intent.project_id,
                intent_id=intent.id,
                mode=task.mode,
                session_id=str(manifest.get("session_id")) if resumable else str(uuid4()),
                workspace_root=workspace_root,
                directory=directory,
                container_directory=container_directory,
                backend=backend,
                resource_token=resource_token,
                port_range=port_range,
                # Keep artifact numbering monotonic even after a rejected
                # session is replaced by a fresh native CLI session.
                turns=prior_turns,
                resumed=resumable,
                resume_session=resumable,
                status="active",
                started_at=float(manifest.get("started_at", time.time())) if resumable else time.time(),
                transcript_refs=[str(item) for item in manifest.get("transcript_refs", [])] if resumable else [],
                fallback=dict(manifest.get("fallback") or {}) if resumable else {},
                command_count=int(manifest.get("command_count", 0) or 0) if resumable else 0,
            )
            self._local.state = state
            self._write_manifest(state)
            self._register_pending_session(state)
            return self.session_info()
        except Exception:
            pending = locals().get("state")
            if isinstance(pending, NativeSessionState):
                self._unregister_active_command(pending)
            self._release_port_range(intent.project_id, resource_token)
            raise

    def end_session(self, outcome: str = "completed") -> dict[str, Any]:
        state = self._state()
        state.status = outcome or "completed"
        try:
            self._write_manifest(state)
            return self.session_info()
        finally:
            self._unregister_active_command(state)
            self._release_port_range(state.project_id, state.resource_token)

    def session_info(self) -> dict[str, Any]:
        state = self._state()
        return {
            "execution": self.execution_mode,
            "adapter": f"native-{self.config.adapter}",
            "worker_name": self.config.worker_name,
            "session_id": state.session_id,
            "turns": state.turns,
            "resumed": state.resumed,
            "resume_allowed": state.resume_allowed,
            "recoverable": True,
            "status": state.status,
            "manifest": str(self._manifest_path(state.directory)),
            "pod_directory": state.directory.relative_to(state.workspace_root).as_posix(),
            "container_directory": state.container_directory,
            "transcript_refs": list(state.transcript_refs),
            "fallback": dict(state.fallback),
            "command_count": state.command_count,
            "health": dict(state.health),
            "reserved_port_range": list(state.port_range),
            "last_error": state.last_error,
        }

    def healthcheck(self) -> dict[str, Any]:
        """Validate an explicit Cairn container-model configuration before dispatch.

        The CLI binary remains a project-container concern.  The model endpoint
        can be checked eagerly because it is configured explicitly and does not
        need a project workspace or a native CLI session.
        """

        if self.config.model_healthcheck_mode == "disabled":
            return {
                "healthy": True,
                "adapter": f"native-{self.config.adapter}",
                "binary": self.config.binary,
                "skipped": True,
                "detail": "model healthcheck disabled",
            }
        if self.config.model_endpoint is None:
            return {
                "healthy": False,
                "adapter": f"native-{self.config.adapter}",
                "binary": self.config.binary,
                "detail": "explicit container model endpoint is not configured",
            }
        result = self.config.model_endpoint.probe(
            self.config.model,
            self.config.healthcheck_timeout,
            self.config.environment,
        )
        result.update(
            {
                "adapter": f"native-{self.config.adapter}",
                "binary": self.config.binary,
                "model": self.config.model,
            }
        )
        return result

    def _model_healthcheck(self) -> dict[str, Any]:
        """Run a non-session model probe for the ``startup_and_task`` mode."""

        if self.config.model_endpoint is None:
            return {
                "healthy": False,
                "detail": "explicit container model endpoint is not configured",
            }
        return self.config.model_endpoint.probe(
            self.config.model,
            self.config.healthcheck_timeout,
            self.config.environment,
        )

    def _model_health_error(self, result: dict[str, Any]) -> ModelInvocationError:
        status = result.get("status")
        detail = str(result.get("detail", ""))[:1000]
        return ModelInvocationError(
            "native worker model healthcheck failed "
            f"worker={self.config.worker_name} status={status if status is not None else '-'}: {detail}"
        )

    def consume_usage(self) -> dict[str, int]:
        state = self._state()
        usage = dict(state.report_usage)
        state.report_usage = {key: 0 for key in usage}
        return usage

    def _health_cache_key(self, state: NativeSessionState) -> str:
        return f"{state.project_id}\x1f{self.config.worker_name}"

    def _health_config_directory(self) -> str:
        names = {
            "claude-code": ("CLAUDE_CONFIG_DIR", "HOME"),
            "codex-cli": ("CODEX_HOME", "HOME"),
            "pi-cli": ("PI_CODING_AGENT_DIR",),
        }[self.config.adapter]
        for name in names:
            value = str(self.config.environment.get(name, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _health_payload(health: NativeTaskHealth, *, cached: bool) -> dict[str, Any]:
        return {
            "checked_at": health.checked_at,
            "expires_at": health.expires_at,
            "healthy": health.healthy,
            "cached": cached,
            "binary": health.binary,
            "config_directory": health.config_directory,
            "detail": health.detail,
        }

    def _cached_task_health(self, key: str) -> NativeTaskHealth | None:
        current = time.time()
        with self._health_lock:
            health = self._health_cache.get(key)
            if health is not None and health.expires_at > current:
                return health
        return None

    def _health_lock_for(self, key: str) -> threading.Lock:
        with self._health_lock:
            lock = self._health_key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._health_key_locks[key] = lock
            return lock

    def _probe_task_health(self, state: NativeSessionState) -> NativeTaskHealth:
        config_directory = self._health_config_directory()
        script = r"""binary="$1"
config_dir="$2"
if ! command -v "$binary" >/dev/null 2>&1; then
  printf 'health_error=binary_not_found:%s\n' "$binary"
  exit 41
fi
version="$($binary --version 2>&1)"
version_status=$?
printf 'binary=%s\nversion=%s\n' "$binary" "$version"
if [ "$version_status" -ne 0 ]; then
  printf 'health_error=version_failed:%s\n' "$binary"
  exit 42
fi
if [ -n "$config_dir" ]; then
  if [ ! -d "$config_dir" ]; then
    printf 'health_error=config_directory_missing:%s\n' "$config_dir"
    exit 43
  fi
  if [ ! -r "$config_dir" ] || [ ! -x "$config_dir" ]; then
    printf 'health_error=config_directory_unreadable:%s\n' "$config_dir"
    exit 44
  fi
  printf 'config_directory=readable:%s\n' "$config_dir"
else
  printf 'config_directory=not_configured\n'
fi
"""
        checked_at = time.time()
        try:
            result = self._execute_native_command(
                state,
                ["/bin/sh", "-lc", script, "--", self.config.binary, config_directory],
                timeout=self.config.healthcheck_timeout,
            )
            detail = (result.stdout or result.stderr).strip()[-2_000:]
            healthy = result.exit_code == 0
            if not detail:
                detail = f"health command exited {result.exit_code}"
        except ModelInvocationCancelled:
            raise
        except Exception as exc:
            healthy = False
            detail = f"{type(exc).__name__}: {exc}"[-2_000:]
        ttl = self.config.healthcheck_cache_seconds if healthy else self.config.healthcheck_failure_cache_seconds
        return NativeTaskHealth(
            project_id=state.project_id,
            worker_name=self.config.worker_name,
            checked_at=checked_at,
            expires_at=checked_at + ttl,
            healthy=healthy,
            binary=self.config.binary,
            config_directory=config_directory,
            detail=detail,
        )

    @staticmethod
    def _resource_health_failure(detail: str) -> bool:
        normalized = str(detail).lower()
        return any(
            marker in normalized
            for marker in (
                "cannot fork",
                "resource temporarily unavailable",
                "health command exited 124",
                "container is not running",
                "container state improper",
            )
        )

    def _ensure_task_health(self, state: NativeSessionState) -> None:
        """Probe the bound project container once per short-lived cache window."""

        # Cairn validates the model endpoint in the Dispatcher and does not
        # fork a container-side ``binary --version`` process before each task.
        # Keep an explicit opt-in for diagnosing custom Worker images.
        if not self.config.container_preflight:
            state.health = {
                "checked_at": time.time(),
                "healthy": True,
                "cached": False,
                "skipped": True,
                "binary": self.config.binary,
                "config_directory": self._health_config_directory(),
                "detail": "Cairn task contract: container preflight deferred",
            }
            if self.config.model_healthcheck_mode == "startup_and_task":
                model_health = self._model_healthcheck()
                state.health["model"] = model_health
                if not bool(model_health.get("healthy")):
                    raise self._model_health_error(model_health)
            return

        if not callable(getattr(state.backend, "start_with_environment", None)):
            # Compatibility test doubles and out-of-tree backends retain the
            # old blocking method.  The persistent Docker runtime always
            # exposes the interruptible path and therefore always probes.
            state.health = {
                "checked_at": time.time(),
                "healthy": True,
                "cached": False,
                "binary": self.config.binary,
                "config_directory": self._health_config_directory(),
                "detail": "preflight deferred: backend has no interruptible execution handle",
            }
            return
        key = self._health_cache_key(state)
        health = self._cached_task_health(key)
        cached = health is not None
        if health is None:
            lock = self._health_lock_for(key)
            with lock:
                health = self._cached_task_health(key)
                cached = health is not None
                if health is None:
                    health = self._probe_task_health(state)
                    with self._health_lock:
                        self._health_cache[key] = health
        assert health is not None
        recovery: dict[str, Any] | None = None
        if not health.healthy and self._resource_health_failure(health.detail):
            recover = getattr(state.backend, "recover_resource_exhaustion", None)
            if callable(recover):
                try:
                    recovery = dict(recover(health.detail))
                    lock = self._health_lock_for(key)
                    with lock:
                        health = self._probe_task_health(state)
                        with self._health_lock:
                            self._health_cache[key] = health
                    cached = False
                except Exception as exc:
                    recovery = {
                        "recovered": False,
                        "action": "container_recovery_failed",
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
        state.health = self._health_payload(health, cached=cached)
        if recovery is not None:
            state.health["runtime_recovery"] = recovery
        if not health.healthy:
            recovery_error = str((recovery or {}).get("error", "")).strip()
            recovery_suffix = f"; recovery={recovery_error}" if recovery_error else ""
            raise ModelInvocationError(
                "native worker preflight failed "
                f"worker={self.config.worker_name} binary={health.binary} "
                f"config_directory={health.config_directory or '<not-configured>'}: "
                f"{health.detail}{recovery_suffix}"
            )
        if self.config.model_healthcheck_mode == "startup_and_task":
            model_health = self._model_healthcheck()
            state.health["model"] = model_health
            if not bool(model_health.get("healthy")):
                raise self._model_health_error(model_health)

    def _execute_native_command(
        self,
        state: NativeSessionState,
        argv: list[str],
        timeout: int | None = None,
    ) -> CommandExecution:
        """Run one CLI turn and retain a cancellation handle until it exits."""

        command_timeout = self.config.timeout if timeout is None else timeout
        pending_reason = self._cancel_reason_for(state)
        if pending_reason:
            raise ModelInvocationCancelled(
                f"native {self.config.adapter} cancelled before launch: {pending_reason}"
            )
        starter = getattr(state.backend, "start_with_environment", None)
        if not callable(starter):
            # Keeps lightweight test backends and older integrations usable.
            return state.backend.execute_with_environment(
                argv,
                state.workspace_root,
                command_timeout,
                self.config.environment,
            )

        try:
            command = starter(
                argv,
                state.workspace_root,
                self.config.environment,
                timeout_seconds=command_timeout,
                kill_after_seconds=5,
            )
        except TypeError:
            # Compatibility for older integrations and lightweight test
            # doubles that still expose Cairn's three-argument backend API.
            command = starter(argv, state.workspace_root, self.config.environment)
        self._register_active_command(state, command)
        try:
            # The container-side GNU timeout owns the phase deadline and has a
            # five-second TERM/KILL window. The host wait is only a wider
            # backstop, matching Cairn's timeout-plus-grace contract.
            result = command.communicate(command_timeout + 15)
            cancellation_reason = self._cancel_reason_for(state)
        finally:
            self._release_active_command_handle(state)
        if result.cancelled or cancellation_reason:
            detail = result.cancel_reason or cancellation_reason or "cancelled"
            raise ModelInvocationCancelled(
                f"native {self.config.adapter} cancelled: {detail}"
            )
        return result

    @staticmethod
    def _merge_usage(*usages: dict[str, int]) -> dict[str, int]:
        """Combine separate native CLI turns into one Worker-run usage record."""

        merged = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_calls": len(usages),
        }
        for usage in usages:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                merged[key] += int(usage.get(key, 0) or 0)
        if not merged["total_tokens"]:
            merged["total_tokens"] = merged["prompt_tokens"] + merged["completion_tokens"]
        return merged

    @staticmethod
    def _conclude_trigger(result: CommandExecution) -> str:
        if result.timed_out or result.exit_code == 124:
            return "execute_timeout"
        if result.exit_code != 0:
            return f"execute_exit_{result.exit_code}"
        return "execute_invalid_structured_report"

    def _conclude_prompt(
        self,
        context: dict[str, Any],
        state: NativeSessionState,
        trigger: str,
        initial_error: str,
    ) -> str:
        """Render the same-session bounded conclude prompt for this phase."""

        del state
        mode = str(context["mode"])
        if mode not in {"bootstrap", "explore"}:
            raise ValueError(f"native conclude is unsupported for mode={mode}")
        return render_prompt(
            load_prompt(self.config.prompt_group, f"{mode}_conclude.md"),
            {
                "trigger": trigger,
                "initial_error": initial_error[:1000],
                "context_json": json.dumps(context, ensure_ascii=False, indent=2),
            },
        )

    def _normalize_conclude_report(
        self,
        payload: dict[str, Any],
        task: WorkerTask,
        state: NativeSessionState,
    ) -> tuple[dict[str, Any], CompletionProposal | None]:
        if task.mode not in {"bootstrap", "explore"}:
            raise ValueError(f"native conclude is unsupported for mode={task.mode}")
        # Existing workspaces may contain the pre-Cairn conclude shape. Keep
        # those resumable while all new prompts use Cairn's compact contract.
        if "summary" in payload:
            allowed = {"summary", "fact", "completion"}
            unexpected = sorted(set(payload) - allowed)
            if unexpected:
                raise ValueError(f"native conclude returned unexpected keys: {unexpected}")
            summary = str(payload.get("summary", "")).strip()
            fact = payload.get("fact")
            if not summary or not isinstance(fact, dict):
                raise ValueError("native conclude requires summary and one fact")
            completion = payload.get("completion")
            if task.mode == "explore" and completion is not None:
                raise ValueError("Explore conclude must leave completion null")
            return self._normalize_legacy_report(
                {
                    "summary": summary,
                    "facts": [fact],
                    "hypotheses": [],
                    "proposed_intents": [],
                    "completion": completion,
                },
                task,
                state,
            )
        return self._normalize_cairn_report(payload, task, state, conclude=True)

    def _run_conclude_fallback(
        self,
        *,
        task: WorkerTask,
        context: dict[str, Any],
        state: NativeSessionState,
        initial_result: CommandExecution,
        initial_started_at: float,
        initial_finished_at: float,
        initial_usage: dict[str, int],
        initial_commands: list[str],
        initial_error: Exception,
        transcript_refs: list[str],
    ) -> tuple[dict[str, Any], CompletionProposal | None, dict[str, int], list[str], float]:
        """Resume one failed execute pass with a strict no-more-work conclude turn."""

        trigger = self._conclude_trigger(initial_result)
        initial_error_text = f"{type(initial_error).__name__}: {initial_error}"
        initial_transcript = self._write_transcript(
            state,
            initial_result,
            initial_started_at,
            initial_finished_at,
            initial_commands,
            error=initial_error_text,
            phase="execute",
        )
        transcript_refs.append(initial_transcript)
        state.turns += 1
        state.command_count += len(initial_commands)
        state.fallback = {
            "used": True,
            "trigger": trigger,
            "initial_error": initial_error_text[:2200],
            "initial_transcript": initial_transcript,
            "recovered": False,
        }
        self._write_manifest(state)
        self._raise_if_cancelled(state, "before conclude fallback")

        conclude_started_at = now()
        conclude_result: CommandExecution | None = None
        conclude_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        conclude_commands: list[str] = []
        conclude_timeout = self.config.timeout_for(task.mode, conclude=True)
        try:
            prompt = self._conclude_prompt(context, state, trigger, initial_error_text)
            argv = self._build_argv(state, prompt)
            conclude_result = self._execute_native_command(
                state,
                argv,
                timeout=conclude_timeout,
            )
            self._raise_if_cancelled(state, "after conclude execution")
            conclude_finished_at = now()
            _, conclude_usage, conclude_commands = self._extract_attempt_metadata(
                conclude_result.stdout
            )
            if conclude_result.cancelled:
                raise ModelInvocationCancelled(
                    f"native {self.config.adapter} conclude cancelled: "
                    f"{conclude_result.cancel_reason or 'cancelled'}"
                )
            if conclude_result.timed_out or conclude_result.exit_code == 124:
                raise ModelInvocationTimeout(
                    f"native {self.config.adapter} conclude timeout after {conclude_timeout}s"
                )
            if conclude_result.exit_code != 0:
                raise ModelInvocationError(
                    f"native {self.config.adapter} conclude exit={conclude_result.exit_code}: "
                    f"{(conclude_result.stderr or conclude_result.stdout)[-1500:]}"
                )
            response_text, conclude_usage, conclude_commands = self._extract_response(
                conclude_result.stdout,
                conclude_result.stderr,
            )
            payload = self._parse_json_object(response_text)
            normalized, completion = self._normalize_conclude_report(payload, task, state)
        except ModelInvocationCancelled:
            raise
        except Exception as exc:
            conclude_finished_at = now()
            conclude_error = f"{type(exc).__name__}: {exc}"
            failed_result = conclude_result or CommandExecution(127, "", conclude_error)
            conclude_transcript = self._write_transcript(
                state,
                failed_result,
                conclude_started_at,
                conclude_finished_at,
                conclude_commands,
                error=conclude_error,
                phase="conclude",
            )
            transcript_refs.append(conclude_transcript)
            state.turns += 1
            state.command_count += len(conclude_commands)
            state.report_usage = self._merge_usage(initial_usage, conclude_usage)
            state.status = "failed"
            if failed_result.timed_out or failed_result.exit_code != 0:
                self._mark_session_unusable(state)
            state.last_error = (
                f"{trigger}: {initial_error_text}; conclude: {conclude_error}"
            )[:2200]
            state.fallback.update(
                {
                    "conclude_transcript": conclude_transcript,
                    "conclude_error": conclude_error[:2200],
                    "recovered": False,
                }
            )
            self._write_manifest(state)
            raise NativeConcludeFallbackError(state.last_error) from exc

        conclude_transcript = self._write_transcript(
            state,
            conclude_result,
            conclude_started_at,
            conclude_finished_at,
            conclude_commands,
            phase="conclude",
        )
        transcript_refs.append(conclude_transcript)
        state.fallback.update(
            {
                "conclude_transcript": conclude_transcript,
                "recovered": True,
            }
        )
        return (
            normalized,
            completion,
            self._merge_usage(initial_usage, conclude_usage),
            conclude_commands,
            conclude_finished_at,
        )

    def run_cairn_task(
        self,
        intent: Intent,
        facts: list[Fact],
        mode: str,
        capsule: Any,
    ) -> PseudopodReport:
        """Execute one complete Cairn task without a model-to-tool relay loop.

        The container CLI owns its shell, files, long-running processes and
        local route for the duration of this task.  The Scheduler receives one
        final report and performs the only Blackboard write-back.
        """

        task = WorkerTask(
            mode=mode,
            objective=intent.objective,
            intent_id=intent.id,
            target_entity=intent.target_entity,
            relevant_fact_ids=list(capsule.manifest.included_fact_ids),
            goal=capsule.goal,
            scope=capsule.scope,
            environment_brief=capsule.environment_brief,
            previous_summary=capsule.previous_summary,
            branch_summaries=[asdict(item) for item in capsule.branches],
            context_manifest=asdict(capsule.manifest),
            hints=[dict(item) for item in capsule.hints],
            open_intents=[dict(item) for item in capsule.open_intents],
            initial_budget=0,
            max_steps=1,
        )
        started_at = now()
        state: NativeSessionState | None = None
        attempt_transcript_refs: list[str] = []
        try:
            self.begin_session(task, intent)
            state = self._state()
            state.fallback = {}
            self._ensure_task_health(state)
            self._raise_if_cancelled(state, "after preflight")
            context = self._context(task, intent, facts, state)
            self._write_json_atomic(state.directory / "context.json", context)
            prompt = self._prompt(context, state)
            argv = self._build_argv(state, prompt)
            if self.config.adapter == "codex-cli" and "-C" in argv:
                argv[argv.index("-C") + 1] = "/workspace"
            execute_timeout = self.config.timeout_for(mode)
            result = self._execute_native_command(state, argv, timeout=execute_timeout)
            self._raise_if_cancelled(state, "after CLI execution")
            initial_finished_at = now()
            _, initial_usage, initial_commands = self._extract_attempt_metadata(result.stdout)
            fallback_used = False
            try:
                if result.cancelled:
                    raise ModelInvocationCancelled(
                        f"native {self.config.adapter} cancelled: "
                        f"{result.cancel_reason or 'cancelled'}"
                    )
                if result.timed_out or result.exit_code == 124:
                    if self._failed_execute_session_unusable(result):
                        self._mark_session_unusable(state)
                    raise ModelInvocationTimeout(
                        f"native {self.config.adapter} timeout after {execute_timeout}s"
                    )
                if result.exit_code != 0:
                    if self._failed_execute_session_unusable(result):
                        self._mark_session_unusable(state)
                    raise ModelInvocationError(
                        f"native {self.config.adapter} exit={result.exit_code}: "
                        f"{(result.stderr or result.stdout)[-1500:]}"
                    )
                response_text, usage, commands = self._extract_response(result.stdout, result.stderr)
                payload = self._parse_json_object(response_text)
                normalized, completion = self._normalize_report(payload, task, state)
                finished_at = initial_finished_at
            except ModelInvocationCancelled:
                raise
            except (
                ModelInvocationError,
                ValueError,
                RuntimeError,
                TypeError,
                KeyError,
                PermissionError,
            ) as initial_error:
                command_failed = isinstance(initial_error, ModelInvocationError) and not isinstance(
                    initial_error, ModelInvocationTimeout
                )
                if task.mode == "reason" or command_failed:
                    # Cairn runs Reason once. It deliberately does not resume
                    # a failed planning turn with a conclude pass. Bootstrap
                    # and Explore also release immediately on non-zero command
                    # exits; conclude is reserved for timeout/parse fallback.
                    self._mark_session_unusable(state)
                    raise
                # Resume the same native session for Cairn's bounded conclude
                # pass whenever the CLI supplied a session ID.  Only explicit
                # missing-session diagnostics make that second call futile.
                if not state.resume_session or self._failed_execute_session_unusable(result):
                    self._mark_session_unusable(state)
                    raise
                (
                    normalized,
                    completion,
                    usage,
                    commands,
                    finished_at,
                ) = self._run_conclude_fallback(
                    task=task,
                    context=context,
                    state=state,
                    initial_result=result,
                    initial_started_at=started_at,
                    initial_finished_at=initial_finished_at,
                    initial_usage=initial_usage,
                    initial_commands=initial_commands,
                    initial_error=initial_error,
                    transcript_refs=attempt_transcript_refs,
                )
                fallback_used = True
            self._raise_if_cancelled(state, "before report import")
            if not fallback_used:
                transcript_ref = self._write_transcript(
                    state,
                    result,
                    started_at,
                    finished_at,
                    commands,
                    phase="execute",
                )
                attempt_transcript_refs.append(transcript_ref)
            state.turns += 1
            state.command_count += len(commands)
            state.status = "active"
            state.last_error = ""
            state.report_usage = (
                dict(usage) if fallback_used else self._merge_usage(usage)
            )
            result_path = state.directory / "results" / f"turn-{state.turns:03d}.json"
            self._write_json_atomic(result_path, normalized)
            result_ref = result_path.relative_to(state.workspace_root).as_posix()
            evidence_refs = sorted(
                set([*attempt_transcript_refs, result_ref, *normalized.get("evidence_refs", [])])
            )
            candidates = [
                FactCandidate(
                    subject=str(item["subject"]),
                    predicate=str(item["predicate"]),
                    object=str(item["object"]),
                    confidence=float(item["confidence"]),
                    evidence_refs=sorted(
                        set([*attempt_transcript_refs, *item.get("evidence_refs", [])])
                    ),
                    source_intent_id=intent.id,
                    attributes=dict(item.get("attributes") or {}),
                )
                for item in normalized["facts"]
            ]
            hypotheses = [
                HypothesisCandidate(
                    statement=str(item["statement"]),
                    supporting_fact_ids=[str(value) for value in item.get("supporting_fact_ids", [])],
                    evidence_refs=list(item.get("evidence_refs", [])),
                    confidence=float(item["confidence"]),
                    next_validation=str(item.get("next_validation", "")),
                    source_intent_id=intent.id,
                )
                for item in normalized["hypotheses"]
            ]
            proposals = [
                IntentProposal(
                    kind=str(item["kind"]),
                    objective=str(item["objective"]),
                    target_entity=str(item["target_entity"]),
                    parent_fact_ids=[str(value) for value in item.get("parent_fact_ids", [])],
                    expected_value=float(item["expected_value"]),
                    novelty=float(item["novelty"]),
                    cost=float(item["cost"]),
                    risk=float(item["risk"]),
                    context=dict(item.get("context") or {}),
                    provenance=dict(item.get("provenance") or {}),
                )
                for item in normalized["proposed_intents"]
            ]
            outcome = str(normalized.get("outcome", "legacy"))
            if outcome == "rejected":
                status = "rejected"
            elif outcome == "noop" or candidates or proposals or completion is not None:
                status = "completed"
            else:
                status = "dormant"
            summary = str(normalized.get("summary", ""))[:1000]
            self._write_manifest(state)
            return PseudopodReport(
                pseudopod_id=new_id("pod"),
                intent_id=intent.id,
                mode=mode,
                status=status,
                candidate_facts=candidates,
                evidence_refs=evidence_refs,
                proposed_intents=proposals,
                tool_calls=0,
                progress_score=float(
                    len(candidates) * 10
                    + len(hypotheses) * 3
                    + len(proposals) * 5
                    + int(completion is not None) * 10
                ),
                stop_reason=(
                    "native_agent_rejected"
                    if outcome == "rejected"
                    else "native_agent_noop"
                    if outcome == "noop"
                    else "native_agent_conclude_fallback"
                    if fallback_used
                    else "native_agent_task_complete"
                ),
                remaining_budget=0,
                activity_summary=(
                    [
                        f"conclude fallback after {state.fallback.get('trigger', 'execute_failure')}",
                        summary,
                    ]
                    if fallback_used
                    else [summary]
                ),
                context_manifest=asdict(capsule.manifest),
                candidate_hypotheses=hypotheses,
                errors=([summary] if outcome == "rejected" else []),
                model_usage=dict(state.report_usage),
                model_session=self.session_info(),
                completion=completion,
                started_at=started_at,
                finished_at=finished_at,
            )
        except ModelInvocationCancelled as exc:
            finished_at = now()
            if state is not None:
                state.last_error = f"{type(exc).__name__}: {exc}"[:2200]
                state.status = "interrupted"
                cancellation_transcript = self._write_transcript(
                    state,
                    locals().get("result", CommandExecution(130, "", state.last_error, cancelled=True)),
                    started_at,
                    finished_at,
                    [],
                    error=state.last_error,
                    phase="conclude" if state.fallback.get("used") else "execute",
                )
                attempt_transcript_refs.append(cancellation_transcript)
                self._write_manifest(state)
            # Scheduler owns the WorkerRun and Intent transitions.  Raising
            # prevents a cancelled CLI from importing late Facts after its
            # lease has been released by the Dispatcher.
            raise
        except Exception as exc:
            finished_at = now()
            error = f"{type(exc).__name__}: {exc}"
            failure_transcript_refs = list(attempt_transcript_refs)
            if state is not None:
                state.last_error = error[:2200]
                state.status = "failed"
                if not isinstance(exc, NativeConcludeFallbackError):
                    failure_transcript = self._write_transcript(
                        state,
                        locals().get("result", CommandExecution(127, "", error)),
                        started_at,
                        finished_at,
                        [],
                        error=error,
                        phase="execute",
                    )
                    failure_transcript_refs.append(failure_transcript)
                self._write_manifest(state)
            return PseudopodReport(
                pseudopod_id=new_id("pod"),
                intent_id=intent.id,
                mode=mode,
                status="failed",
                candidate_facts=[],
                evidence_refs=sorted(set(failure_transcript_refs)),
                proposed_intents=[],
                tool_calls=0,
                progress_score=0.0,
                stop_reason="native_agent_error",
                remaining_budget=0,
                activity_summary=[error[:1000]],
                context_manifest=asdict(capsule.manifest),
                errors=[error],
                model_usage=(dict(state.report_usage) if state is not None else {}),
                model_session=(self.session_info() if state is not None else {}),
                started_at=started_at,
                finished_at=finished_at,
            )
        finally:
            if state is not None:
                try:
                    self._unregister_active_command(state)
                    self.end_session("completed" if state.status == "active" else state.status)
                except Exception:
                    pass


    def _context(
        self,
        task: WorkerTask,
        intent: Intent,
        facts: list[Fact],
        state: NativeSessionState,
    ) -> dict[str, Any]:
        graph_ref = str(task.context_manifest.get("graph_snapshot_ref", "")).strip()
        return {
            "mode": task.mode,
            "goal": task.goal,
            "origin": str(task.scope.get("origin") or task.target_entity),
            "intent": {
                "id": intent.id,
                "kind": intent.kind,
                "objective": task.objective,
                "target_entity": task.target_entity,
                "parent_fact_ids": list(intent.parent_fact_ids),
                "context": dict(intent.context),
            },
            "scope": dict(task.scope),
            "hints": list(task.hints),
            "valid_fact_ids": list(task.relevant_fact_ids),
            "open_intents": [dict(item) for item in task.open_intents],
            "context_manifest": dict(task.context_manifest),
            "branch_checkpoint": dict(task.context_manifest.get("branch_checkpoint") or {}),
            "graph_file": f"/workspace/{graph_ref}" if graph_ref else "",
            "environment": {
                "pod_directory": state.container_directory,
                "shared_directory": "/workspace/shared",
                "evidence_directory": f"{state.container_directory}/evidence",
                "tools": "Use the Kali shell and inspect /opt/slime-cairn/tools.json on demand.",
                "pocs": "/opt/slime-cairn/pocs",
                "knowledge": "/opt/slime-cairn/knowledges",
                "resource_namespace": f"slime-{state.directory.name}",
                "reserved_port_range": list(state.port_range),
            },
        }

    def _prompt(self, context: dict[str, Any], state: NativeSessionState) -> str:
        del state
        graph_file = str(context.get("graph_file", "")).strip()
        graph_reference = (
            "The graph YAML snapshot is stored in this file inside the current container:\n\n"
            f"{graph_file}\n\n"
            "Before using the graph, read the entire file and treat its contents as the YAML "
            "snapshot for this Graph section."
            if graph_file
            else "Read the current task descriptor from context.json."
        )
        mode = str(context["mode"])
        if mode == "bootstrap":
            return render_prompt(
                load_prompt(self.config.prompt_group, "bootstrap.md"),
                {
                    "origin": context.get("origin", ""),
                    "goal": context.get("goal", ""),
                    "hints": json.dumps(context.get("hints", []), ensure_ascii=False, indent=2),
                },
            )
        if mode == "explore":
            intent = context["intent"]
            benchmark = context.get("scope", {}).get("benchmark")
            benchmark_managed = isinstance(benchmark, dict) and benchmark.get("managed") is True
            explore_shape = (
                '{"accepted":true,"data":{"description":"...",'
                '"submissions":["exact candidate"]}}'
                if benchmark_managed
                else '{"accepted":true,"data":{"description":"..."}}'
            )
            benchmark_rule = (
                "- For a managed Benchmark, put every exact candidate in `data.submissions` using "
                "the original value returned by the target. Candidates have no required prefix, wrapper, "
                "or syntax. Clearly label any candidate repeated in the description.\n"
                if benchmark_managed
                else ""
            )
            return render_prompt(
                load_prompt(self.config.prompt_group, "explore.md"),
                {
                    "graph_yaml": graph_reference,
                    "intent_id": intent["id"],
                    "intent_description": intent["objective"],
                    "explore_shape": explore_shape,
                    "benchmark_rule": benchmark_rule,
                    "branch_checkpoint": json.dumps(
                        context.get("branch_checkpoint", {}), ensure_ascii=False, indent=2
                    ),
                },
            )
        if mode != "reason":
            raise ValueError(f"native-agent unsupported task mode: {mode}")

        benchmark = context.get("scope", {}).get("benchmark")
        benchmark_managed = isinstance(benchmark, dict) and benchmark.get("managed") is True
        complete_shape = (
            '{"accepted":true,"data":{"complete":{"from":["fact_id"],'
            '"description":"...","submissions":["exact candidate"]}}}'
            if benchmark_managed
            else '{"accepted":true,"data":{"complete":{"from":["fact_id"],"description":"..."}}}'
        )
        benchmark_rule = (
            "- For a managed Benchmark, put every exact candidate in `complete.submissions` using "
            "the original value returned by the target. Candidates have no required prefix, wrapper, "
            "or syntax. Clearly label any candidate repeated in the completion description.\n"
            if benchmark_managed
            else ""
        )
        return render_prompt(
            load_prompt(self.config.prompt_group, "reason.md"),
            {
                "graph_yaml": graph_reference,
                "fact_ids": json.dumps(
                    context.get("valid_fact_ids", []), ensure_ascii=False, indent=2
                ),
                "open_intents": json.dumps(
                    context.get("open_intents", []), ensure_ascii=False, indent=2
                ),
                "max_intents": self.config.reason_max_intents,
                "complete_shape": complete_shape,
                "benchmark_rule": benchmark_rule,
            },
        )

    def _build_argv(self, state: NativeSessionState, prompt: str) -> list[str]:
        adapter = self.config.adapter
        if adapter == "claude-code":
            claude_argv = [
                self.config.binary,
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--output-format",
                "stream-json",
                "--verbose",
                "--print",
            ]
            if self.config.model:
                claude_argv.extend(["--model", self.config.model])
            if state.resume_session:
                claude_argv.extend(["--resume", state.session_id])
            else:
                claude_argv.extend(["--session-id", state.session_id])
            prompt_file = self._write_prompt_file(state, prompt)
            # Cairn passes prompts through the Docker API. On Windows this
            # runtime reaches the same container through docker.exe, whose
            # command line is bounded, so Claude reads the mounted prompt from
            # stdin instead of receiving the complete graph as one argv item.
            script = 'prompt_file="$1"\nshift\nexec "$@" < "$prompt_file"\n'
            return [
                "/bin/sh",
                "-lc",
                script,
                "--",
                prompt_file,
                *claude_argv,
            ]
        if adapter == "codex-cli":
            common = [
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-c",
                "features.plugins=false",
                "-c",
                "features.remote_plugin=false",
                "-c",
                "features.plugin_sharing=false",
            ]
            # Native Workers use the container shell and Kali tools directly.
            # Plugin marketplace sync adds network-bound startup work but does
            # not participate in the Cairn task contract. Explicit Worker
            # overrides are appended later and can opt back in when needed.
            for key, value in self.config.config_overrides:
                common.extend(["-c", f"{key}={self._toml_literal(value)}"])
            if self.config.model:
                common.extend(["--model", self.config.model])
            prompt_file = self._write_prompt_file(state, prompt)
            if state.resume_session:
                codex_argv = [
                    self.config.binary,
                    "exec",
                    "resume",
                    *common,
                    state.session_id,
                ]
            else:
                codex_argv = [
                    self.config.binary,
                    "exec",
                    *common,
                    "-C",
                    state.container_directory,
                ]
            # Cairn sends container exec requests through the Docker API, which
            # bypasses the host command-line limit. This runtime uses docker.exe
            # on Windows, so feed the prompt from the mounted workspace instead.
            script = 'prompt_file="$1"\nshift\nexec "$@" - < "$prompt_file"\n'
            return [
                "/bin/sh",
                "-lc",
                script,
                "--",
                prompt_file,
                *codex_argv,
            ]

        prompt_file = self._write_prompt_file(state, prompt)
        argv = [
            self.config.binary,
            "--mode",
            "json",
            "--print",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--tools",
            "read,write,edit,bash,grep,find,ls",
            "--session-dir",
            f"{state.container_directory}/sessions",
        ]
        if self.config.provider:
            argv.extend(["--provider", self.config.provider])
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.thinking:
            argv.extend(["--thinking", self.config.thinking])
        if state.resume_session:
            argv.extend(["--session", state.session_id])
        # Pi expands @file arguments itself. Keeping only the mounted path in
        # docker.exe argv avoids the Windows command-line size boundary while
        # preserving the complete task prompt inside the project workspace.
        argv.extend(["-p", f"@{prompt_file}"])
        if self.config.model_endpoint is not None:
            return self._wrap_explicit_pi_provider(state, argv)
        return argv

    @staticmethod
    def _wrap_explicit_pi_provider(state: NativeSessionState, argv: list[str]) -> list[str]:
        """Create Pi's provider file inside the persistent container, never on the host.

        Pi reads custom providers from ``models.json``.  The script obtains all
        credential material from Docker's short-lived env file, so the key is
        absent from argv, transcripts, the host workspace, and the Blackboard.
        """

        namespace = sha256(state.resource_token.encode("utf-8")).hexdigest()[:24]
        agent_dir = f"/tmp/slime-cairn-pi/{namespace}"
        script = r'''agent_dir="$1"
shift
mkdir -p "$agent_dir" "$agent_dir/sessions"
python3 - "$agent_dir/models.json" <<'PY'
import json
import os
import sys

model = os.environ["SLIME_PI_MODEL"]
entry = {"id": model, "name": model}
context_window = os.environ.get("PI_MODEL_CONTEXT_WINDOW", "").strip()
if context_window:
    entry["contextWindow"] = int(context_window)
payload = {
    "providers": {
        "slime_cairn": {
            "baseUrl": os.environ["SLIME_PI_BASE_URL"],
            "api": os.environ["SLIME_PI_PROVIDER_API"],
            "apiKey": os.environ["PI_API_KEY"],
            "models": [entry],
        }
    }
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
PY
exec env PI_CODING_AGENT_DIR="$agent_dir" "$@"
'''
        return ["/bin/sh", "-lc", script, "--", agent_dir, *argv]

    @staticmethod
    def _toml_literal(value: Any) -> str:
        """Render a JSON config value as a small TOML literal for CLI -c flags."""

        if isinstance(value, dict) and set(value) == {"toml"}:
            return str(value["toml"])
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return '""'
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _json_lines(text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _assistant_text(message: Any) -> str:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = str(key).lower()
                    if isinstance(child, (int, float)):
                        if normalized in {"input", "input_tokens", "prompt_tokens"}:
                            totals["prompt_tokens"] = max(totals["prompt_tokens"], int(child))
                        elif normalized in {"output", "output_tokens", "completion_tokens"}:
                            totals["completion_tokens"] = max(totals["completion_tokens"], int(child))
                        elif normalized in {"totaltokens", "total_tokens"}:
                            totals["total_tokens"] = max(totals["total_tokens"], int(child))
                    else:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return totals

    def _extract_attempt_metadata(
        self,
        stdout: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
        """Extract session/usage/tool metadata even when no final report exists."""

        events = self._json_lines(stdout)
        commands: list[str] = []
        state = self._state()

        def record_session_id(value: Any) -> None:
            session_id = str(value or "").strip()
            if session_id:
                state.session_id = session_id
                state.resume_session = True

        if self.config.adapter == "claude-code":
            try:
                direct = json.loads(stdout)
            except json.JSONDecodeError:
                direct = None
            if isinstance(direct, dict):
                record_session_id(direct.get("session_id"))

        for event in events:
            # Codex JSONL announces a usable remote thread as
            # {"type":"thread.started","thread_id":"..."}; it does not
            # use the generic session_id field.  Missing this event leaves the
            # random local UUID in the manifest and breaks every resume.
            session_id = event.get("session_id") or (
                event.get("thread_id") if event.get("type") == "thread.started" else None
            ) or (event.get("id") if event.get("type") == "session" else None)
            record_session_id(session_id)
            commands.extend(self._event_commands(event))
        return events, self._usage(events or stdout), commands[:200]

    def _extract_response(
        self,
        stdout: str,
        stderr: str = "",
    ) -> tuple[str, dict[str, int], list[str]]:
        events, usage, commands = self._extract_attempt_metadata(stdout)
        messages: list[str] = []

        if self.config.adapter == "claude-code":
            try:
                direct = json.loads(stdout)
            except json.JSONDecodeError:
                direct = None
            if isinstance(direct, dict) and isinstance(direct.get("result"), str):
                messages.append(direct["result"])

        for event in events:
            if isinstance(event.get("result"), str):
                messages.append(event["result"])
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message"}:
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    messages.append(text)
            text = self._assistant_text(event.get("message"))
            if text:
                messages.append(text)
            for message in event.get("messages") or []:
                text = self._assistant_text(message)
                if text:
                    messages.append(text)

        if not messages:
            stripped = stdout.strip()
            try:
                raw_report = json.loads(stripped)
            except json.JSONDecodeError:
                raw_report = None
            if isinstance(raw_report, dict) and any(
                key in raw_report
                for key in (
                    "accepted",
                    "data",
                    "description",
                    "fact",
                    "complete",
                    "intent",
                    "intents",
                    "summary",
                    "facts",
                    "hypotheses",
                    "proposed_intents",
                    "completion",
                )
            ):
                messages.append(stripped)
        if not messages:
            diagnostics: list[str] = []
            for event in events:
                subtype = str(event.get("subtype", "")).strip()
                error = str(event.get("error", "")).strip()
                if subtype and error:
                    diagnostics.append(f"{subtype}: {error}")
                elif error:
                    diagnostics.append(error)
            if stderr.strip():
                diagnostics.append(f"stderr: {stderr.strip()[-1000:]}")
            detail = "; ".join(dict.fromkeys(diagnostics)) or "no assistant event"
            raise RuntimeError(f"native {self.config.adapter} 输出中没有 assistant 结果: {detail}")
        return messages[-1], usage, commands

    @staticmethod
    def _event_commands(event: dict[str, Any]) -> list[str]:
        found: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                kind = str(value.get("type", "")).lower()
                name = str(value.get("name", "")).lower()
                if kind in {"command_execution", "shell_command", "command"} or name in {
                    "bash",
                    "shell",
                    "exec_command",
                }:
                    candidate = value.get("command") or value.get("argv") or value.get("input")
                    if isinstance(candidate, str):
                        found.append(candidate[:2000])
                    elif isinstance(candidate, list):
                        found.append(" ".join(str(item) for item in candidate)[:2000])
                    elif isinstance(candidate, dict):
                        command = candidate.get("command") or candidate.get("cmd")
                        if command:
                            found.append(str(command)[:2000])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(event)
        return found

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        try:
            return parse_json_output(text)
        except ValueError as exc:
            raise ValueError("native-agent 最终结果不是 JSON object") from exc

    def _normalize_report(
        self,
        payload: dict[str, Any],
        task: WorkerTask,
        state: NativeSessionState,
    ) -> tuple[dict[str, Any], CompletionProposal | None]:
        """Normalize a Cairn result, with a compatibility path for old runs."""

        legacy_keys = {"summary", "facts", "hypotheses", "proposed_intents", "completion"}
        if set(payload) & legacy_keys:
            return self._normalize_legacy_report(payload, task, state)
        return self._normalize_cairn_report(payload, task, state, conclude=False)

    @staticmethod
    def _validated_cairn_from(value: Any, task: WorkerTask, *, label: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{label}.from 必须是非空 Fact ID 数组")
        fact_ids = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not fact_ids:
            raise ValueError(f"{label}.from 必须是非空 Fact ID 数组")
        unknown = sorted(set(fact_ids) - set(task.relevant_fact_ids))
        if unknown:
            raise ValueError(f"{label}.from 引用了上下文外 Fact: {unknown}")
        return fact_ids

    def _cairn_normalized(
        self,
        state: NativeSessionState,
        *,
        summary: str,
        outcome: str,
        facts: list[dict[str, Any]] | None = None,
        intents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "summary": summary.strip()[:2000],
            "outcome": outcome,
            "contract": "cairn",
            "pod_path": state.directory.relative_to(state.workspace_root).as_posix(),
            "facts": list(facts or []),
            "hypotheses": [],
            "proposed_intents": list(intents or []),
            "evidence_refs": [],
        }

    def _cairn_fact(
        self,
        description: str,
        task: WorkerTask,
    ) -> dict[str, Any]:
        return {
            "subject": task.target_entity,
            "predicate": "cairn_fact",
            "object": description,
            "confidence": 0.9,
            "evidence_refs": [],
            "attributes": {
                **SlimeGrowthLayer.fact_attributes(
                    mode=task.mode,
                    intent_id=task.intent_id,
                    description=description,
                ),
                "native_agent": self.config.worker_name,
            },
        }

    def _normalize_cairn_report(
        self,
        payload: dict[str, Any],
        task: WorkerTask,
        state: NativeSessionState,
        *,
        conclude: bool,
    ) -> tuple[dict[str, Any], CompletionProposal | None]:
        """Map Cairn's compact wire protocol into the internal report model."""

        rejected_summary = str(payload.get("reason", "")).strip() or "Cairn worker rejected the task"
        if task.mode == "bootstrap":
            if conclude:
                kind, description = validate_bootstrap_conclude_payload(payload)
                if kind == "rejected":
                    return self._cairn_normalized(
                        state, summary=rejected_summary, outcome="rejected"
                    ), None
                assert isinstance(description, str)
                fact = self._cairn_fact(description, task)
                return self._cairn_normalized(
                    state,
                    summary=description,
                    outcome="fact",
                    facts=[fact],
                ), None

            kind, data = validate_bootstrap_execute_payload(payload)
            if kind == "rejected":
                return self._cairn_normalized(
                    state, summary=rejected_summary, outcome="rejected"
                ), None
            assert isinstance(data, dict)
            fact_description = data["fact_description"]
            complete_description = data["complete_description"]
            fact = self._cairn_fact(fact_description, task)
            return self._cairn_normalized(
                state,
                summary=fact_description,
                outcome="complete",
                facts=[fact],
            ), CompletionProposal([], complete_description, [0])

        if task.mode == "explore":
            kind, description = validate_explore_payload(payload)
            if kind == "rejected":
                return self._cairn_normalized(
                    state, summary=rejected_summary, outcome="rejected"
                ), None
            assert isinstance(description, str)
            submissions = extract_explore_submissions(payload)
            benchmark = task.scope.get("benchmark")
            if isinstance(benchmark, dict) and benchmark.get("managed") is True and not submissions:
                submissions = extract_submission_candidates(description)
            benchmark_managed = isinstance(benchmark, dict) and benchmark.get("managed") is True
            if submissions and not benchmark_managed:
                raise ValueError("Explore submissions are only valid for managed Benchmark tasks")
            fact = self._cairn_fact(description, task)
            if submissions:
                fact["attributes"]["benchmark_submissions"] = submissions
            return self._cairn_normalized(
                state,
                summary=description,
                outcome="fact",
                facts=[fact],
            ), None

        if task.mode != "reason" or conclude:
            raise ValueError(f"native Cairn result unsupported for mode={task.mode}")

        kind, data = validate_reason_payload(
            payload,
            open_intents_empty=not bool(task.open_intents),
            max_intents=min(self.config.max_report_items, self.config.reason_max_intents),
            known_fact_ids=set(task.relevant_fact_ids),
        )
        if kind == "rejected":
            return self._cairn_normalized(
                state, summary=rejected_summary, outcome="rejected"
            ), None
        if kind == "noop":
            return self._cairn_normalized(
                state,
                summary="Existing open Intents already cover the current valuable directions",
                outcome="noop",
            ), None
        if kind == "complete":
            assert isinstance(data, dict)
            description = str(data.get("description", "")).strip()
            fact_ids = self._validated_cairn_from(data.get("from"), task, label="complete")
            submissions = [str(item).strip() for item in data.get("submissions", [])]
            benchmark = task.scope.get("benchmark")
            if isinstance(benchmark, dict) and benchmark.get("managed") is True and not submissions:
                submissions = extract_submission_candidates(description)
            if isinstance(benchmark, dict) and benchmark.get("managed") is True and not submissions:
                raise ValueError("Benchmark Reason completion requires a concrete flag candidate")
            return self._cairn_normalized(
                state,
                summary=description,
                outcome="complete",
            ), CompletionProposal(fact_ids, description, submissions=submissions)

        assert kind == "intents" and isinstance(data, list)
        intents: list[dict[str, Any]] = []
        for index, raw in enumerate(data):
            description = str(raw.get("description", "")).strip()
            if not description:
                raise ValueError(f"intent[{index}].description 不能为空")
            fact_ids = self._validated_cairn_from(
                raw.get("from"), task, label=f"intent[{index}]"
            )
            intents.append(
                {
                    "kind": "explore",
                    "objective": description,
                    "target_entity": task.target_entity,
                    "parent_fact_ids": fact_ids,
                    "expected_value": 0.5,
                    "novelty": 0.5,
                    "cost": 0.2,
                    "risk": 0.0,
                    "context": {},
                    "provenance": {
                        "native_agent": self.config.worker_name,
                        "cairn": {"from": fact_ids, "description": description},
                    },
                }
            )
        return self._cairn_normalized(
            state,
            summary=f"Reason proposed {len(intents)} Cairn Intent(s)",
            outcome="intents",
            intents=intents,
        ), None

    def _normalize_legacy_report(
        self,
        payload: dict[str, Any],
        task: WorkerTask,
        state: NativeSessionState,
    ) -> tuple[dict[str, Any], CompletionProposal | None]:
        limit = self.config.max_report_items
        summary = str(payload.get("summary", "")).strip()[:2000]
        if not summary:
            raise ValueError("native-agent 结果缺少 summary")
        raw_facts = payload.get("facts") or []
        raw_hypotheses = payload.get("hypotheses") or []
        raw_intents = payload.get("proposed_intents") or []
        if not all(isinstance(value, list) for value in (raw_facts, raw_hypotheses, raw_intents)):
            raise ValueError("facts/hypotheses/proposed_intents 必须是数组")
        if task.mode == "reason" and (raw_facts or raw_hypotheses):
            raise ValueError("Reason 只能融合现有黑板，不能直接提交新 Fact/Hypothesis")
        if task.mode != "reason" and raw_intents:
            raise ValueError(f"{task.mode.title()} 只能返回发现；只有 Reason 能创建 Intent")

        evidence_refs: list[str] = []

        def evidence_files(item: dict[str, Any]) -> list[str]:
            refs: list[str] = []
            for raw in list(item.get("evidence_files") or [])[:limit]:
                relative = Path(str(raw))
                candidate = (state.directory / relative).resolve()
                try:
                    candidate.relative_to(state.directory)
                except ValueError as exc:
                    raise PermissionError(f"native evidence 越出伪足目录: {raw}") from exc
                if not candidate.exists():
                    raise ValueError(f"native evidence 文件不存在: {raw}")
                ref = candidate.relative_to(state.workspace_root).as_posix()
                refs.append(ref)
                evidence_refs.append(ref)
            return sorted(set(refs))

        facts: list[dict[str, Any]] = []
        for raw in raw_facts[:limit]:
            if not isinstance(raw, dict):
                raise ValueError("native Fact 必须是 object")
            facts.append(
                {
                    "subject": str(raw.get("subject", "")).strip(),
                    "predicate": str(raw.get("predicate", "")).strip(),
                    "object": str(raw.get("object", "")).strip(),
                    "confidence": float(raw.get("confidence", 0.7)),
                    "evidence_refs": evidence_files(raw),
                    "attributes": {
                        **dict(raw.get("attributes") or {}),
                        "native_agent": self.config.worker_name,
                    },
                }
            )
        hypotheses: list[dict[str, Any]] = []
        for raw in raw_hypotheses[:limit]:
            if not isinstance(raw, dict):
                raise ValueError("native Hypothesis 必须是 object")
            hypotheses.append(
                {
                    "statement": str(raw.get("statement", "")).strip(),
                    "supporting_fact_ids": [str(item) for item in raw.get("supporting_fact_ids", [])],
                    "evidence_refs": evidence_files(raw),
                    "confidence": float(raw.get("confidence", 0.5)),
                    "next_validation": str(raw.get("next_validation", "")),
                }
            )
        intents: list[dict[str, Any]] = []
        intent_limit = (
            min(limit, self.config.reason_max_intents)
            if task.mode == "reason"
            else limit
        )
        # Cairn admits only the configured first N Reason directions from a
        # planning response, even when the model returned a longer list.
        for raw in raw_intents[:intent_limit]:
            if not isinstance(raw, dict) or not str(raw.get("objective", "")).strip():
                raise ValueError("native Intent 必须包含 objective")
            context = dict(raw.get("context") or {})
            scores: dict[str, float] = {}
            score_notes: dict[str, str] = {}
            for field, default in (("expected_value", 0.5), ("novelty", 0.5), ("cost", 0.2), ("risk", 0.0)):
                score, note = coerce_unit_score(raw.get(field, default), default)
                scores[field] = score
                if note:
                    score_notes[field] = note
            if score_notes:
                existing_notes = context.get("score_notes")
                context["score_notes"] = {
                    **(dict(existing_notes) if isinstance(existing_notes, dict) else {}),
                    **score_notes,
                }
            intents.append(
                {
                    "kind": str(raw.get("kind", "explore")),
                    "objective": str(raw["objective"]),
                    "target_entity": str(raw.get("target_entity", task.target_entity)),
                    "parent_fact_ids": [
                        str(item) for item in raw.get("parent_fact_ids", task.relevant_fact_ids)
                    ],
                    "expected_value": scores["expected_value"],
                    "novelty": scores["novelty"],
                    "cost": scores["cost"],
                    "risk": scores["risk"],
                    "context": context,
                    "provenance": {
                        **dict(raw.get("provenance") or {}),
                        "native_agent": self.config.worker_name,
                    },
                }
            )

        completion = self._completion(payload.get("completion"), task, len(facts))
        if completion is not None and intents:
            raise ValueError("native completion 与 proposed_intents 不能同时出现")
        return (
            {
                "summary": summary,
                "pod_path": state.directory.relative_to(state.workspace_root).as_posix(),
                "facts": facts,
                "hypotheses": hypotheses,
                "proposed_intents": intents,
                "evidence_refs": sorted(set(evidence_refs)),
            },
            completion,
        )

    @staticmethod
    def _completion(
        value: Any,
        task: WorkerTask,
        candidate_fact_count: int = 0,
    ) -> CompletionProposal | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("native completion 必须是 object")
        description = str(value.get("description", "")).strip()
        if not description:
            raise ValueError("native completion 需要 description")
        if task.mode == "reason":
            raw_ids = value.get("fact_ids")
            if not isinstance(raw_ids, list) or not raw_ids:
                raise ValueError("Reason completion 需要 fact_ids")
            fact_ids = list(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
            unknown = sorted(set(fact_ids) - set(task.relevant_fact_ids))
            if unknown:
                raise ValueError(f"native completion 引用了上下文外 Fact: {unknown}")
            raw_submissions = value.get("submissions", [])
            if not isinstance(raw_submissions, list):
                raise ValueError("Reason completion.submissions 必须是 array")
            submissions = list(
                dict.fromkeys(str(item).strip() for item in raw_submissions if str(item).strip())
            )
            benchmark = task.scope.get("benchmark")
            if isinstance(benchmark, dict) and benchmark.get("managed") is True and not submissions:
                submissions = extract_submission_candidates(description)
            if isinstance(benchmark, dict) and benchmark.get("managed") is True and not submissions:
                raise ValueError("Benchmark Reason completion 需要 submissions")
            return CompletionProposal(fact_ids, description, submissions=submissions)
        if task.mode != "bootstrap":
            raise ValueError("Explore 不支持 native completion")

        raw_indexes = value.get("fact_indexes")
        if raw_indexes is None:
            # Cairn's Bootstrap contract has one Fact plus one Complete. Keep
            # that compact form usable while requiring explicit selection when
            # a Worker reports more than one Fact.
            if candidate_fact_count != 1:
                raise ValueError("Bootstrap completion 需要 fact_indexes")
            indexes = [0]
        else:
            if not isinstance(raw_indexes, list) or not raw_indexes:
                raise ValueError("Bootstrap completion 需要非空 fact_indexes")
            indexes = []
            for raw_index in raw_indexes:
                if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                    raise ValueError("Bootstrap completion.fact_indexes 必须是整数")
                if raw_index < 0 or raw_index >= candidate_fact_count:
                    raise ValueError(
                        f"Bootstrap completion 引用了不存在的 Fact 下标: {raw_index}"
                    )
                if raw_index not in indexes:
                    indexes.append(raw_index)
        return CompletionProposal([], description, indexes)

    def _write_transcript(
        self,
        state: NativeSessionState,
        result: CommandExecution,
        started_at: float,
        finished_at: float,
        commands: list[str],
        error: str = "",
        phase: str = "execute",
    ) -> str:
        turn = state.turns + 1
        transcript = state.directory / "transcripts" / f"turn-{turn:03d}.json"
        limit = self.config.max_transcript_chars
        payload = {
            "execution": self.execution_mode,
            "adapter": self.config.adapter,
            "worker_name": self.config.worker_name,
            "project_id": state.project_id,
            "intent_id": state.intent_id,
            "mode": state.mode,
            "phase": phase,
            "session_id": state.session_id,
            "binary": self.config.binary,
            "returncode": result.exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": round(finished_at - started_at, 3),
            "commands": commands[:200],
            "stdout": result.stdout[:limit],
            "stderr": result.stderr[:limit],
            "stdout_truncated": len(result.stdout) > limit,
            "stderr_truncated": len(result.stderr) > limit,
            "error": error,
        }
        self._write_json_atomic(transcript, payload)
        ref = transcript.relative_to(state.workspace_root).as_posix()
        state.transcript_refs.append(ref)
        return ref
