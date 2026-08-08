from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from slime_cairn.execution import CommandExecution  # noqa: E402
from slime_cairn.worker_manager import STANDARD_PROFILE, WorkerManager  # noqa: E402


def execute(argv: list[str]) -> CommandExecution:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    return CommandExecution(completed.returncode, completed.stdout, completed.stderr)


def main() -> int:
    project_id = f"v021-smoke-{uuid4().hex[:8]}"
    temporary = tempfile.TemporaryDirectory(prefix="slime-cairn-native-smoke-")
    backend = None
    cleanup: dict[str, int] = {}
    try:
        manager = WorkerManager(temporary.name)
        status = manager.ensure_started(project_id, execute, STANDARD_PROFILE)
        backend = manager.backend_for(project_id, STANDARD_PROFILE)
        pod = backend.workspace_root / "pods" / "integration-smoke"
        pod.mkdir(parents=True, exist_ok=True)

        result = backend.execute_with_environment(
            [
                "python3",
                "-c",
                "import json,os; from pathlib import Path; "
                "Path('evidence').mkdir(exist_ok=True); "
                "Path('evidence/result.txt').write_text('ok', encoding='utf-8'); "
                "print(json.dumps({'secret_seen': os.getenv('SLIME_SMOKE_SECRET') == 'dummy-value'}))",
            ],
            pod,
            timeout=30,
            environment={"SLIME_SMOKE_SECRET": "dummy-value"},
        )
        manifest_result = backend.execute(
            [
                "python3",
                "-c",
                "import json; print(len(json.load(open('/opt/slime-cairn/tools.json'))))",
            ],
            backend.workspace_root,
            timeout=30,
        )
        evidence = pod / "evidence" / "result.txt"
        report = {
            "container_status": status,
            "exec_exit_code": result.exit_code,
            "exec_result": json.loads(result.stdout or "{}"),
            "evidence_round_trip": evidence.read_text(encoding="utf-8") == "ok",
            "manifest_tools": int((manifest_result.stdout or "0").strip() or 0),
            "profile": manager.profiles[project_id].name,
        }
        print(json.dumps(report, ensure_ascii=False))
        return 0 if all(
            (
                result.exit_code == 0,
                manifest_result.exit_code == 0,
                report["exec_result"].get("secret_seen") is True,
                report["evidence_round_trip"] is True,
                report["manifest_tools"] > 0,
            )
        ) else 1
    finally:
        if backend is not None:
            cleanup["stop"] = execute(backend.stop_command()).exit_code
            cleanup["remove"] = execute(backend.remove_command()).exit_code
            if any(cleanup.values()):
                print(json.dumps({"cleanup": cleanup}), file=sys.stderr)
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
