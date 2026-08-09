from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import threading
import time
from typing import Protocol
from uuid import uuid4


DEFAULT_WORKER_IMAGE = os.environ.get(
    "SLIME_WORKER_IMAGE",
    "ghcr.io/moumouhao12138-sketch/slime-worker:0.0.37",
)
DEFAULT_CONTAINER_MEMORY: str | None = None
DEFAULT_CONTAINER_CPUS: str | None = None
DEFAULT_CONTAINER_PIDS_LIMIT: int | None = None


@dataclass(slots=True)
class CommandExecution:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str = ""


class InterruptibleCommand(Protocol):
    """A running command whose container process can be terminated explicitly."""

    def communicate(self, timeout: int | None = None) -> CommandExecution: ...

    def cancel(self, reason: str = "cancelled") -> bool: ...


class DockerExecProcess:
    """Track one ``docker exec`` invocation and stop its in-container process group.

    Killing only the host-side Docker CLI can leave the process started by
    ``docker exec`` behind.  The launch wrapper records a session-leader PID in
    the mounted workspace so cancellation can signal that process group from a
    second ``docker exec`` before the local client is terminated.
    """

    _PID_WAIT_SECONDS = 1.0
    _TERM_GRACE_SECONDS = 2.0
    _CLIENT_GRACE_SECONDS = 2.0

    def __init__(
        self,
        *,
        docker_binary: str,
        container_name: str,
        process: subprocess.Popen[str],
        env_path: Path | None,
        pid_path: Path,
    ) -> None:
        self._docker_binary = docker_binary
        self._container_name = container_name
        self._process = process
        self._env_path = env_path
        self._pid_path = pid_path
        self._lock = threading.RLock()
        self._cancel_reason = ""
        self._timed_out = False
        self._cleaned = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return bool(self._cancel_reason)

    @property
    def cancel_reason(self) -> str:
        with self._lock:
            return self._cancel_reason

    def communicate(self, timeout: int | None = None) -> CommandExecution:
        stdout = ""
        stderr = ""
        try:
            try:
                stdout, stderr = self._process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                with self._lock:
                    self._timed_out = True
                self.cancel("timeout")
                stdout, stderr = self._finish_after_interrupt(exc)
            except OSError as exc:
                return CommandExecution(127, "", f"{type(exc).__name__}: {exc}")

            code = self._process.returncode
            if code is None:
                code = 137 if self.cancelled else 1
            with self._lock:
                timed_out = self._timed_out
                cancelled = bool(self._cancel_reason) and not timed_out
                reason = self._cancel_reason
            if timed_out:
                code = 124
            return CommandExecution(
                int(code),
                self._text(stdout),
                self._text(stderr),
                timed_out=timed_out,
                cancelled=cancelled,
                cancel_reason=reason,
            )
        finally:
            self._cleanup_files()

    def cancel(self, reason: str = "cancelled") -> bool:
        """Request cancellation once and asynchronously escalate TERM to KILL."""

        text = str(reason).strip() or "cancelled"
        with self._lock:
            already_cancelled = bool(self._cancel_reason)
            if not already_cancelled:
                self._cancel_reason = text
            if self._process.poll() is not None:
                return not already_cancelled
            if already_cancelled:
                return False
        threading.Thread(target=self._terminate_target, name="slime-docker-exec-cancel", daemon=True).start()
        return True

    def _finish_after_interrupt(self, expired: subprocess.TimeoutExpired) -> tuple[str, str]:
        try:
            # Let the cancellation thread finish its in-container TERM/KILL
            # sequence before terminating the Docker client.  Terminating the
            # client first can detach it while the CLI process is still alive.
            return self._process.communicate(
                timeout=self._PID_WAIT_SECONDS + self._TERM_GRACE_SECONDS + self._CLIENT_GRACE_SECONDS + 1.0
            )
        except subprocess.TimeoutExpired:
            self._terminate_client()
            try:
                return self._process.communicate(timeout=self._CLIENT_GRACE_SECONDS)
            except subprocess.TimeoutExpired as exc:
                return (
                    self._text(exc.stdout or expired.stdout),
                    self._text(exc.stderr or expired.stderr),
                )

    def _terminate_target(self) -> None:
        pid = self._wait_for_pid()
        if pid is not None:
            self._terminate_process_tree(pid)
        deadline = time.monotonic() + self._CLIENT_GRACE_SECONDS
        while self._process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._process.poll() is None:
            self._terminate_client()

    def _wait_for_pid(self) -> int | None:
        deadline = time.monotonic() + self._PID_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                value = self._pid_path.read_text(encoding="ascii").strip()
                if value.isdecimal() and int(value) > 0:
                    return int(value)
            except OSError:
                pass
            if self._process.poll() is not None:
                return None
            time.sleep(0.025)
        return None

    def _terminate_process_tree(self, pid: int) -> None:
        """Terminate the complete in-container process tree, then its groups.

        Native agents can start commands that create their own process groups.
        Killing only the original session leader lets those descendants become
        orphans; with a persistent container they eventually exhaust the PID
        cgroup. Capture the descendants before TERM and always follow with KILL,
        even when the outer ``docker exec`` client has already returned.
        """

        script = (
            'pid="$1"\n'
            'case "$pid" in ""|*[!0-9]*) exit 2;; esac\n'
            'descendants() {\n'
            '  current="$1"\n'
            '  children_file="/proc/$current/task/$current/children"\n'
            '  if [ -r "$children_file" ]; then\n'
            '    children=""\n'
            '    IFS= read -r children < "$children_file" || true\n'
            '    for child in $children; do\n'
            '      descendants "$child"\n'
            '    done\n'
            '  fi\n'
            '  printf "%s\\n" "$current"\n'
            '}\n'
            'targets="$(descendants "$pid")"\n'
            'for target in $targets; do\n'
            '  kill -TERM -- "-$target" 2>/dev/null '
            '  || kill -TERM "$target" 2>/dev/null || true\n'
            'done\n'
            f'sleep {self._TERM_GRACE_SECONDS}\n'
            'for target in $targets; do\n'
            '  kill -KILL -- "-$target" 2>/dev/null '
            '  || kill -KILL "$target" 2>/dev/null || true\n'
            'done\n'
        )
        try:
            subprocess.run(
                [
                    self._docker_binary,
                    "exec",
                    self._container_name,
                    "/bin/sh",
                    "-lc",
                    script,
                    "--",
                    str(pid),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._TERM_GRACE_SECONDS + 5,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # The local docker client is still terminated below if this control
            # exec cannot be started or the container is already gone.
            return

    def _terminate_client(self) -> None:
        try:
            self._process.terminate()
        except OSError:
            return

    def _cleanup_files(self) -> None:
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        for path in (self._env_path, self._pid_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""


class ProcessBackend(Protocol):
    def execute(self, argv: list[str], cwd: Path, timeout: int) -> CommandExecution: ...


class HostProcessBackend:
    """Development fallback only. It is not a security sandbox."""

    def execute(self, argv: list[str], cwd: Path, timeout: int) -> CommandExecution:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return CommandExecution(completed.returncode, completed.stdout, completed.stderr)


@dataclass(slots=True)
class DockerWorkerConfig:
    image: str = DEFAULT_WORKER_IMAGE
    docker_binary: str = "docker"
    network: str = "bridge"
    # Match Cairn's unrestricted resource defaults. Operators can explicitly
    # add any Docker limit in the container configuration.
    memory: str | None = DEFAULT_CONTAINER_MEMORY
    cpus: str | None = DEFAULT_CONTAINER_CPUS
    pids_limit: int | None = DEFAULT_CONTAINER_PIDS_LIMIT
    user: str = "65532:65532"
    init: bool = True


@dataclass(frozen=True, slots=True)
class AgentConfigMount:
    """One read-only host Agent configuration tree exposed to a Worker container."""

    agent: str
    source: Path | str
    target: str

    def __post_init__(self) -> None:
        aliases = {
            "codex": "codex",
            "codex-cli": "codex",
            "claude": "claude",
            "claude-code": "claude",
            "claudecode": "claude",
            "pi": "pi",
            "pi-cli": "pi",
        }
        raw_agent = str(self.agent).strip().lower()
        try:
            agent = aliases[raw_agent]
        except KeyError as exc:
            raise ValueError(f"不支持的 Agent 配置挂载类型: {self.agent}") from exc

        source_text = os.path.expandvars(str(self.source).strip())
        if not source_text or "," in source_text:
            raise ValueError("Agent 配置挂载 source 无效")
        source = Path(source_text).expanduser().resolve()

        target_text = str(self.target).strip().replace("\\", "/")
        target = PurePosixPath(target_text)
        if (
            not target.is_absolute()
            or str(target) == "/"
            or "," in target_text
            or any(part == ".." for part in target.parts)
        ):
            raise ValueError(f"Agent 配置挂载 target 无效: {self.target}")

        object.__setattr__(self, "agent", agent)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", str(target))

    @property
    def docker_mount(self) -> str:
        """Render Docker's long bind-mount form, which handles Windows source paths."""

        return f"type=bind,source={self.source},target={self.target},readonly"


class DockerProcessBackend:
    """Runs one argv action in an ephemeral, low-privilege worker container."""

    def __init__(self, workspace_root: Path, config: DockerWorkerConfig | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config = config or DockerWorkerConfig()

    def build_command(self, argv: list[str], cwd: Path) -> list[str]:
        relative = cwd.resolve().relative_to(self.workspace_root)
        container_cwd = str(PurePosixPath("/workspace", *relative.parts))
        mount = f"{self.workspace_root}:/workspace:rw"
        command = [
            self.config.docker_binary,
            "run",
            "--rm",
            "--network", self.config.network,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self.config.user,
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
            "--volume", mount,
            "--workdir", container_cwd,
        ]
        if self.config.pids_limit is not None:
            command.extend(["--pids-limit", str(self.config.pids_limit)])
        if self.config.memory is not None:
            command.extend(["--memory", self.config.memory])
        if self.config.cpus is not None:
            command.extend(["--cpus", self.config.cpus])
        if self.config.init:
            command.append("--init")
        command.extend([self.config.image, *argv])
        return command

    def execute(self, argv: list[str], cwd: Path, timeout: int) -> CommandExecution:
        command = self.build_command(argv, cwd)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return CommandExecution(completed.returncode, completed.stdout, completed.stderr)


@dataclass(slots=True)
class PersistentDockerConfig(DockerWorkerConfig):
    container_name: str = "slime-cairn-worker"
    cap_add: tuple[str, ...] = ()
    extra_hosts: tuple[str, ...] = ()
    agent_config_mounts: tuple[AgentConfigMount, ...] = ()
    workspace_volume: str = ""
    workspace_volume_subpath: str = ""

    def __post_init__(self) -> None:
        mounts = tuple(self.agent_config_mounts)
        if any(not isinstance(mount, AgentConfigMount) for mount in mounts):
            raise TypeError("agent_config_mounts 必须由 AgentConfigMount 组成")
        targets = [mount.target for mount in mounts]
        if len(targets) != len(set(targets)):
            raise ValueError("Agent 配置挂载 target 不能重复")
        self.agent_config_mounts = mounts
        volume = str(self.workspace_volume).strip()
        subpath = str(self.workspace_volume_subpath).strip().replace("\\", "/")
        if bool(volume) != bool(subpath):
            raise ValueError("workspace_volume and workspace_volume_subpath must be configured together")
        if volume and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", volume):
            raise ValueError("workspace_volume contains unsupported characters")
        if subpath:
            relative = PurePosixPath(subpath)
            if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
                raise ValueError("workspace_volume_subpath must be one safe relative directory")
        self.workspace_volume = volume
        self.workspace_volume_subpath = subpath


class PersistentDockerBackend:
    """Keeps one low-privilege container alive so pseudopods can reuse tools and files."""

    def __init__(self, workspace_root: Path, config: PersistentDockerConfig | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config = config or PersistentDockerConfig()
        self._recovery_lock = threading.Lock()

    def prepare_writable_path(self, path: Path) -> None:
        """Give the non-root Worker ownership of a named-volume subtree."""

        candidate = path.resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("writable path escaped the project workspace") from exc
        if not self.config.workspace_volume:
            return
        if not candidate.exists():
            raise FileNotFoundError(candidate)

        match = re.fullmatch(r"(\d+)(?::(\d+))?", self.config.user)
        if match is None:
            raise ValueError("named-volume Worker user must use a numeric uid[:gid]")
        uid = int(match.group(1))
        gid = int(match.group(2) or match.group(1))
        paths = [candidate, *candidate.rglob("*")]
        if os.name == "posix":
            try:
                for item in paths:
                    os.chown(item, uid, gid, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"failed to assign Worker workspace ownership: {exc}") from exc
            return

        # Windows tests cannot change uid/gid. Keep the tree writable so path
        # validation and named-volume command construction remain testable.
        for item in paths:
            item.chmod(0o700 if item.is_dir() else 0o600)

    def create_command(self) -> list[str]:
        workspace_mount = f"{self.workspace_root}:/workspace:rw"
        command = [
            self.config.docker_binary,
            "create",
            "--name", self.config.container_name,
            "--network", self.config.network,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self.config.user,
        ]
        if self.config.pids_limit is not None:
            command.extend(["--pids-limit", str(self.config.pids_limit)])
        if self.config.memory is not None:
            command.extend(["--memory", self.config.memory])
        if self.config.cpus is not None:
            command.extend(["--cpus", self.config.cpus])
        if self.config.init:
            command.append("--init")
        for capability in self.config.cap_add:
            command.extend(["--cap-add", capability])
        for host in self.config.extra_hosts:
            command.extend(["--add-host", host])
        for agent_mount in self.config.agent_config_mounts:
            command.extend(["--mount", agent_mount.docker_mount])
        command.extend(["--tmpfs", "/tmp:rw,nosuid,nodev,size=128m"])
        if self.config.workspace_volume:
            command.extend(
                [
                    "--mount",
                    "type=volume,"
                    f"source={self.config.workspace_volume},"
                    "target=/workspace,"
                    f"volume-subpath={self.config.workspace_volume_subpath}",
                ]
            )
        else:
            command.extend(["--volume", workspace_mount])
        command.extend(
            [
                "--workdir", "/workspace",
                self.config.image,
                "sleep", "infinity",
            ]
        )
        return command

    def start_command(self) -> list[str]:
        return [self.config.docker_binary, "start", self.config.container_name]

    def inspect_command(self) -> list[str]:
        return [
            self.config.docker_binary,
            "inspect",
            "--format",
            "{{.State.Running}}",
            self.config.container_name,
        ]

    def inspect_image_command(self) -> list[str]:
        """Return the image reference used to create this persistent container."""

        return [
            self.config.docker_binary,
            "inspect",
            "--format",
            "{{.Config.Image}}",
            self.config.container_name,
        ]

    def inspect_container_image_id_command(self) -> list[str]:
        """Return the immutable image ID currently backing the container."""

        return [
            self.config.docker_binary,
            "inspect",
            "--format",
            "{{.Image}}",
            self.config.container_name,
        ]

    def inspect_image_id_command(self) -> list[str]:
        """Resolve the configured image reference to its current immutable ID."""

        return [
            self.config.docker_binary,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            self.config.image,
        ]

    def inspect_network_command(self) -> list[str]:
        """Return the Docker network mode fixed at container creation time."""

        return [
            self.config.docker_binary,
            "inspect",
            "--format",
            "{{.HostConfig.NetworkMode}}",
            self.config.container_name,
        ]

    def inspect_runtime_command(self) -> list[str]:
        """Return create-time resource settings that require recreation to change."""

        return [
            self.config.docker_binary,
            "inspect",
            "--format",
            "{{json .HostConfig}}",
            self.config.container_name,
        ]

    def stop_command(self) -> list[str]:
        return [self.config.docker_binary, "stop", "--time", "5", self.config.container_name]

    def restart_command(self) -> list[str]:
        return [self.config.docker_binary, "restart", "--time", "2", self.config.container_name]

    def pids_current_command(self) -> list[str]:
        return [
            self.config.docker_binary,
            "exec",
            self.config.container_name,
            "/bin/cat",
            "/sys/fs/cgroup/pids.current",
        ]

    def remove_command(self, force: bool = False) -> list[str]:
        command = [self.config.docker_binary, "rm"]
        if force:
            command.append("--force")
        command.append(self.config.container_name)
        return command

    def build_exec_command(
        self,
        argv: list[str],
        cwd: Path,
        interactive: bool = False,
        env_file: str | None = None,
    ) -> list[str]:
        relative = cwd.resolve().relative_to(self.workspace_root)
        container_cwd = str(PurePosixPath("/workspace", *relative.parts))
        command = [self.config.docker_binary, "exec"]
        if interactive:
            command.append("--interactive")
        if env_file:
            command.extend(["--env-file", env_file])
        command.extend(["--workdir", container_cwd, self.config.container_name, *argv])
        return command

    def execute(self, argv: list[str], cwd: Path, timeout: int) -> CommandExecution:
        completed = subprocess.run(
            self.build_exec_command(argv, cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return CommandExecution(completed.returncode, completed.stdout, completed.stderr)

    def _run_control_command(self, argv: list[str], timeout: float = 15) -> CommandExecution:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
            return CommandExecution(completed.returncode, completed.stdout, completed.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandExecution(127, "", f"{type(exc).__name__}: {exc}")

    def recover_resource_exhaustion(self, detail: str = "") -> dict[str, object]:
        """Restart a wedged persistent runtime once and preserve its workspace.

        Multiple pseudopods may observe ``Cannot fork`` together. The lock and
        PID probe let the first caller restart the container while later callers
        reuse the recovered runtime instead of restarting it repeatedly.
        """

        with self._recovery_lock:
            probe = self._run_control_command(self.pids_current_command(), timeout=5)
            try:
                current_pids = int(probe.stdout.strip()) if probe.exit_code == 0 else None
            except ValueError:
                current_pids = None
            healthy_threshold = (
                16
                if self.config.pids_limit is None
                else max(16, self.config.pids_limit // 2)
            )
            if current_pids is not None and current_pids < healthy_threshold:
                return {
                    "recovered": False,
                    "action": "runtime_already_healthy",
                    "pids_current": current_pids,
                    "detail": detail[-500:],
                }

            restarted = self._run_control_command(self.restart_command(), timeout=20)
            if restarted.exit_code != 0:
                message = (restarted.stderr or restarted.stdout).strip()
                raise RuntimeError(f"failed to restart exhausted project runtime: {message}")
            inspected = self._run_control_command(self.inspect_command(), timeout=10)
            if inspected.exit_code != 0 or inspected.stdout.strip().lower() != "true":
                message = (inspected.stderr or inspected.stdout).strip()
                raise RuntimeError(f"project runtime did not recover after restart: {message}")
            return {
                "recovered": True,
                "action": "container_restarted",
                "pids_before": current_pids,
                "detail": detail[-500:],
            }

    @staticmethod
    def _clean_environment(environment: dict[str, str] | None) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for raw_name, raw_value in dict(environment or {}).items():
            name = str(raw_name).strip()
            value = str(raw_value)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid container environment variable name: {name}")
            if "\n" in value or "\r" in value:
                raise ValueError(f"container environment variable {name} cannot contain newlines")
            cleaned[name] = value
        return cleaned

    @staticmethod
    def _write_environment_file(environment: dict[str, str]) -> Path | None:
        if not environment:
            return None
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="slime-native-env-",
            suffix=".list",
            delete=False,
        )
        try:
            for name, value in sorted(environment.items()):
                handle.write(f"{name}={value}\n")
        finally:
            handle.close()
        path = Path(handle.name)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def _pid_file(self) -> tuple[Path, str]:
        root = self.workspace_root / ".slime-cairn-runtime"
        root.mkdir(parents=True, exist_ok=True)
        self.prepare_writable_path(root)
        name = f"exec-{uuid4().hex}.pid"
        path = root / name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return path, str(PurePosixPath("/workspace", ".slime-cairn-runtime", name))

    @staticmethod
    def _pid_wrapped_argv(pid_file: str, argv: list[str]) -> list[str]:
        """Launch the CLI in a fresh session and expose its PID on the workspace mount."""

        script = r"""pid_file="$1"
shift
mkdir -p "$(dirname "$pid_file")"
if command -v setsid >/dev/null 2>&1 && setsid --help 2>&1 | grep -q -- '--wait'; then
  # setsid normally forks and makes docker exec return before the CLI is done.
  # --wait preserves the session boundary while keeping stdout attached to docker exec.
  exec setsid --wait /bin/sh -c 'printf "%s\n" "$$" > "$1"; shift; exec "$@"' -- "$pid_file" "$@"
fi
printf "%s\n" "$$" > "$pid_file"
exec "$@"
"""
        return ["/bin/sh", "-lc", script, "--", pid_file, *argv]

    def start_with_environment(
        self,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> DockerExecProcess:
        """Start an interruptible native CLI command inside the project container."""

        env_path: Path | None = None
        pid_path: Path | None = None
        try:
            if timeout_seconds is not None and timeout_seconds < 1:
                raise ValueError("timeout_seconds must be positive")
            if kill_after_seconds < 1:
                raise ValueError("kill_after_seconds must be positive")
            bounded_argv = list(argv)
            if timeout_seconds is not None:
                # Cairn's primary timeout boundary lives inside the project
                # container. The already-running timeout process can TERM and
                # then KILL the Agent without needing a second docker exec.
                bounded_argv = [
                    "timeout",
                    "-k",
                    f"{kill_after_seconds}s",
                    f"{timeout_seconds}s",
                    *bounded_argv,
                ]
            env_path = self._write_environment_file(self._clean_environment(environment))
            pid_path, container_pid_file = self._pid_file()
            command = self.build_exec_command(
                self._pid_wrapped_argv(container_pid_file, bounded_argv),
                cwd,
                env_file=str(env_path) if env_path is not None else None,
            )
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            return DockerExecProcess(
                docker_binary=self.config.docker_binary,
                container_name=self.config.container_name,
                process=process,
                env_path=env_path,
                pid_path=pid_path,
            )
        except Exception:
            for path in (env_path, pid_path):
                if path is None:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def execute_with_environment(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        environment: dict[str, str] | None = None,
    ) -> CommandExecution:
        """Run an interruptible native CLI command and wait for its result."""

        try:
            return self.start_with_environment(
                argv,
                cwd,
                environment,
                timeout_seconds=timeout,
            ).communicate(timeout + 15)
        except OSError as exc:
            return CommandExecution(127, "", f"{type(exc).__name__}: {exc}")

    def _execute_with_environment_legacy(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        environment: dict[str, str] | None = None,
    ) -> CommandExecution:
        """Execute inside the project container without putting secrets in argv.

        Docker reads a short-lived host-side env file.  Its path may appear in
        the process list, but values do not; the file is deleted in ``finally``.
        """

        cleaned: dict[str, str] = {}
        for raw_name, raw_value in dict(environment or {}).items():
            name = str(raw_name).strip()
            value = str(raw_value)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"无效容器环境变量名: {name}")
            if "\n" in value or "\r" in value:
                raise ValueError(f"容器环境变量 {name} 不能包含换行")
            cleaned[name] = value

        env_path: str | None = None
        try:
            if cleaned:
                handle = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="slime-native-env-",
                    suffix=".list",
                    delete=False,
                )
                try:
                    for name, value in sorted(cleaned.items()):
                        handle.write(f"{name}={value}\n")
                finally:
                    handle.close()
                env_path = handle.name
                try:
                    os.chmod(env_path, 0o600)
                except OSError:
                    pass
            completed = subprocess.run(
                self.build_exec_command(argv, cwd, env_file=env_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
            return CommandExecution(completed.returncode, completed.stdout, completed.stderr)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or f"timeout after {timeout}s"
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return CommandExecution(124, stdout, stderr)
        except OSError as exc:
            return CommandExecution(127, "", f"{type(exc).__name__}: {exc}")
        finally:
            if env_path:
                try:
                    Path(env_path).unlink(missing_ok=True)
                except OSError:
                    pass

