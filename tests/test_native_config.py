from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slime_cairn.dispatcher.loop import load_dispatch_config
from slime_cairn.workers.native import NativeAgentMind


class NativeDispatchConfigTests(unittest.TestCase):
    def test_dispatcher_defaults_bound_retries_and_probe_workers_at_startup(self):
        from slime_cairn.dispatcher.loop import DispatcherConfig

        config = DispatcherConfig()

        self.assertEqual(config.max_intent_attempts, 6)
        self.assertEqual(config.intent_failure_backoff_seconds, 30)
        self.assertEqual(config.intent_failure_backoff_max_seconds, 1800)
        self.assertEqual(config.worker_healthcheck, "startup_only")
        self.assertEqual(config.worker_rejected_cooldown_seconds, 5)
        self.assertEqual(config.reason_failure_cooldown_seconds, 5)
        self.assertEqual(config.reason_failure_backoff_max_seconds, 5)
        self.assertEqual(config.reason_failure_pause_threshold, 3)
        self.assertEqual(config.reason_failure_pause_seconds, 60)
        self.assertEqual(config.reason_failure_pause_max_seconds, 300)
        self.assertEqual(config.growth_selection_mode, "sma_discrete")
        self.assertEqual(config.growth_exploration_probability, 0.12)
        self.assertEqual(config.growth_exploration_min_probability, 0.03)
        self.assertEqual(config.growth_convergence_selections, 24)
        self.assertEqual(config.growth_random_seed, "slime-sma-v1")

        with patch.dict("os.environ", {}, clear=True):
            from_environment = DispatcherConfig.from_env()
        self.assertEqual(from_environment.max_intent_attempts, 6)
        self.assertEqual(from_environment.intent_failure_backoff_seconds, 30)
        self.assertEqual(from_environment.intent_failure_backoff_max_seconds, 1800)
        self.assertEqual(from_environment.worker_healthcheck, "startup_only")
        self.assertEqual(from_environment.growth_selection_mode, "sma_discrete")
        self.assertEqual(from_environment.growth_exploration_probability, 0.12)
        self.assertEqual(from_environment.growth_exploration_min_probability, 0.03)
        self.assertEqual(from_environment.growth_convergence_selections, 24)
        self.assertEqual(from_environment.growth_random_seed, "slime-sma-v1")

    def write_config(self, root: Path, workers: list[dict]) -> Path:
        path = root / "dispatch.json"
        path.write_text(
            json.dumps({"runtime": {}, "workers": workers}),
            encoding="utf-8",
        )
        return path

    def test_loads_three_direct_container_cli_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "claude-bootstrap",
                        "type": "claude-code",
                        "execution": "native-agent",
                        "task_types": ["bootstrap"],
                    },
                    {
                        "name": "codex-explore",
                        "type": "codex-cli",
                        "execution": "native-agent",
                        "task_types": ["explore"],
                    },
                    {
                        "name": "pi-reason",
                        "type": "pi-cli",
                        "execution": "native-agent",
                        "task_types": ["reason"],
                    },
                ],
            )
            _, pool = load_dispatch_config(path, lambda project_id: object())

            self.assertEqual(pool.configured_task_types(), {"bootstrap", "explore", "reason"})
            self.assertIsInstance(pool.get("claude-bootstrap").mind, NativeAgentMind)
            self.assertEqual(pool.get("codex-explore").mind.config.binary, "codex")
            self.assertEqual(pool.get("pi-reason").mind.config.adapter, "pi-cli")

    def test_default_config_uses_a_general_capability_pool(self):
        root = Path(__file__).parents[1]
        payload = json.loads((root / "dispatch.json").read_text(encoding="utf-8"))

        workers = payload["workers"]
        expected = {"bootstrap", "explore", "reason"}
        self.assertEqual({worker["name"] for worker in workers}, {"claude-native", "codex-native", "pi-native"})
        self.assertTrue(all(set(worker["task_types"]) == expected for worker in workers))
        enabled = {worker["name"]: worker.get("enabled", True) for worker in workers}
        self.assertTrue(enabled["codex-native"])
        # Optional providers remain defined but opt in through the dispatch file.
        self.assertFalse(enabled["pi-native"])
        self.assertFalse(enabled["claude-native"])
        claude = next(worker for worker in workers if worker["name"] == "claude-native")
        self.assertEqual(claude["max_running"], 2)
        self.assertEqual(claude["priority"], 0)
        self.assertEqual(
            claude["environment"]["CLAUDE_CONFIG_DIR"],
            "/workspace/shared/agent-homes/claude",
        )
        self.assertEqual(
            claude["env"]["ANTHROPIC_AUTH_TOKEN"],
            "${SLIME_CLAUDE_API_KEY|SLIME_LLM_API_KEY}",
        )
        self.assertEqual(payload["runtime"]["worker_healthcheck"], "disabled")
        self.assertEqual(payload["runtime"]["prompt_group"], "default")
        self.assertEqual(payload["runtime"]["max_intent_attempts"], 6)

        self.assertEqual(payload["runtime"]["intent_failure_backoff_seconds"], 30)
        self.assertEqual(payload["runtime"]["intent_failure_backoff_max_seconds"], 1800)
        self.assertEqual(payload["runtime"]["growth_selection_mode"], "sma_discrete")
        self.assertEqual(payload["runtime"]["growth_exploration_probability"], 0.12)
        self.assertEqual(payload["runtime"]["growth_exploration_min_probability"], 0.03)
        self.assertEqual(payload["runtime"]["growth_convergence_selections"], 24)
        self.assertEqual(payload["runtime"]["growth_random_seed"], "slime-sma-v1")
        self.assertNotIn("pids_limit", payload["container"])
        self.assertNotIn("memory", payload["container"])
        self.assertNotIn("cpus", payload["container"])
        self.assertTrue(payload["container"]["init"])
        self.assertNotIn("credential_source", payload["container"])
        self.assertNotIn("agent_config_mounts", payload["container"])
        pi = next(worker for worker in workers if worker["name"] == "pi-native")
        self.assertEqual(pi["env"]["PI_MODEL"], "${SLIME_PI_MODEL|SLIME_LLM_MODEL}")
        self.assertEqual(pi["env"]["PI_PROVIDER_API"], "openai-completions")

    def test_prompt_group_is_loaded_and_propagated_to_native_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-all",
                        "type": "codex",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["runtime"] = {"prompt_group": "default"}
            path.write_text(json.dumps(payload), encoding="utf-8")

            config, pool = load_dispatch_config(path, lambda project_id: object())

            self.assertEqual(config.prompt_group, "default")
            self.assertEqual(pool.get("codex-all").mind.config.prompt_group, "default")

            payload["runtime"] = {"prompt_group": "missing"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing prompt group"):
                load_dispatch_config(path, lambda project_id: object())

    def test_cairn_tasks_section_controls_each_native_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-all",
                        "type": "codex",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["runtime"] = {
                "interval": 3,
                "lease_seconds": 15,
                "reason_lease_seconds": 15,
            }
            payload["tasks"] = {
                "bootstrap": {"timeout": 101, "conclude_timeout": 17},
                "reason": {"timeout": 79, "max_intents": 2},
                "explore": {"timeout": 131, "conclude_timeout": 19},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            config, pool = load_dispatch_config(path, lambda project_id: object())
            native = pool.get("codex-all").mind.config

            self.assertEqual(config.interval, 3)
            self.assertEqual(config.heartbeat_interval, 3)
            self.assertEqual(config.lease_seconds, 15)
            self.assertEqual(native.timeout_for("bootstrap"), 101)
            self.assertEqual(native.timeout_for("bootstrap", conclude=True), 17)
            self.assertEqual(native.timeout_for("reason"), 79)
            self.assertEqual(native.reason_max_intents, 2)
            self.assertEqual(native.timeout_for("explore"), 131)
            self.assertEqual(native.timeout_for("explore", conclude=True), 19)

    def test_rejects_invalid_growth_selection_configuration(self):
        from slime_cairn.dispatcher.loop import DispatcherConfig

        invalid_cases = [
            ({"growth_selection_mode": "unknown"}, "growth_selection_mode"),
            ({"growth_selection_mode": None}, "growth_selection_mode"),
            ({"growth_exploration_probability": 1.01}, "probabilities"),
            ({"growth_exploration_min_probability": -0.01}, "probabilities"),
            (
                {
                    "growth_exploration_probability": 0.03,
                    "growth_exploration_min_probability": 0.12,
                },
                "probabilities",
            ),
            ({"growth_convergence_selections": 0}, "growth_convergence_selections"),
            ({"growth_random_seed": "  "}, "growth_random_seed"),
            ({"growth_random_seed": None}, "growth_random_seed"),
        ]

        for overrides, message in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    DispatcherConfig(**overrides)

    def test_rejects_every_non_native_execution_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "invalid-worker",
                        "type": "codex-cli",
                        "execution": "legacy-relay",
                        "task_types": ["bootstrap", "explore", "reason"],
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "native-agent"):
                load_dispatch_config(path, lambda project_id: object())

    def test_requires_a_project_container_resolver(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-all",
                        "type": "codex-cli",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "Docker project runtime"):
                load_dispatch_config(path)

    def test_resolves_native_cli_environment_without_putting_values_in_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-all",
                        "type": "codex-cli",
                        "execution": "native-agent",
                        "model_env": "TEST_NATIVE_MODEL",
                        "task_types": ["bootstrap", "explore", "reason"],
                        "env_from": {"OPENAI_API_KEY": ["MISSING_KEY", "TEST_NATIVE_KEY"]},
                        "config_overrides_from_env": {
                            "model_providers.custom.base_url": "TEST_NATIVE_BASE_URL"
                        },
                    }
                ],
            )
            environment = {
                "TEST_NATIVE_MODEL": "model-test",
                "TEST_NATIVE_KEY": "secret-value",
                "TEST_NATIVE_BASE_URL": "https://model.example/v1",
            }
            with patch.dict("os.environ", environment, clear=False):
                _, pool = load_dispatch_config(path, lambda project_id: object())

            config = pool.get("codex-all").mind.config
            self.assertEqual(config.model, "model-test")
            self.assertEqual(config.environment["OPENAI_API_KEY"], "secret-value")
            self.assertIn(
                ("model_providers.custom.base_url", "https://model.example/v1"),
                config.config_overrides,
            )

    def test_cairn_explicit_codex_env_builds_provider_and_endpoint_health_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-explicit",
                        "type": "codex",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                        "env": {
                            "CODEX_MODEL": "${TEST_CODEX_MODEL}",
                            "CODEX_BASE_URL": "${TEST_CODEX_BASE_URL}",
                            "OPENAI_API_KEY": "${TEST_CODEX_API_KEY}",
                        },
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["runtime"] = {"worker_healthcheck": "startup_and_task"}
            payload["common_env"] = {"HTTPS_PROXY": "${TEST_PROXY}"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            environment = {
                "TEST_CODEX_MODEL": "codex-fixture",
                "TEST_CODEX_BASE_URL": "https://models.example/v1",
                "TEST_CODEX_API_KEY": "very-secret-key",
                "TEST_PROXY": "http://proxy.example:8080",
            }
            with patch.dict("os.environ", environment, clear=True):
                config, pool = load_dispatch_config(path, lambda project_id: object())

            native = pool.get("codex-explicit").mind.config
            self.assertEqual(config.worker_healthcheck, "startup_and_task")
            self.assertEqual(native.adapter, "codex-cli")
            self.assertEqual(native.model, "codex-fixture")
            self.assertEqual(native.environment["HTTPS_PROXY"], "http://proxy.example:8080")
            self.assertEqual(native.environment["OPENAI_API_KEY"], "very-secret-key")
            self.assertEqual(native.model_endpoint.base_url, "https://models.example/v1")
            self.assertNotIn("very-secret-key", repr(native.model_endpoint))
            self.assertIn(("model_provider", "slime_cairn"), native.config_overrides)
            self.assertIn(
                ("model_providers.slime_cairn.base_url", "https://models.example/v1"),
                native.config_overrides,
            )

    def test_cairn_explicit_claude_env_builds_anthropic_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "claude-explicit",
                        "type": "claudecode",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                        "env": {
                            "ANTHROPIC_MODEL": "${TEST_CLAUDE_MODEL}",
                            "ANTHROPIC_BASE_URL": "${TEST_CLAUDE_BASE_URL}",
                            "ANTHROPIC_AUTH_TOKEN": "${TEST_CLAUDE_API_KEY}",
                        },
                    }
                ],
            )
            environment = {
                "TEST_CLAUDE_MODEL": "claude-fixture",
                "TEST_CLAUDE_BASE_URL": "https://claude.example",
                "TEST_CLAUDE_API_KEY": "claude-secret-key",
            }
            with patch.dict("os.environ", environment, clear=True):
                _, pool = load_dispatch_config(path, lambda project_id: object())

            native = pool.get("claude-explicit").mind.config
            self.assertEqual(native.adapter, "claude-code")
            self.assertEqual(native.model, "claude-fixture")
            self.assertEqual(native.environment["ANTHROPIC_AUTH_TOKEN"], "claude-secret-key")
            self.assertEqual(native.model_endpoint.base_url, "https://claude.example")
            self.assertEqual(native.model_endpoint.protocol, "anthropic-messages")
            self.assertNotIn("claude-secret-key", repr(native.model_endpoint))

    def test_cairn_explicit_worker_rejects_a_missing_credential_environment_variable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_config(
                Path(temporary),
                [
                    {
                        "name": "codex-explicit",
                        "type": "codex",
                        "execution": "native-agent",
                        "task_types": ["bootstrap", "explore", "reason"],
                        "env": {
                            "CODEX_MODEL": "fixture-model",
                            "CODEX_BASE_URL": "https://models.example/v1",
                            "OPENAI_API_KEY": "${MISSING_CODEX_KEY}",
                        },
                    }
                ],
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "MISSING_CODEX_KEY"):
                    load_dispatch_config(path, lambda project_id: object())


if __name__ == "__main__":
    unittest.main()
