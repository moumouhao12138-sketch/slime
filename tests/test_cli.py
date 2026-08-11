from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import socket
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from slime_cairn.cli import (
    ApiClient,
    CliError,
    COMMANDS,
    DEFAULT_API_URL,
    SlimeCli,
    build_parser,
    main,
)


class FakeApiClient:
    def __init__(self, responses: dict[tuple[str, str], dict] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        key = (method, path)
        self.calls.append((method, path, payload))
        if key not in self.responses:
            raise AssertionError(f"unexpected API request: {key}")
        return self.responses[key]


class CliTests(unittest.TestCase):
    @staticmethod
    def args(*values: str):
        return build_parser().parse_args(list(values))

    @staticmethod
    def run_cli(args, client: FakeApiClient) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            result = SlimeCli(args, client).run()
        if result != 0:
            raise AssertionError(f"unexpected exit code: {result}")
        return output.getvalue()

    def test_parser_exposes_only_api_client_commands(self):
        self.assertEqual(
            COMMANDS,
            (
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
            ),
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["up"])
        self.assertEqual(raised.exception.code, 2)

    def test_parser_keeps_windows_and_posix_option_spellings(self):
        legacy = self.args(
            "retry",
            "-Name",
            "fixture",
            "-IntentId",
            "intent-1",
            "-BaseUrl",
            "http://api:9000",
        )
        modern = self.args(
            "retry",
            "--name",
            "fixture",
            "--intent-id",
            "intent-1",
            "--base-url",
            "http://api:9000",
        )
        self.assertEqual(legacy.name, modern.name)
        self.assertEqual(legacy.intent_id, modern.intent_id)
        self.assertEqual(legacy.base_url, modern.base_url)

    def test_base_url_uses_environment_then_explicit_option(self):
        with patch.dict(os.environ, {"SLIME_API_URL": "http://api:8080"}):
            from_environment = build_parser().parse_args(["list"])
            explicit = build_parser().parse_args(
                ["list", "--base-url", "https://other.example/api/"]
            )
        self.assertEqual(from_environment.base_url, "http://api:8080")
        self.assertEqual(explicit.base_url, "https://other.example/api/")
        self.assertEqual(
            build_parser().parse_args(["list"]).base_url,
            os.environ.get("SLIME_API_URL", DEFAULT_API_URL),
        )

    def test_api_client_sends_json_with_configured_timeout(self):
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with patch("slime_cairn.cli.urlopen", return_value=response) as open_url:
            result = ApiClient("http://api:8080/", 2.5).request(
                "POST", "projects", {"name": "fixture"}
            )

        self.assertEqual(result, {"ok": True})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://api:8080/projects")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"name": "fixture"})
        self.assertEqual(open_url.call_args.kwargs["timeout"], 2.5)

    def test_api_client_reports_fastapi_error_detail(self):
        error = HTTPError(
            "http://api/projects",
            409,
            "Conflict",
            None,
            io.BytesIO(b'{"detail":"project is deleting"}'),
        )
        with patch("slime_cairn.cli.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                CliError, r"API DELETE /projects/p1 failed \(409\): project is deleting"
            ):
                ApiClient("http://api").request("DELETE", "/projects/p1")

    def test_api_client_reports_timeout_and_connection_errors(self):
        with patch(
            "slime_cairn.cli.urlopen", side_effect=URLError(socket.timeout("late"))
        ):
            with self.assertRaisesRegex(CliError, "timed out after 3s"):
                ApiClient("http://api", 3).request("GET", "/projects")

        with patch(
            "slime_cairn.cli.urlopen", side_effect=URLError("connection refused")
        ):
            with self.assertRaisesRegex(CliError, "docker compose is running"):
                ApiClient("http://api").request("GET", "/projects")

    def test_api_client_rejects_invalid_url_and_non_json_response(self):
        with self.assertRaisesRegex(CliError, "Invalid API URL"):
            ApiClient("localhost:8000")

        response = MagicMock()
        response.read.return_value = b"not json"
        response.__enter__.return_value = response
        with patch("slime_cairn.cli.urlopen", return_value=response):
            with self.assertRaisesRegex(CliError, "returned invalid JSON"):
                ApiClient("http://api").request("GET", "/projects")

    def test_new_creates_project_with_growth_or_direct_start_mode(self):
        for mode, bootstrap_enabled in (("growth", False), ("direct", True)):
            with self.subTest(mode=mode):
                client = FakeApiClient(
                    {
                        ("POST", "/projects"): {
                            "project": {
                                "id": "project-1",
                                "name": "fixture",
                                "target": "https://target.example/",
                                "status": "running",
                            }
                        }
                    }
                )
                output = self.run_cli(
                    self.args(
                        "new",
                        "--name",
                        "fixture",
                        "--target",
                        "https://target.example/",
                        "--goal",
                        "Finish the task",
                        "--start-mode",
                        mode,
                    ),
                    client,
                )
                payload = client.calls[0][2]
                self.assertEqual(payload["bootstrap_enabled"], bootstrap_enabled)
                self.assertEqual(payload["allowed_targets"], ["https://target.example/"])
                self.assertIn("Project created: fixture", output)

    def test_list_prints_projects_from_api(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [
                        {
                            "id": "project-1",
                            "name": "fixture",
                            "target": "target.example",
                            "status": "running",
                        }
                    ]
                }
            }
        )
        output = self.run_cli(self.args("list"), client)
        self.assertIn("STATUS", output)
        self.assertIn("fixture", output)
        self.assertEqual(client.calls, [("GET", "/projects", None)])

    def test_status_resolves_latest_name_and_reads_view(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [
                        {"id": "old", "name": "fixture", "created_at": 1},
                        {"id": "new/id", "name": "fixture", "created_at": 2},
                    ]
                },
                ("GET", "/projects/new%2Fid/view"): {
                    "project": {
                        "id": "new/id",
                        "name": "fixture",
                        "target": "target.example",
                        "status": "running",
                    },
                    "facts": [{"id": "fact-1"}],
                    "summary": {
                        "fact_count": 1,
                        "intent_status": {"failed": 1, "pending": 2},
                        "active_lease_count": 0,
                        "retry_waiting_count": 1,
                    },
                    "active_completion": {"description": "Final result"},
                },
            }
        )
        output = self.run_cli(self.args("status", "--name", "fixture"), client)

        self.assertIn("Facts:           1", output)
        self.assertIn("failed=1, pending=2", output)
        self.assertIn("Result:          Final result", output)
        self.assertEqual(
            client.calls,
            [
                ("GET", "/projects", None),
                ("GET", "/projects/new%2Fid/view", None),
            ],
        )

    def test_status_json_is_a_concise_projection(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [{"id": "p1", "name": "fixture"}]
                },
                ("GET", "/projects/p1/view"): {
                    "project": {
                        "id": "p1",
                        "name": "fixture",
                        "target": "target.example",
                        "status": "stopped",
                    },
                    "summary": {"fact_count": 7, "intent_status": {"done": 3}},
                    "worker_runs": [{"large": "record"}],
                    "active_completion": None,
                },
            }
        )
        output = self.run_cli(
            self.args("status", "--name", "fixture", "--json"), client
        )
        payload = json.loads(output)
        self.assertEqual(payload["fact_count"], 7)
        self.assertEqual(payload["intent_status"], {"done": 3})
        self.assertNotIn("worker_runs", payload)

    def test_runtime_reads_runtime_endpoint(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [{"id": "p1", "name": "fixture"}]
                },
                ("GET", "/projects/p1/runtime"): {
                    "intent_status": {"running": 1}
                },
            }
        )
        output = self.run_cli(self.args("runtime", "--name", "fixture"), client)
        self.assertEqual(json.loads(output), {"intent_status": {"running": 1}})

    def test_pause_stop_and_resume_use_status_endpoint(self):
        for command, expected in (
            ("pause", "stopped"),
            ("stop", "stopped"),
            ("resume", "running"),
        ):
            with self.subTest(command=command):
                client = FakeApiClient(
                    {
                        ("GET", "/projects"): {
                            "projects": [{"id": "p1", "name": "fixture"}]
                        },
                        ("PUT", "/projects/p1/status"): {
                            "project": {"name": "fixture", "status": expected}
                        },
                    }
                )
                output = self.run_cli(
                    self.args(command, "--name", "fixture"), client
                )
                self.assertIn(f"fixture -> {expected}", output)
                self.assertEqual(
                    client.calls[-1],
                    ("PUT", "/projects/p1/status", {"status": expected}),
                )

    def test_delete_uses_project_api(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [{"id": "p1", "name": "fixture"}]
                },
                ("DELETE", "/projects/p1"): {"accepted": True},
            }
        )
        output = self.run_cli(self.args("delete", "--name", "fixture"), client)
        self.assertIn("Project deletion requested: fixture", output)

    def test_retry_resolves_project_and_posts_encoded_intent(self):
        client = FakeApiClient(
            {
                ("GET", "/projects"): {
                    "projects": [{"id": "p1", "name": "fixture"}]
                },
                ("POST", "/projects/p1/intents/intent%2F1/retry"): {
                    "retried": True,
                    "intent": {"id": "intent/1", "status": "pending"},
                },
            }
        )
        output = self.run_cli(
            self.args(
                "retry",
                "--name",
                "fixture",
                "--intent-id",
                "intent/1",
            ),
            client,
        )
        self.assertIn("Intent queued for retry: intent/1", output)
        self.assertEqual(
            client.calls[-1],
            ("POST", "/projects/p1/intents/intent%2F1/retry", None),
        )

    def test_missing_project_and_intent_have_clear_errors(self):
        client = FakeApiClient({("GET", "/projects"): {"projects": []}})
        with self.assertRaisesRegex(CliError, "Project name not found: missing"):
            SlimeCli(self.args("status", "--name", "missing"), client).run()

        with self.assertRaisesRegex(CliError, "Intent ID .* is required"):
            SlimeCli(self.args("retry", "--name", "fixture"), client).run()

    def test_main_prints_help_without_contacting_api(self):
        output = io.StringIO()
        with redirect_stdout(output), patch(
            "slime_cairn.cli.ApiClient", side_effect=AssertionError("must not connect")
        ):
            main(["help"])
        self.assertIn("Manage Slime projects", output.getvalue())
        self.assertIn("retry", output.getvalue())


if __name__ == "__main__":
    unittest.main()
