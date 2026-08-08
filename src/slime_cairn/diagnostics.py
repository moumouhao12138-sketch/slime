from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from . import __version__
from .dispatcher import REQUIRED_TASK_MODES, load_dispatch_config
from .execution import DEFAULT_WORKER_IMAGE


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )


def _docker_health(binary: str, image: str, timeout: int = 20) -> dict[str, Any]:
    resolved = shutil.which(binary)
    if not resolved:
        return {
            "healthy": False,
            "binary": binary,
            "image": image,
            "error": "Docker CLI not found",
        }
    try:
        version = _run([resolved, "--version"], timeout)
        if version.returncode != 0:
            return {
                "healthy": False,
                "binary": resolved,
                "image": image,
                "error": (version.stderr or version.stdout).strip()[-1000:],
            }
        engine = _run([resolved, "info", "--format", "{{.ServerVersion}}"], timeout)
        if engine.returncode != 0 or not engine.stdout.strip():
            return {
                "healthy": False,
                "cli_healthy": True,
                "engine_healthy": False,
                "binary": resolved,
                "image": image,
                "error": (engine.stderr or engine.stdout).strip()[-1000:],
            }
        inspected = _run([resolved, "image", "inspect", image], timeout)
        return {
            "healthy": inspected.returncode == 0,
            "cli_healthy": True,
            "engine_healthy": True,
            "binary": resolved,
            "version": version.stdout.strip().splitlines()[0][:300],
            "server_version": engine.stdout.strip()[:300],
            "image": image,
            "image_present": inspected.returncode == 0,
            "error": "" if inspected.returncode == 0 else (inspected.stderr or inspected.stdout).strip()[-1000:],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "healthy": False,
            "binary": resolved,
            "image": image,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def _native_container_workers(
    docker: dict[str, Any],
    adapters: dict[str, str],
    timeout: int = 20,
) -> dict[str, Any]:
    """Verify the exact CLI binaries inside the configured Kali image."""

    if not docker.get("healthy"):
        return {
            name: {
                "healthy": False,
                "adapter": adapter,
                "error": "Kali image is not ready",
            }
            for name, adapter in adapters.items()
        }
    commands = {"claude-code": "claude", "codex-cli": "codex", "pi-cli": "pi"}
    binary = str(docker["binary"])
    image = str(docker["image"])
    checks: dict[str, Any] = {}
    for name, adapter in adapters.items():
        command = commands[adapter]
        try:
            result = _run(
                [
                    binary,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    image,
                    command,
                    "--version",
                ],
                timeout,
            )
            output = (result.stdout or result.stderr).strip().splitlines()
            checks[name] = {
                "healthy": result.returncode == 0,
                "adapter": adapter,
                "binary": command,
                "version": output[0][:300] if output else "",
                "error": "" if result.returncode == 0 else (result.stderr or result.stdout)[-800:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks[name] = {
                "healthy": False,
                "adapter": adapter,
                "binary": command,
                "error": f"{type(exc).__name__}: {exc}"[:800],
            }
    return checks


def collect_diagnostics(
    config_path: str | Path,
    docker_binary: str = "docker",
    docker_image: str = DEFAULT_WORKER_IMAGE,
) -> dict[str, Any]:
    started = time.time()
    configured: dict[str, str] = {}
    model_health: dict[str, dict[str, Any]] = {}
    try:
        _, pool = load_dispatch_config(config_path, lambda project_id: None)
        configured = {
            item["name"]: pool.get(item["name"]).mind.config.adapter
            for item in pool.snapshot()
        }
        model_health = pool.healthcheck_all()
        config_error = ""
    except Exception as exc:
        config_error = f"{type(exc).__name__}: {exc}"[:1000]
    docker = _docker_health(docker_binary, docker_image)
    workers = (
        _native_container_workers(docker, configured)
        if not config_error
        else {}
    )
    native_ready = bool(workers) and all(item.get("healthy") for item in workers.values())
    healthy_modes = {
        mode
        for item in pool.snapshot()
        if item["healthy"]
        for mode in item["task_types"]
    } if not config_error else set()
    model_ready = REQUIRED_TASK_MODES <= healthy_modes
    return {
        "version": __version__,
        "checked_at": started,
        "python": {
            "healthy": sys.version_info >= (3, 10),
            "version": sys.version.split()[0],
            "executable": sys.executable,
        },
        "workers": workers,
        "model_health": model_health,
        "dispatch_config_error": config_error,
        "docker": docker,
        "ready": {
            "native_container": native_ready and bool(docker.get("healthy")),
            "model_workers": model_ready,
            "service": native_ready and bool(docker.get("healthy")) and model_ready,
        },
        "notes": [
            "CLI checks run inside the Kali image.",
            "Configured explicit model Workers receive a bounded real endpoint probe without a native CLI session.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Cairn-style Kali Native Agent runtime")
    parser.add_argument("--config", required=True, help="native dispatch JSON")
    parser.add_argument("--docker-binary", default=os.environ.get("SLIME_DOCKER_BINARY", "docker"))
    parser.add_argument("--docker-image", default=DEFAULT_WORKER_IMAGE)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = collect_diagnostics(args.config, args.docker_binary, args.docker_image)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and not report["ready"]["service"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
