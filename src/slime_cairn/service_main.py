from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .benchmark import BenchmarkClient, BenchmarkSettings
from .benchmark_automation import BenchmarkAutomationService
from .blackboard import Blackboard
from .dispatcher import load_dispatch_config
from .runtime_factory import DockerCairnRuntimeFactory
from .service import DispatcherService
from .worker_manager import (
    ContainerResourceLimits,
    active_agent_config_mounts,
    parse_agent_config_mounts,
)


def build_service() -> tuple[Blackboard, DispatcherService]:
    board = Blackboard(os.environ.get("SLIME_CAIRN_DB", "slime-cairn.db"))
    try:
        config_path = os.environ.get("SLIME_DISPATCH_CONFIG", "").strip()
        if not config_path:
            raise ValueError("SLIME_DISPATCH_CONFIG is required for Cairn native runtime")
        runtime_mode = os.environ.get("SLIME_RUNTIME_MODE", "docker").strip().lower()
        if runtime_mode != "docker":
            raise ValueError("SLIME_RUNTIME_MODE must be docker")
        config_file = Path(config_path).resolve()
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        container_config = dict(config_data.get("container") or {})
        agent_config_mounts = parse_agent_config_mounts(
            active_agent_config_mounts(
                container_config.get("agent_config_mounts"),
                config_data.get("workers"),
            ),
            base_directory=config_file.parent,
        )
        resource_limits = ContainerResourceLimits.from_mapping(container_config)
        factory = DockerCairnRuntimeFactory(
            board,
            os.environ.get("SLIME_WORKSPACES_ROOT", "slime-workspaces"),
            profile=os.environ.get("SLIME_WORKER_PROFILE", "standard"),
            docker_binary=os.environ.get("SLIME_DOCKER_BINARY", "docker"),
            lab_network=os.environ.get("SLIME_LAB_NETWORK", "slime-cairn-lab"),
            stop_on_cleanup=os.environ.get("SLIME_STOP_WORKERS_ON_EXIT", "0") == "1",
            agent_config_mounts=agent_config_mounts,
            resource_limits=resource_limits,
        )
        native_resolver = lambda project_id: factory.manager.backend_for(
            project_id,
            factory.profile,
        )
        config, pool = load_dispatch_config(config_path, native_resolver)
    except Exception:
        board.close()
        raise

    service = DispatcherService(
        board,
        pool,
        factory,
        config,
        discovery_interval=float(os.environ.get("SLIME_PROJECT_DISCOVERY_INTERVAL", "0.25")),
    )
    benchmark_settings = BenchmarkSettings.from_env()
    if benchmark_settings.configured:
        service.benchmark_automation = BenchmarkAutomationService(
            board,
            BenchmarkClient(benchmark_settings),
            benchmark_settings,
            interval=float(os.environ.get("BENCHMARK_AUTOMATION_INTERVAL", "3")),
        )
    return board, service


async def run_service(service: DispatcherService) -> dict:
    automation = getattr(service, "benchmark_automation", None)
    automation_task = (
        asyncio.create_task(automation.serve(), name="slime-benchmark-automation")
        if automation is not None
        else None
    )
    try:
        return await service.serve()
    finally:
        if automation is not None:
            automation.request_stop()
        if automation_task is not None:
            await asyncio.gather(automation_task, return_exceptions=True)


def main() -> None:
    board, service = build_service()
    try:
        asyncio.run(run_service(service))
    except KeyboardInterrupt:
        service.request_stop()
    finally:
        board.close()


if __name__ == "__main__":
    main()
