from __future__ import annotations

import argparse
import json
import os
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


COMMANDS = (
    "help",
    "new",
    "list",
    "status",
    "runtime",
    "pause",
    "stop",
    "resume",
    "delete",
    "retry",
)
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_GOAL = (
    "AI pseudopods analyze target evidence, solve the task, and Reason returns "
    "the final result."
)


class CliError(RuntimeError):
    """A user-facing command error."""


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def _normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliError(
            "Invalid API URL. Use an absolute http(s) URL, for example "
            f"{DEFAULT_API_URL}."
        )
    if parsed.query or parsed.fragment:
        raise CliError("API URL must not contain a query string or fragment.")
    return base_url


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "no response body"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return " ".join(text.split())[:300]
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)[:300]


class ApiClient:
    """Small JSON client for an already-running Slime control plane."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = _normalize_base_url(base_url)
        if timeout <= 0:
            raise CliError("API timeout must be greater than zero.")
        self.timeout = float(timeout)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = "/" + path.lstrip("/")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            self.base_url + path,
            data=body,
            method=method.upper(),
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = _error_detail(exc.read())
            raise CliError(
                f"API {method.upper()} {path} failed ({exc.code}): {detail}"
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise CliError(
                    f"API {method.upper()} {path} timed out after {self.timeout:g}s."
                ) from exc
            raise CliError(
                f"Cannot reach Slime API at {self.base_url}: {exc.reason}. "
                "Check that docker compose is running."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise CliError(
                f"API {method.upper()} {path} timed out after {self.timeout:g}s."
            ) from exc
        except (OSError, ValueError) as exc:
            raise CliError(
                f"Cannot reach Slime API at {self.base_url}: {exc}. "
                "Check that docker compose is running."
            ) from exc

        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CliError(
                f"API {method.upper()} {path} returned invalid JSON."
            ) from exc
        if not isinstance(result, dict):
            raise CliError(f"API {method.upper()} {path} returned a non-object response.")
        return result


class SlimeCli:
    def __init__(
        self,
        args: argparse.Namespace,
        client: ApiClient | None = None,
    ) -> None:
        self.args = args
        self.client = client or ApiClient(args.base_url, args.timeout)

    def run(self) -> int:
        actions = {
            "help": self.show_help,
            "new": self.new_project,
            "list": self.list_projects,
            "status": self.status,
            "runtime": self.runtime,
            "pause": lambda: self.set_project_status("stopped"),
            "stop": lambda: self.set_project_status("stopped"),
            "resume": lambda: self.set_project_status("running"),
            "delete": self.delete_project,
            "retry": self.retry_intent,
        }
        actions[self.args.command]()
        return 0

    def show_help(self) -> None:
        build_parser().print_help()

    def new_project(self) -> None:
        name = self._required(self.args.name, "Project name")
        target = self._required(
            self.args.target,
            "Target (use --target, for example https://target.example/)",
        )
        goal = self._required(self.args.goal, "Project goal")
        payload = {
            "name": name,
            "target": target,
            "goal": goal,
            "allowed_targets": [target],
            "start_mode": self.args.start_mode,
            # Keep the legacy field for older API deployments.
            "bootstrap_enabled": self.args.start_mode in {"direct", "hybrid"},
        }
        result = self.client.request("POST", "/projects", payload)
        project = self._mapping(result, "project", "create project")
        if self.args.json:
            self._print_json(result)
            return
        print(f"Project created: {project.get('name', name)}")
        print(f"ID:              {project.get('id', '')}")
        print(f"Status:          {project.get('status', '')}")
        print(f"Target:          {project.get('target', target)}")

    def list_projects(self) -> None:
        result = self.client.request("GET", "/projects")
        projects = result.get("projects")
        if not isinstance(projects, list):
            raise CliError("API list projects response is missing 'projects'.")
        if self.args.json:
            self._print_json({"projects": projects})
            return
        if not projects:
            print("No projects.")
            return
        print(f"{'STATUS':10} {'NAME':24} {'ID':28} TARGET")
        for project in projects:
            if not isinstance(project, dict):
                raise CliError("API list projects response contains an invalid project.")
            print(
                f"{str(project.get('status', '')):10} "
                f"{str(project.get('name', '')):24} "
                f"{str(project.get('id', '')):28} "
                f"{project.get('target', '')}"
            )

    def status(self) -> None:
        project_id = self._project_id(self.args.name)
        view = self.client.request("GET", self._project_path(project_id, "/view"))
        summary = self._status_summary(view)
        if self.args.json:
            self._print_json(summary)
            return
        print(f"Name:            {summary['name']}")
        print(f"ID:              {summary['id']}")
        print(f"Status:          {summary['status']}")
        print(f"Target:          {summary['target']}")
        print(f"Facts:           {summary['fact_count']}")
        print(f"Intents:         {self._format_counts(summary['intent_status'])}")
        print(f"Active leases:   {summary['active_lease_count']}")
        print(f"Retries waiting: {summary['retry_waiting_count']}")
        result = summary.get("result")
        print(f"Result:          {self._one_line(result) if result else 'pending'}")

    def runtime(self) -> None:
        project_id = self._project_id(self.args.name)
        result = self.client.request("GET", self._project_path(project_id, "/runtime"))
        self._print_json(result)

    def set_project_status(self, status: str) -> None:
        project_id = self._project_id(self.args.name)
        result = self.client.request(
            "PUT",
            self._project_path(project_id, "/status"),
            {"status": status},
        )
        project = self._mapping(result, "project", "update project status")
        if self.args.json:
            self._print_json(result)
        else:
            print(f"Project {project.get('name', self.args.name)} -> {project.get('status', status)}")

    def delete_project(self) -> None:
        project_id = self._project_id(self.args.name)
        result = self.client.request("DELETE", self._project_path(project_id))
        if self.args.json:
            self._print_json(result)
        elif result.get("accepted"):
            print(f"Project deletion requested: {self.args.name}")
        else:
            print(f"Project deletion already in progress: {self.args.name}")

    def retry_intent(self) -> None:
        intent_id = self._required(self.args.intent_id, "Intent ID (use --intent-id)")
        project_id = self._project_id(self.args.name)
        path = self._project_path(
            project_id,
            f"/intents/{quote(intent_id, safe='')}/retry",
        )
        result = self.client.request("POST", path)
        intent = self._mapping(result, "intent", "retry intent")
        if self.args.json:
            self._print_json(result)
        else:
            print(f"Intent queued for retry: {intent.get('id', intent_id)}")
            print(f"Status:                  {intent.get('status', '')}")

    def _project_id(self, name: str) -> str:
        name = self._required(name, "Project name")
        result = self.client.request("GET", "/projects")
        projects = result.get("projects")
        if not isinstance(projects, list):
            raise CliError("API list projects response is missing 'projects'.")
        matches = [
            project
            for project in projects
            if isinstance(project, dict) and project.get("name") == name
        ]
        if not matches:
            raise CliError(f"Project name not found: {name}")

        def created_at(project: dict[str, Any]) -> float:
            try:
                return float(project.get("created_at") or 0)
            except (TypeError, ValueError):
                return 0.0

        project_id = max(matches, key=created_at).get("id")
        if not project_id:
            raise CliError(f"Project '{name}' has no ID in the API response.")
        return str(project_id)

    @staticmethod
    def _project_path(project_id: str, suffix: str = "") -> str:
        return f"/projects/{quote(project_id, safe='')}{suffix}"

    @staticmethod
    def _required(value: object, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise CliError(f"{label} is required.")
        return text

    @staticmethod
    def _mapping(result: dict[str, Any], key: str, action: str) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise CliError(f"API {action} response is missing '{key}'.")
        return value

    @classmethod
    def _status_summary(cls, view: dict[str, Any]) -> dict[str, Any]:
        project = cls._mapping(view, "project", "project view")
        raw_summary = view.get("summary")
        summary = raw_summary if isinstance(raw_summary, dict) else {}
        raw_intents = summary.get("intent_status")
        intent_status = raw_intents if isinstance(raw_intents, dict) else {}
        completion = view.get("active_completion")
        result = completion.get("description") if isinstance(completion, dict) else None
        facts = view.get("facts")
        return {
            "id": project.get("id", ""),
            "name": project.get("name", ""),
            "target": project.get("target", ""),
            "status": project.get("status", ""),
            "fact_count": summary.get(
                "fact_count", len(facts) if isinstance(facts, list) else 0
            ),
            "intent_status": intent_status,
            "active_lease_count": summary.get("active_lease_count", 0),
            "retry_waiting_count": summary.get("retry_waiting_count", 0),
            "result": result,
        }

    @staticmethod
    def _format_counts(counts: dict[str, Any]) -> str:
        if not counts:
            return "none"
        return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))

    @staticmethod
    def _one_line(value: object, limit: int = 240) -> str:
        text = " ".join(str(value).split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    @staticmethod
    def _print_json(value: dict[str, Any]) -> None:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slime",
        allow_abbrev=False,
        description="Manage Slime projects through the containerized HTTP API.",
        epilog=(
            "Deployment is managed by Docker Compose. Set SLIME_API_URL or use "
            "--base-url when the API is not on 127.0.0.1:8000."
        ),
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="help")
    parser.add_argument("--name", default="ctf-test", help="project name")
    parser.add_argument("--target", default="", help="project target")
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="project goal")
    parser.add_argument(
        "--start-mode",
        choices=("growth", "direct", "hybrid"),
        default="growth",
        help="create with Reason growth, a bootstrap Intent, or bootstrap then growth",
    )
    parser.add_argument("--intent-id", default="", help="Intent to retry")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SLIME_API_URL", DEFAULT_API_URL),
        help=f"API URL (default: SLIME_API_URL or {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return
    try:
        SlimeCli(args).run()
    except CliError as exc:
        parser.exit(2, f"slime: {exc}\n")


if __name__ == "__main__":
    main()
