from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from . import __version__
from .blackboard import Blackboard
from .dispatcher import AsyncDispatcher, REQUIRED_TASK_MODES, load_dispatch_config
from .runtime_factory import DockerCairnRuntimeFactory
from .scheduler import Scheduler
from .seeding import seed_project_context_facts
from .worker_manager import (
    ContainerResourceLimits,
    active_agent_config_mounts,
    parse_agent_config_mounts,
)


async def run_authorized_e2e(args: argparse.Namespace) -> dict[str, Any]:
    if not args.authorized:
        raise PermissionError("蹇呴』鏄惧紡浼犲叆 --authorized锛岀‘璁ょ洰鏍囧睘浜庝綘鐨勯澏鍦?CTF 鎺堟潈鑼冨洿")

    database = Path(args.database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    board = Blackboard(database)
    binding = None
    try:
        config_file = Path(args.config).resolve()
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        container_config = dict(config_data.get("container") or {})
        agent_config_mounts = parse_agent_config_mounts(
            active_agent_config_mounts(
                container_config.get("agent_config_mounts"),
                config_data.get("workers"),
            ),
            base_directory=config_file.parent,
        )
        factory = DockerCairnRuntimeFactory(
            board,
            args.workspaces_root,
            profile=args.profile,
            docker_binary=args.docker_binary,
            lab_network=args.lab_network,
            stop_on_cleanup=args.stop_worker,
            agent_config_mounts=agent_config_mounts,
            resource_limits=ContainerResourceLimits.from_mapping(container_config),
        )
        native_resolver = lambda project_id: factory.manager.backend_for(
            project_id,
            factory.profile,
        )
        config, pool = load_dispatch_config(args.config, native_resolver)
        health = await asyncio.to_thread(pool.healthcheck_all)
        healthy_modes = pool.configured_task_types(healthy_only=True)
        missing = REQUIRED_TASK_MODES - healthy_modes
        if missing:
            raise RuntimeError(f"缂哄皯鍋ュ悍鐨?Worker 妯″紡: {sorted(missing)}")

        project = board.create_project(
            args.name,
            args.target,
            args.goal,
            {
                "targets": [args.target],
                "authorization": "explicit-cli-confirmation",
                "bootstrap_enabled": "bootstrap" in healthy_modes,
            },
        )
        seed_project_context_facts(board, project)
        binding = await asyncio.to_thread(factory, project)
        scheduler = Scheduler(
            board,
            project.id,
            mind=pool.any_mind(),
            workspace=binding.workspace,
        )
        if "bootstrap" in healthy_modes:
            scheduler.seed(project.target)
        dispatcher = AsyncDispatcher(board, project.id, scheduler, pool, config)
        dispatcher_result = await dispatcher.run_until_idle(max_cycles=args.max_cycles)
        return {
            "version": __version__,
            "project": asdict(project),
            "dispatcher": {
                "state": dispatcher_result["state"],
                "completed_workers": dispatcher_result["completed_workers"],
                "completed_reason_runs": dispatcher_result["completed_reason_runs"],
                "errors": dispatcher_result["errors"],
                "aborted": dispatcher_result.get("aborted", {}),
            },
            "worker_health": health,
            "facts": [asdict(item) for item in board.list_facts(project.id)],
            "hypotheses": [asdict(item) for item in board.list_hypotheses(project.id)],
            "intents": [asdict(item) for item in board.list_intents(project.id)],
            "worker_runs": board.list_worker_runs(project.id),
            "database": str(database),
        }
    finally:
        if binding is not None and binding.cleanup is not None:
            await asyncio.to_thread(binding.cleanup)
        board.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="鍦ㄦ槑纭巿鏉冪殑闈跺満涓繍琛岀湡瀹?CLI/Native Agent 鈫?Kali Worker 鈫?榛戞澘闂幆"
    )
    parser.add_argument("--target", required=True, help="鍞竴鎺堟潈鐩爣锛涗笉浼氳嚜鍔ㄦ墿灞?Scope")
    parser.add_argument("--config", required=True, help="鍖呭惈 bootstrap/explore/reason Worker 鐨?dispatch JSON")
    parser.add_argument("--authorized", action="store_true", help="纭璇ョ洰鏍囧睘浜庝綘鐨勬巿鏉冮澏鍦烘垨 CTF")
    parser.add_argument("--name", default="authorized-e2e")
    parser.add_argument("--goal", default="鍦ㄦ巿鏉冭寖鍥村唴鏀堕泦璇佹嵁銆侀獙璇佸亣璁惧苟瀹屾垚鐩爣")
    parser.add_argument("--database", default="slime-cairn-e2e.db")
    parser.add_argument("--workspaces-root", default="slime-workspaces")
    parser.add_argument("--profile", choices=["standard", "raw-network", "lab-network-admin"], default="raw-network")
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--lab-network", default="slime-cairn-lab")
    parser.add_argument("--max-cycles", type=int, default=10000)
    parser.add_argument("--stop-worker", action="store_true")
    parser.add_argument("--output", help="鍙€?JSON 缁撴灉鏂囦欢")
    args = parser.parse_args()
    result = asyncio.run(run_authorized_e2e(args))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()




