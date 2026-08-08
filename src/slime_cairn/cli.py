from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser


COMMANDS = (
    "help",
    "install",
    "uninstall",
    "up",
    "down",
    "restart",
    "serve",
    "dispatch",
    "new",
    "list",
    "status",
    "runtime",
    "logs",
    "stop",
    "pause",
    "resume",
    "delete",
    "ui",
    "doctor",
)
PROFILE_START = "# >>> slime-cairn PATH >>>"
PROFILE_END = "# <<< slime-cairn PATH <<<"
LAUNCHER_MARKER = "# Managed by Slime Cairn CLI."


class CliError(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


def _discover_project_root(explicit: str = "") -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    configured = os.environ.get("SLIME_PROJECT_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.extend([Path.cwd(), Path(__file__).resolve().parents[2]])
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "dispatch.cairn.native.json").is_file():
            return resolved
    raise CliError(
        "Slime project root was not found. Run from the repository or set "
        "SLIME_PROJECT_ROOT."
    )


def _resolve_from(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(slots=True)
class RuntimeContext:
    root: Path
    config: Path
    database: Path
    workspaces: Path
    runs: Path
    service_dir: Path
    state_file: Path
    profile: str
    docker_binary: str
    lab_network: str
    bind_host: str
    port: int

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RuntimeContext":
        root = _discover_project_root(args.project_root)
        runs = (root / "runs").resolve()
        service_dir = runs / "service"
        return cls(
            root=root,
            config=_resolve_from(root, args.config),
            database=_resolve_from(root, args.database),
            workspaces=_resolve_from(root, args.workspaces_root),
            runs=runs,
            service_dir=service_dir,
            state_file=service_dir / "state.json",
            profile=args.profile,
            docker_binary=args.docker_binary,
            lab_network=args.lab_network,
            bind_host=args.bind_host,
            port=args.port,
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        source = str(self.root / "src")
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
        environment.update(
            {
                "SLIME_PROJECT_ROOT": str(self.root),
                "SLIME_CAIRN_DB": str(self.database),
                "SLIME_RUNTIME_MODE": "docker",
                "SLIME_WORKSPACES_ROOT": str(self.workspaces),
                "SLIME_WORKER_PROFILE": self.profile,
                "SLIME_DISPATCH_CONFIG": str(self.config),
                "SLIME_DOCKER_BINARY": self.docker_binary,
                "SLIME_LAB_NETWORK": self.lab_network,
            }
        )
        return environment


def print_help() -> None:
    print("Slime Cairn")
    print()
    print("  slime up")
    print("  slime new --name ctf-001 --target https://TARGET/ --start-mode growth")
    print("  slime status --name ctf-001")
    print("  slime ui")
    print("  slime logs --follow")
    print("  slime list")
    print("  slime pause --name ctf-001")
    print("  slime resume --name ctf-001")
    print("  slime delete --name ctf-001")
    print("  slime down")
    print()
    print("Foreground development: slime serve | slime dispatch")
    print("Command setup:         slime install | slime uninstall")


class SlimeCli:
    def __init__(self, args: argparse.Namespace, context: RuntimeContext) -> None:
        self.args = args
        self.context = context

    def run(self) -> int:
        command = self.args.command
        actions = {
            "help": self.show_help,
            "install": self.install,
            "uninstall": self.uninstall,
            "up": self.up,
            "down": self.down,
            "restart": self.restart,
            "serve": self.serve,
            "dispatch": self.dispatch,
            "new": self.new_project,
            "list": self.list_projects,
            "status": self.status,
            "runtime": self.runtime,
            "logs": self.logs,
            "stop": lambda: self.set_project_status("stopped"),
            "pause": lambda: self.set_project_status("stopped"),
            "resume": lambda: self.set_project_status("running"),
            "delete": self.delete_project,
            "ui": self.open_ui,
            "doctor": self.doctor,
        }
        result = actions[command]()
        return int(result or 0)

    def show_help(self) -> None:
        print_help()

    def install(self) -> None:
        if _is_windows():
            self._install_windows()
            return
        home = Path.home().resolve()
        command_dir = home / ".local" / "bin"
        target = command_dir / "slime"
        profile = home / ".profile"
        launcher = self._posix_launcher()
        if target.exists() and LAUNCHER_MARKER not in target.read_text(
            encoding="utf-8", errors="replace"
        ):
            raise CliError(f"Refusing to replace an unmanaged command: {target}")
        path_entries = [Path(item).expanduser() for item in os.environ.get("PATH", "").split(os.pathsep) if item]
        path_ready = any(item.resolve() == command_dir for item in path_entries)
        profile_text = profile.read_text(encoding="utf-8") if profile.exists() else ""
        add_profile = not path_ready and PROFILE_START not in profile_text
        if self.args.dry_run:
            print(f"Would install executable: {target}")
            if add_profile:
                print(f"Would add ~/.local/bin to PATH in: {profile}")
            return
        command_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(launcher, encoding="utf-8", newline="\n")
        target.chmod(0o755)
        if add_profile:
            block = (
                f"{PROFILE_START}\n"
                'case ":$PATH:" in\n'
                '  *":$HOME/.local/bin:"*) ;;\n'
                '  *) export PATH="$HOME/.local/bin:$PATH" ;;\n'
                "esac\n"
                f"{PROFILE_END}\n"
            )
            separator = "" if not profile_text or profile_text.endswith("\n") else "\n"
            profile.write_text(profile_text + separator + block, encoding="utf-8", newline="\n")
        print(f"Installed Slime command: {target}")
        print("Open a new terminal, then use: slime up")

    def uninstall(self) -> None:
        if _is_windows():
            self._uninstall_windows()
            return
        home = Path.home().resolve()
        target = home / ".local" / "bin" / "slime"
        profile = home / ".profile"
        managed_target = target.exists() and LAUNCHER_MARKER in target.read_text(
            encoding="utf-8", errors="replace"
        )
        profile_text = profile.read_text(encoding="utf-8") if profile.exists() else ""
        next_profile = self._without_profile_block(profile_text)
        if self.args.dry_run:
            if managed_target:
                print(f"Would remove executable: {target}")
            if next_profile != profile_text:
                print(f"Would remove managed PATH block from: {profile}")
            if not managed_target and next_profile == profile_text:
                print("Slime command is not installed for this user.")
            return
        if target.exists() and not managed_target:
            raise CliError(f"Refusing to remove an unmanaged command: {target}")
        if managed_target:
            target.unlink()
        if next_profile != profile_text:
            profile.write_text(next_profile, encoding="utf-8", newline="\n")
        print("Removed the Slime user command. Open a new terminal to refresh PATH.")

    def _install_windows(self) -> None:
        command_dir = (self.context.root / "bin").resolve()
        current = self._read_windows_user_path()
        entries = self._windows_path_entries(current)
        if any(self._same_windows_path(entry, command_dir) for entry in entries):
            print(f"Slime command is already installed in the current user PATH: {command_dir}")
            return
        if self.args.dry_run:
            print(f"Would add to the current user PATH: {command_dir}")
            return
        entries.append(str(command_dir))
        self._write_windows_user_path(";".join(entries))
        print(f"Installed Slime command in the current user PATH: {command_dir}")
        print("Open a new terminal, then use: slime up")

    def _uninstall_windows(self) -> None:
        command_dir = (self.context.root / "bin").resolve()
        current = self._read_windows_user_path()
        entries = self._windows_path_entries(current)
        remaining = [
            entry for entry in entries if not self._same_windows_path(entry, command_dir)
        ]
        if len(remaining) == len(entries):
            print("Slime command is not installed in the current user PATH.")
            return
        if self.args.dry_run:
            print(f"Would remove from the current user PATH: {command_dir}")
            return
        self._write_windows_user_path(";".join(remaining))
        print("Removed the Slime user command. Open a new terminal to refresh PATH.")

    @staticmethod
    def _windows_path_entries(value: str) -> list[str]:
        return [entry.strip() for entry in value.split(";") if entry.strip()]

    @staticmethod
    def _same_windows_path(left: str, right: Path) -> bool:
        try:
            normalized_left = str(Path(left).expanduser().resolve()).rstrip("\\/").casefold()
        except OSError:
            normalized_left = left.rstrip("\\/").casefold()
        return normalized_left == str(right).rstrip("\\/").casefold()

    @staticmethod
    def _read_windows_user_path() -> str:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return ""
        return str(value)

    @staticmethod
    def _write_windows_user_path(value: str) -> None:
        import ctypes
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF,
                0x001A,
                0,
                "Environment",
                0x0002,
                5000,
                None,
            )
        except (AttributeError, OSError):
            pass

    def _posix_launcher(self) -> str:
        root = shlex.quote(str(self.context.root))
        python = shlex.quote(sys.executable)
        return (
            "#!/bin/sh\n"
            f"{LAUNCHER_MARKER}\n"
            f"SLIME_PROJECT_ROOT={root}\n"
            "export SLIME_PROJECT_ROOT\n"
            'PYTHONPATH="$SLIME_PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"\n'
            "export PYTHONPATH\n"
            f"exec {python} -m slime_cairn.cli \"$@\"\n"
        )

    @staticmethod
    def _without_profile_block(text: str) -> str:
        start = text.find(PROFILE_START)
        if start < 0:
            return text
        end = text.find(PROFILE_END, start)
        if end < 0:
            return text
        end += len(PROFILE_END)
        if end < len(text) and text[end] == "\n":
            end += 1
        return text[:start] + text[end:]

    def up(self) -> None:
        context = self.context
        context.service_dir.mkdir(parents=True, exist_ok=True)
        context.workspaces.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        server_alive = self._state_process_alive(state, "server")
        dispatcher_alive = self._state_process_alive(state, "dispatcher")
        restart_needed = bool(
            state
            and (
                str(state.get("config", "")) != str(context.config)
                or str(state.get("runtime_mode", "")) != "docker"
                or str(state.get("profile", "")) != context.profile
                or str(state.get("bind_host", context.bind_host)) != context.bind_host
                or int(state.get("port", context.port)) != context.port
            )
        )
        if self.args.dry_run:
            print(f"ProjectRoot:     {context.root}")
            print(f"PYTHONPATH:      {context.root / 'src'}")
            print(f"Database:        {context.database}")
            print(f"WorkspacesRoot:  {context.workspaces}")
            print(f"Config:          {context.config}")
            print("Runtime:         persistent Kali native-agent")
            print(f"Profile:         {context.profile}")
            print(f"BaseUrl:         {context.base_url}")
            print(f"ServerAlive:     {server_alive}")
            print(f"DispatcherAlive: {dispatcher_alive}")
            print(f"RestartNeeded:   {restart_needed}")
            return
        if restart_needed:
            print("Service configuration changed; restarting current runtime.")
            self._stop_from_state(state)
            state = None
            server_alive = False
            dispatcher_alive = False
        if self.args.no_dispatcher and dispatcher_alive:
            self._stop_named_process(state, "dispatcher")
            dispatcher_alive = False
        environment = context.environment()
        if not self.args.no_dispatcher:
            print("Checking configured container Workers before startup...")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "slime_cairn.diagnostics",
                    "--config",
                    str(context.config),
                    "--docker-binary",
                    context.docker_binary,
                    "--strict",
                ],
                cwd=context.root,
                env=environment,
                check=False,
            )
            if completed.returncode:
                raise CliError("Worker preflight failed. Update .env or run slime doctor.")
        server_out = context.service_dir / "server.out.log"
        server_err = context.service_dir / "server.err.log"
        dispatcher_out = context.service_dir / "dispatcher.out.log"
        dispatcher_err = context.service_dir / "dispatcher.err.log"
        if not server_alive:
            server_pid, server_pgid = self._spawn_background(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "slime_cairn.api:app",
                    "--host",
                    context.bind_host,
                    "--port",
                    str(context.port),
                ],
                server_out,
                server_err,
                environment,
            )
            print(f"Server started pid={server_pid} url={context.base_url}")
        else:
            server_pid = int(state["server_pid"])
            server_pgid = state.get("server_pgid")
            print(f"Server reused pid={server_pid} url={context.base_url}")
        if self._wait_for_health(30):
            print("Server health ok")
        else:
            print(f"Server health pending; see {server_err}")
        if self.args.no_dispatcher:
            dispatcher_pid = None
            dispatcher_pgid = None
        elif not dispatcher_alive:
            dispatcher_pid, dispatcher_pgid = self._spawn_background(
                [sys.executable, "-m", "slime_cairn.service_main"],
                dispatcher_out,
                dispatcher_err,
                environment,
            )
            print(f"Dispatcher started pid={dispatcher_pid}")
        else:
            dispatcher_pid = int(state["dispatcher_pid"])
            dispatcher_pgid = state.get("dispatcher_pgid")
            print(f"Dispatcher reused pid={dispatcher_pid}")
        self._write_state(
            {
                "server_pid": server_pid,
                "server_pgid": server_pgid,
                "dispatcher_pid": dispatcher_pid,
                "dispatcher_pgid": dispatcher_pgid,
                "base_url": context.base_url,
                "bind_host": context.bind_host,
                "port": context.port,
                "database": str(context.database),
                "workspaces_root": str(context.workspaces),
                "config": str(context.config),
                "runtime_mode": "docker",
                "profile": context.profile,
                "docker_binary": context.docker_binary,
                "lab_network": context.lab_network,
                "service_dir": str(context.service_dir),
                "platform": sys.platform,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        print(f"State: {context.state_file}")
        print(f"Logs:  {context.service_dir}")

    def down(self) -> None:
        state = self._read_state()
        if not state:
            print(f"No service state file: {self.context.state_file}")
            return
        if self.args.dry_run:
            print(f"Would stop dispatcher pid={state.get('dispatcher_pid')}")
            print(f"Would stop server pid={state.get('server_pid')}")
            return
        self._stop_from_state(state)
        self.context.state_file.unlink(missing_ok=True)

    def restart(self) -> None:
        self.down()
        if not self.args.dry_run:
            self.up()

    def serve(self) -> int:
        print(f"Serving Slime Cairn API on {self.context.base_url}")
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "slime_cairn.api:app",
                "--host",
                self.context.bind_host,
                "--port",
                str(self.context.port),
            ],
            cwd=self.context.root,
            env=self.context.environment(),
        )

    def dispatch(self) -> int:
        print(f"Running Slime Cairn dispatcher with {self.context.config}")
        return subprocess.call(
            [sys.executable, "-m", "slime_cairn.service_main"],
            cwd=self.context.root,
            env=self.context.environment(),
        )

    def new_project(self) -> None:
        if not self.args.target.strip():
            raise CliError("Target is required. Example: slime new --name ctf-001 --target https://TARGET/")
        self._require_health()
        payload = {
            "name": self.args.name,
            "target": self.args.target,
            "goal": self.args.goal,
            "allowed_targets": [self.args.target],
            "title": self.args.name,
            "origin": self.args.target,
            "bootstrap_enabled": self.args.start_mode == "direct",
        }
        project = self._api_json("POST", "/projects", payload)["project"]
        print(f"Project created: {project['name']}")
        print(f"Project id:      {project['id']}")
        print(f"Target:          {project['target']}")
        print(f"Status:          {project['status']}")
        print(f"Start mode:      {self.args.start_mode}")
        print()
        print(f"Check progress:  slime status --name {self.args.name}")
        print("Watch logs:      slime logs --follow")

    def list_projects(self) -> None:
        if self._api_healthy():
            projects = self._api_json("GET", "/projects").get("projects", [])
            print(f"{'STATUS':10} {'NAME':24} {'ID':28} TARGET")
            for project in projects:
                print(
                    f"{str(project['status']):10} {str(project['name']):24} "
                    f"{str(project['id']):28} {project['target']}"
                )
            return
        print("API is not responding; reading database summary.")
        if not self.context.database.exists():
            print(f"database not found: {self.context.database}")
            return
        with sqlite3.connect(self.context.database) as connection:
            rows = connection.execute(
                "SELECT id, name, target, status FROM projects ORDER BY created_at"
            ).fetchall()
        for project_id, name, target, status in rows:
            print(f"{status:10} {name:24} {project_id} {target}")

    def status(self) -> int:
        if not self.context.database.exists():
            print(f"Database pending: {self.context.database}")
            return 0
        return subprocess.call(
            [
                sys.executable,
                str(self.context.root / "scripts" / "check-e2e-result.py"),
                "--database",
                str(self.context.database),
                "--name",
                self.args.name,
                "--no-fail",
            ],
            cwd=self.context.root,
            env=self.context.environment(),
        )

    def runtime(self) -> None:
        self._require_health()
        project_id = self._project_id(self.args.name)
        print(json.dumps(self._api_json("GET", f"/projects/{project_id}/runtime"), indent=2))

    def open_ui(self) -> None:
        self._require_health()
        opened = webbrowser.open(self.context.base_url + "/")
        print(f"Opened Slime Cairn UI: {self.context.base_url}/" if opened else self.context.base_url + "/")

    def set_project_status(self, status: str) -> None:
        self._require_health()
        project_id = self._project_id(self.args.name)
        result = self._api_json("PUT", f"/projects/{project_id}/status", {"status": status})
        print(f"Project {self.args.name} -> {result['project']['status']}")

    def delete_project(self) -> None:
        self._require_health()
        project_id = self._project_id(self.args.name)
        result = self._api_json("DELETE", f"/projects/{project_id}")
        if result.get("accepted"):
            print(f"Project deletion requested: {self.args.name}")
            print("The service is stopping its Worker and clearing project-owned runtime data.")
        else:
            print(f"Project deletion is already in progress: {self.args.name}")

    def logs(self) -> None:
        files = self._log_files()
        self.context.service_dir.mkdir(parents=True, exist_ok=True)
        positions: dict[Path, int] = {}
        for path in files:
            print(f"\n===== {path} =====")
            if not path.exists():
                print("pending")
                path.touch()
                positions[path] = 0
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-self.args.tail :]:
                print(line)
            positions[path] = path.stat().st_size
        if not self.args.follow:
            return
        print("\nFollowing logs; press Ctrl+C to stop.")
        try:
            while True:
                for path in files:
                    size = path.stat().st_size if path.exists() else 0
                    if size < positions[path]:
                        positions[path] = 0
                    if size == positions[path]:
                        continue
                    with path.open("rb") as handle:
                        handle.seek(positions[path])
                        data = handle.read()
                        positions[path] = handle.tell()
                    if data:
                        print(f"\n===== {path.name} =====")
                        print(data.decode("utf-8", errors="replace"), end="", flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            return

    def doctor(self) -> int:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "slime_cairn.diagnostics",
                "--config",
                str(self.context.config),
                "--docker-binary",
                self.context.docker_binary,
            ],
            cwd=self.context.root,
            env=self.context.environment(),
        )

    def _read_state(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.context.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write_state(self, state: dict[str, Any]) -> None:
        self.context.service_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.context.state_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(self.context.state_file)

    @staticmethod
    def _pid_alive(value: Any) -> bool:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return False
        if pid <= 1:
            return False
        if os.name == "nt":
            # Windows does not support the POSIX ``kill(pid, 0)`` probe and
            # raises WinError 87 for a valid PID.  Use tasklist's CSV output,
            # which is available on supported Windows versions and avoids
            # treating a stale service PID as a live process.
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                return False
            return result.returncode == 0 and f'"{pid}"' in result.stdout
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                return stat.read_text(encoding="utf-8").split()[2] != "Z"
            except (OSError, IndexError):
                pass
        return True

    def _state_process_alive(self, state: dict[str, Any] | None, name: str) -> bool:
        return bool(state and self._pid_alive(state.get(f"{name}_pid")))

    def _process_matches(self, pid: int, name: str) -> bool:
        command_line = Path(f"/proc/{pid}/cmdline")
        if not command_line.exists():
            return True
        try:
            text = command_line.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return True
        expected = "slime_cairn.service_main" if name == "dispatcher" else "slime_cairn.api:app"
        return expected in text

    def _stop_named_process(self, state: dict[str, Any] | None, name: str) -> None:
        if not state:
            return
        value = state.get(f"{name}_pid")
        if not self._pid_alive(value):
            print(f"{name} already stopped pid={value or ''}")
            return
        pid = int(value)
        if os.name != "nt" and not self._process_matches(pid, name):
            print(f"{name} state is stale; refusing to stop unrelated pid={pid}")
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            pgid_value = state.get(f"{name}_pgid")
            pgid = int(pgid_value) if pgid_value else pid
            try:
                if pgid > 1 and pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while self._pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._pid_alive(pid):
                try:
                    if pgid > 1 and pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        while self._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pid_alive(pid):
            raise CliError(f"Failed to stop {name} pid={pid}")
        if os.name != "nt":
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        print(f"{name} stopped pid={pid}")

    def _stop_from_state(self, state: dict[str, Any] | None) -> None:
        self._stop_named_process(state, "dispatcher")
        self._stop_named_process(state, "server")

    def _spawn_background(
        self,
        argv: list[str],
        stdout_path: Path,
        stderr_path: Path,
        environment: dict[str, str],
    ) -> tuple[int, int | None]:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            read_fd = os.open(os.devnull, os.O_RDONLY)
            stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            ready_read_fd, ready_write_fd = os.pipe()
            try:
                pid = os.fork()
                if pid == 0:
                    try:
                        os.close(ready_read_fd)
                        os.setsid()
                        os.chdir(self.context.root)
                        os.dup2(read_fd, 0)
                        os.dup2(stdout_fd, 1)
                        os.dup2(stderr_fd, 2)
                        for descriptor in {read_fd, stdout_fd, stderr_fd}:
                            if descriptor > 2:
                                os.close(descriptor)
                        os.execve(argv[0], argv, environment)
                    except BaseException as exc:
                        message = f"background launch failed: {exc}\n".encode(
                            "utf-8", errors="replace"
                        )
                        os.write(2, message)
                        os.write(ready_write_fd, message[:4096])
                        os._exit(127)
                os.close(ready_write_fd)
                launch_error = os.read(ready_read_fd, 4096)
                os.close(ready_read_fd)
                if launch_error:
                    os.waitpid(pid, 0)
                    raise CliError(launch_error.decode("utf-8", errors="replace").strip())
                return pid, pid
            finally:
                for descriptor in (
                    read_fd,
                    stdout_fd,
                    stderr_fd,
                    ready_read_fd,
                    ready_write_fd,
                ):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=self.context.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            )
        return process.pid, None

    def _api_healthy(self) -> bool:
        try:
            self._api_json("GET", "/health", timeout=2)
            return True
        except CliError:
            return False

    def _wait_for_health(self, seconds: int) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._api_healthy():
                return True
            time.sleep(0.5)
        return False

    def _require_health(self) -> None:
        if not self._api_healthy():
            raise CliError("API is not responding. Start it with: slime up")

    def _api_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 10,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.context.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"} if body else {},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise CliError(f"API {method} {path} failed ({exc.code}): {details}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CliError(f"API {method} {path} failed: {exc}") from exc

    def _project_id(self, name: str) -> str:
        projects = self._api_json("GET", "/projects").get("projects", [])
        matches = [project for project in projects if project.get("name") == name]
        if not matches:
            raise CliError(f"Project name not found: {name}")
        matches.sort(key=lambda item: float(item.get("created_at", 0)), reverse=True)
        return str(matches[0]["id"])

    def _log_files(self) -> list[Path]:
        names = {
            "server": ["server.out.log", "server.err.log"],
            "dispatcher": ["dispatcher.out.log", "dispatcher.err.log"],
            "all": [
                "dispatcher.out.log",
                "dispatcher.err.log",
                "server.out.log",
                "server.err.log",
            ],
        }
        return [self.context.service_dir / name for name in names[self.args.log]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slime", allow_abbrev=False)
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="help")
    parser.add_argument("-Name", "--name", default="ctf-test")
    parser.add_argument("-Target", "--target", default="")
    parser.add_argument(
        "-Goal",
        "--goal",
        default="AI pseudopods analyze target evidence, solve the CTF task, and Reason returns the final result.",
    )
    parser.add_argument("-Config", "--config", default="dispatch.cairn.native.json")
    parser.add_argument("-Database", "--database", default="runs/slime-server.db")
    parser.add_argument("-WorkspacesRoot", "--workspaces-root", default="runs/slime-workspaces")
    parser.add_argument(
        "-Profile",
        "--profile",
        choices=("standard", "raw-network", "lab-network-admin"),
        default="raw-network",
    )
    parser.add_argument("-StartMode", "--start-mode", choices=("growth", "direct"), default="growth")
    parser.add_argument("-DockerBinary", "--docker-binary", default="docker")
    parser.add_argument("-LabNetwork", "--lab-network", default="slime-cairn-lab")
    parser.add_argument("-BindHost", "--bind-host", default="127.0.0.1")
    parser.add_argument("-Port", "--port", type=int, default=8000)
    parser.add_argument("-Tail", "--tail", type=int, default=80)
    parser.add_argument("-Log", "--log", choices=("dispatcher", "server", "all"), default="dispatcher")
    parser.add_argument("-Follow", "--follow", action="store_true")
    parser.add_argument("-NoDispatcher", "--no-dispatcher", action="store_true")
    parser.add_argument("-DryRun", "--dry-run", action="store_true")
    parser.add_argument("--project-root", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "help":
            print_help()
            return
        context = RuntimeContext.from_args(args)
        raise SystemExit(SlimeCli(args, context).run())
    except CliError as exc:
        parser.exit(2, f"slime: {exc}\n")


if __name__ == "__main__":
    main()
