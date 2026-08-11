from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slime_cairn.workers.execution import (
    DEFAULT_WORKER_IMAGE,
    AgentConfigMount,
    CommandExecution,
    PersistentDockerBackend,
    PersistentDockerConfig,
)
from slime_cairn.workers.manager import (
    ContainerResourceLimits,
    LAB_NETWORK_ADMIN_PROFILE,
    RAW_NETWORK_PROFILE,
    STANDARD_PROFILE,
    WorkerManager,
    active_agent_config_mounts,
    parse_agent_config_mounts,
)


class AgentConfigMountTests(unittest.TestCase):
    def test_standard_profile_has_upstream_network_without_extra_capabilities(self):
        self.assertEqual(STANDARD_PROFILE.network, "bridge")
        self.assertEqual(STANDARD_PROFILE.capabilities, ())

    def test_disabled_agent_mounts_do_not_require_host_configuration(self):
        configured = {
            "claude": {
                "source": "missing-claude-home",
                "target": "/host-agent-config/claude",
            }
        }
        workers = [
            {"name": "codex", "type": "codex", "enabled": True},
            {"name": "claude", "type": "claudecode", "enabled": False},
        ]

        active = active_agent_config_mounts(configured, workers)

        self.assertEqual(active, {})
        self.assertEqual(parse_agent_config_mounts(active), ())

    def test_enabled_agent_mount_and_unknown_entries_remain_validated(self):
        configured = {
            "claude-code": {
                "source": "claude-home",
                "target": "/host-agent-config/claude",
            },
            "custom": {
                "source": "custom-home",
                "target": "/host-agent-config/custom",
            },
        }
        workers = [{"name": "claude", "type": "claude", "enabled": True}]

        active = active_agent_config_mounts(configured, workers)

        self.assertEqual(set(active), {"claude-code", "custom"})

    def test_container_limits_are_unbounded_by_default_and_can_be_enabled(self):
        defaults = ContainerResourceLimits.from_mapping({})
        self.assertIsNone(defaults.pids_limit)
        self.assertIsNone(defaults.memory)
        self.assertIsNone(defaults.cpus)
        self.assertTrue(defaults.init)

        bounded = ContainerResourceLimits.from_mapping(
            {"pids_limit": 256, "memory": "2g", "cpus": "1.5", "init": False}
        )
        self.assertEqual(bounded.pids_limit, 256)
        self.assertEqual(bounded.memory, "2g")
        self.assertEqual(bounded.cpus, "1.5")
        self.assertFalse(bounded.init)

        for invalid in (
            {"pids_limit": True},
            {"pids_limit": 0},
            {"memory": "unbounded"},
            {"cpus": 0},
            {"cpus": "nan"},
            {"init": "true"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    ContainerResourceLimits.from_mapping(invalid)

    def test_persistent_container_uses_overlay_tmp(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()

            persistent = PersistentDockerBackend(
                workspace,
                PersistentDockerConfig(container_name="slime-overlay-tmp"),
            ).create_command()

            self.assertNotIn("--tmpfs", persistent)

    def test_parses_keyed_json_mounts_relative_to_config_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("codex", "claude", "pi"):
                (root / "profiles" / name).mkdir(parents=True)

            mounts = parse_agent_config_mounts(
                {
                    "codex-cli": {
                        "source": "profiles/codex",
                        "target": "/host-agent-config/codex",
                    },
                    "claude-code": {
                        "source": "profiles/claude",
                        "target": "/host-agent-config/claude",
                    },
                    "pi-cli": {
                        "source": "profiles/pi",
                        "target": "/host-agent-config/pi",
                    },
                },
                base_directory=root,
            )

            self.assertEqual([mount.agent for mount in mounts], ["codex", "claude", "pi"])
            self.assertEqual(mounts[0].source, (root / "profiles" / "codex").resolve())
            self.assertEqual(mounts[2].target, "/host-agent-config/pi")

    def test_persistent_container_renders_read_only_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            mount = AgentConfigMount("codex", codex_home, "/host-agent-config/codex")
            backend = PersistentDockerBackend(
                root / "workspace",
                PersistentDockerConfig(
                    container_name="slime-agent-mount",
                    agent_config_mounts=(mount,),
                ),
            )

            command = backend.create_command()
            mount_index = command.index("--mount")
            self.assertEqual(
                command[mount_index + 1],
                f"type=bind,source={codex_home.resolve()},target=/host-agent-config/codex,readonly",
            )
            self.assertIn("--read-only", command)

    def test_persistent_container_mounts_one_named_volume_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = PersistentDockerBackend(
                Path(temporary) / "project-one",
                PersistentDockerConfig(
                    container_name="slime-named-volume",
                    workspace_volume="slime-workspaces",
                    workspace_volume_subpath="project-one",
                ),
            )

            command = backend.create_command()
            mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
            self.assertIn(
                "type=volume,source=slime-workspaces,target=/workspace,volume-subpath=project-one",
                mounts,
            )
            self.assertNotIn("--volume", command)

            nested = backend.workspace_root / "pods" / "fixture"
            nested.mkdir(parents=True)
            if os.name == "posix":
                with patch("slime_cairn.workers.execution.os.chown") as chown:
                    backend.prepare_writable_path(nested)
                    pid_path, container_pid_path = backend._pid_file()
                chown.assert_any_call(
                    nested.resolve(),
                    65532,
                    65532,
                    follow_symlinks=False,
                )
                chown.assert_any_call(
                    pid_path.parent.resolve(),
                    65532,
                    65532,
                    follow_symlinks=False,
                )
            else:
                backend.prepare_writable_path(nested)
                pid_path, container_pid_path = backend._pid_file()
            self.assertEqual(pid_path.parent.name, ".slime-cairn-runtime")
            self.assertTrue(container_pid_path.startswith("/workspace/.slime-cairn-runtime/"))
            with self.assertRaises(ValueError):
                backend.prepare_writable_path(Path(temporary).resolve())

    def test_worker_manager_propagates_mounts_and_locks_them_after_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "codex-profile"
            profile.mkdir()
            mount = AgentConfigMount("codex", profile, "/host-agent-config/codex")
            manager = WorkerManager(root / "workspaces", agent_config_mounts=(mount,))

            create = " ".join(manager.lifecycle_plan("project-one")["create"])
            self.assertIn("target=/host-agent-config/codex,readonly", create)
            with self.assertRaises(ValueError):
                manager.set_agent_config_mounts(())

    def test_worker_manager_propagates_explicit_container_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = WorkerManager(
                Path(temporary) / "workspaces",
                resource_limits=ContainerResourceLimits(
                    pids_limit=256,
                    memory="2g",
                    cpus="1.5",
                    init=False,
                ),
            )

            create = manager.lifecycle_plan("limited-project")["create"]
            self.assertNotIn("--init", create)
            self.assertEqual(create[create.index("--pids-limit") + 1], "256")
            self.assertEqual(create[create.index("--memory") + 1], "2g")
            self.assertEqual(create[create.index("--cpus") + 1], "1.5")

    def test_projects_copy_minimal_agent_home_and_skip_codex_model_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('model = "test"', encoding="utf-8")
            (codex_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            (codex_home / "models_cache.json").write_text('{"stale":true}', encoding="utf-8")
            (codex_home / "instructions.md").write_text("instructions", encoding="utf-8")
            manager = WorkerManager(
                root / "workspaces",
                agent_config_mounts=(
                    AgentConfigMount("codex", codex_home, "/host-agent-config/codex"),
                ),
            )

            homes = manager.project_agent_homes("project-one")
            copied = manager.backend_for("project-one").workspace_root / homes["codex"]
            self.assertTrue((copied / "config.toml").is_file())
            self.assertTrue((copied / "auth.json").is_file())
            self.assertTrue((copied / "instructions.md").is_file())
            self.assertFalse((copied / "models_cache.json").exists())

    def test_existing_container_with_an_old_image_is_recreated_in_its_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkerManager(root / "workspaces")
            backend = manager.backend_for("upgrade-project")
            calls: list[list[str]] = []

            def executor(command: list[str]) -> CommandExecution:
                calls.append(command)
                if command[1] == "inspect":
                    if command[3] == "{{.State.Running}}":
                        return CommandExecution(0, "true\n", "")
                    if command[3] == "{{.Config.Image}}":
                        return CommandExecution(0, "slime-cairn-kali:0.0.14\n", "")
                    if command[3] == "{{.HostConfig.NetworkMode}}":
                        return CommandExecution(0, "bridge\n", "")
                return CommandExecution(0, "", "")

            status = manager.ensure_started("upgrade-project", executor)

            self.assertEqual(status, "recreated")
            self.assertEqual(
                [command[1] for command in calls],
                ["inspect", "inspect", "inspect", "stop", "rm", "create", "start"],
            )
            create = calls[5]
            self.assertIn(DEFAULT_WORKER_IMAGE, create)
            self.assertIn(f"{backend.workspace_root}:/workspace:rw", create)

    def test_real_runtime_recreates_container_when_same_tag_points_to_new_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkerManager(root / "workspaces", verify_image_identity=True)
            calls: list[list[str]] = []

            def executor(command: list[str]) -> CommandExecution:
                calls.append(command)
                if command[1] == "inspect":
                    if command[3] == "{{.State.Running}}":
                        return CommandExecution(0, "false\n", "")
                    if command[3] == "{{.Config.Image}}":
                        return CommandExecution(0, f"{DEFAULT_WORKER_IMAGE}\n", "")
                    if command[3] == "{{.Image}}":
                        return CommandExecution(0, "sha256:old\n", "")
                    if command[3] == "{{.HostConfig.NetworkMode}}":
                        return CommandExecution(0, "bridge\n", "")
                    if command[3] == "{{json .HostConfig}}":
                        return CommandExecution(
                            0,
                            '{"Init":true,"PidsLimit":0,"Memory":0,"NanoCpus":0}\n',
                            "",
                        )
                if command[1:3] == ["image", "inspect"]:
                    return CommandExecution(0, "sha256:new\n", "")
                return CommandExecution(0, "", "")

            status = manager.ensure_started("same-tag-upgrade", executor)

            self.assertEqual(status, "recreated")
            self.assertTrue(any("{{.Image}}" in " ".join(command) for command in calls))
            self.assertTrue(any(command[1:3] == ["image", "inspect"] for command in calls))
            self.assertEqual([command[1] for command in calls][-3:], ["rm", "create", "start"])

    def test_raw_network_profile_uses_bridge_and_lab_admin_stays_internal(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = WorkerManager(Path(temporary) / "workspaces", lab_network="fixture-lab")

            raw = manager.backend_for("raw-project", RAW_NETWORK_PROFILE)
            lab = manager.backend_for("lab-project", LAB_NETWORK_ADMIN_PROFILE)

            self.assertEqual(raw.config.network, "bridge")
            self.assertEqual(lab.config.network, "fixture-lab")

    def test_existing_container_is_recreated_when_network_profile_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkerManager(root / "workspaces", lab_network="fixture-lab")
            calls: list[list[str]] = []

            def executor(command: list[str]) -> CommandExecution:
                calls.append(command)
                if command[1] == "inspect":
                    if command[3] == "{{.State.Running}}":
                        return CommandExecution(0, "false\n", "")
                    if command[3] == "{{.Config.Image}}":
                        return CommandExecution(0, f"{DEFAULT_WORKER_IMAGE}\n", "")
                    if command[3] == "{{.HostConfig.NetworkMode}}":
                        return CommandExecution(0, "fixture-lab\n", "")
                return CommandExecution(0, "", "")

            status = manager.ensure_started("raw-upgrade", executor, RAW_NETWORK_PROFILE)

            self.assertEqual(status, "recreated")
            self.assertEqual(
                [command[1] for command in calls],
                ["inspect", "inspect", "inspect", "rm", "create", "start"],
            )
            self.assertIn("bridge", calls[-2])

    def test_existing_container_is_recreated_when_runtime_limits_are_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkerManager(root / "workspaces")
            calls: list[list[str]] = []

            def executor(command: list[str]) -> CommandExecution:
                calls.append(command)
                if command[1] == "inspect":
                    if command[3] == "{{.State.Running}}":
                        return CommandExecution(0, "true\n", "")
                    if command[3] == "{{.Config.Image}}":
                        return CommandExecution(0, f"{DEFAULT_WORKER_IMAGE}\n", "")
                    if command[3] == "{{.HostConfig.NetworkMode}}":
                        return CommandExecution(0, "bridge\n", "")
                    if command[3] == "{{json .HostConfig}}":
                        return CommandExecution(
                            0,
                            '{"Init":true,"PidsLimit":128,"Memory":805306368,"NanoCpus":1000000000}\n',
                            "",
                        )
                return CommandExecution(0, "", "")

            status = manager.ensure_started("runtime-upgrade", executor)

            self.assertEqual(status, "recreated")
            self.assertEqual(
                [command[1] for command in calls],
                ["inspect", "inspect", "inspect", "inspect", "stop", "rm", "create", "start"],
            )
            create = calls[-2]
            self.assertIn("--init", create)
            self.assertNotIn("--pids-limit", create)
            self.assertNotIn("--memory", create)
            self.assertNotIn("--cpus", create)

    def test_existing_container_with_legacy_tmpfs_is_recreated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkerManager(root / "workspaces")
            calls: list[list[str]] = []

            def executor(command: list[str]) -> CommandExecution:
                calls.append(command)
                if command[1] == "inspect":
                    if command[3] == "{{.State.Running}}":
                        return CommandExecution(0, "true\n", "")
                    if command[3] == "{{.Config.Image}}":
                        return CommandExecution(0, f"{DEFAULT_WORKER_IMAGE}\n", "")
                    if command[3] == "{{.HostConfig.NetworkMode}}":
                        return CommandExecution(0, "bridge\n", "")
                    if command[3] == "{{json .HostConfig}}":
                        return CommandExecution(
                            0,
                            '{"Init":true,"PidsLimit":0,"Memory":0,"NanoCpus":0,'
                            '"Tmpfs":{"/tmp":"rw,nosuid,nodev,size=128m"}}\n',
                            "",
                        )
                return CommandExecution(0, "", "")

            status = manager.ensure_started("tmpfs-upgrade", executor)

            self.assertEqual(status, "recreated")
            self.assertEqual(
                [command[1] for command in calls],
                ["inspect", "inspect", "inspect", "inspect", "stop", "rm", "create", "start"],
            )
            self.assertNotIn("--tmpfs", calls[-2])

    def test_rejects_missing_or_conflicting_mount_sources_and_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                parse_agent_config_mounts(
                    {
                        "codex": {
                            "source": "missing",
                            "target": "/host-agent-config/codex",
                        }
                    },
                    base_directory=root,
                )

            codex = root / "codex"
            claude = root / "claude"
            codex.mkdir()
            claude.mkdir()
            with self.assertRaises(ValueError):
                parse_agent_config_mounts(
                    [
                        {
                            "agent": "codex",
                            "source": str(codex),
                            "target": "/host-agent-config/shared",
                        },
                        {
                            "agent": "claude",
                            "source": str(claude),
                            "target": "/host-agent-config/shared",
                        },
                    ]
                )


if __name__ == "__main__":
    unittest.main()
