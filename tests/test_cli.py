from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from slime_cairn.cli import RuntimeContext, SlimeCli, _read_env_file, build_parser


class CliTests(unittest.TestCase):
    @staticmethod
    def _temporary_context(root: Path) -> RuntimeContext:
        runs = root / "runs"
        return RuntimeContext(
            root=root,
            config=root / "dispatch.cairn.native.json",
            database=runs / "slime-server.db",
            workspaces=runs / "slime-workspaces",
            runs=runs,
            service_dir=runs / "service",
            state_file=runs / "service" / "state.json",
            profile="raw-network",
            docker_binary="docker",
            lab_network="slime-cairn-lab",
            bind_host="127.0.0.1",
            port=8000,
        )

    def test_parser_accepts_legacy_single_dash_and_posix_long_options(self):
        parser = build_parser()

        legacy = parser.parse_args(["new", "-Name", "fixture", "-Target", "fixture.local"])
        modern = parser.parse_args(["new", "--name", "fixture", "--target", "fixture.local"])

        self.assertEqual(legacy.name, modern.name)
        self.assertEqual(legacy.target, modern.target)

    def test_parser_exposes_one_command_setup_and_worker_rebuild(self):
        args = build_parser().parse_args(["setup", "--rebuild-worker"])

        self.assertEqual(args.command, "setup")
        self.assertTrue(args.rebuild_worker)

    def test_runtime_environment_loads_dotenv_without_overriding_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "SLIME_LLM_MODEL=file-model\nSLIME_LLM_API_KEY='file-key'\n",
                encoding="utf-8",
            )
            context = self._temporary_context(root)

            with patch.dict("os.environ", {"SLIME_LLM_MODEL": "host-model"}, clear=False):
                environment = context.environment()

            self.assertEqual(environment["SLIME_LLM_MODEL"], "host-model")
            self.assertEqual(environment["SLIME_LLM_API_KEY"], "file-key")
            self.assertEqual(_read_env_file(root / ".env")["SLIME_LLM_API_KEY"], "file-key")

    def test_prepare_runtime_creates_env_then_prepares_dependencies_and_docker(self):
        root = Path(__file__).parents[1]
        args = build_parser().parse_args(["setup", "--project-root", str(root)])
        cli = SlimeCli(args, RuntimeContext.from_args(args))

        with (
            patch.object(cli, "_ensure_env_file", return_value=True) as ensure_env,
            patch.object(cli, "_validate_model_configuration") as validate,
            patch.object(cli, "_ensure_python_dependencies") as dependencies,
            patch.object(cli, "_ensure_docker_engine", return_value="docker") as engine,
            patch.object(cli, "_ensure_worker_image") as image,
            patch.object(cli, "_ensure_lab_network") as network,
        ):
            created = cli._prepare_runtime(require_model_configuration=True)

        self.assertTrue(created)
        ensure_env.assert_called_once_with()
        validate.assert_called_once_with()
        dependencies.assert_called_once_with()
        engine.assert_called_once_with()
        image.assert_called_once_with("docker")
        network.assert_called_once_with("docker")

    def test_first_setup_copies_env_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.example").write_text("SLIME_LLM_MODEL=model\n", encoding="utf-8")
            args = build_parser().parse_args(["setup"])
            cli = SlimeCli(args, self._temporary_context(root))

            self.assertTrue(cli._ensure_env_file())
            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"),
                "SLIME_LLM_MODEL=model\n",
            )
            self.assertFalse(cli._ensure_env_file())

    def test_model_configuration_rejects_example_placeholders(self):
        root = Path(__file__).parents[1]
        args = build_parser().parse_args(["setup", "--project-root", str(root)])
        cli = SlimeCli(args, RuntimeContext.from_args(args))
        environment = {
            "SLIME_LLM_BASE_URL": "https://api.example.com/v1",
            "SLIME_LLM_API_KEY": "replace-with-a-low-privilege-key",
            "SLIME_LLM_MODEL": "replace-with-model-name",
        }

        with patch.object(RuntimeContext, "environment", return_value=environment):
            with self.assertRaisesRegex(RuntimeError, "Model configuration is incomplete"):
                cli._validate_model_configuration()

    def test_up_dry_run_does_not_create_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(["up", "--dry-run"])
            cli = SlimeCli(args, self._temporary_context(root))

            with patch.object(cli, "_prepare_runtime"):
                cli.up()

            self.assertFalse((root / "runs").exists())

    def test_profile_block_is_removed_without_touching_surrounding_content(self):
        profile = (
            "export EDITOR=vim\n"
            "# >>> slime-cairn PATH >>>\n"
            "export PATH=\"$HOME/.local/bin:$PATH\"\n"
            "# <<< slime-cairn PATH <<<\n"
            "export LANG=C.UTF-8\n"
        )

        self.assertEqual(
            SlimeCli._without_profile_block(profile),
            "export EDITOR=vim\nexport LANG=C.UTF-8\n",
        )

    def test_posix_install_and_uninstall_touch_only_managed_user_files(self):
        root = Path(__file__).parents[1]
        parser = build_parser()
        install_args = parser.parse_args(
            ["install", "--project-root", str(root)]
        )
        uninstall_args = parser.parse_args(
            ["uninstall", "--project-root", str(root)]
        )
        context = RuntimeContext.from_args(install_args)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with (
                patch("slime_cairn.cli._is_windows", return_value=False),
                patch("slime_cairn.cli.Path.home", return_value=home),
                patch.dict("os.environ", {"PATH": "C:\\Windows"}, clear=False),
            ):
                SlimeCli(install_args, context).install()
                launcher = home / ".local" / "bin" / "slime"
                profile = home / ".profile"
                self.assertTrue(launcher.exists())
                self.assertIn("Managed by Slime Cairn CLI", launcher.read_text(encoding="utf-8"))
                self.assertIn("slime-cairn PATH", profile.read_text(encoding="utf-8"))

                SlimeCli(uninstall_args, context).uninstall()
                self.assertFalse(launcher.exists())
                self.assertNotIn("slime-cairn PATH", profile.read_text(encoding="utf-8"))

    def test_windows_install_and_uninstall_update_only_project_bin(self):
        root = Path(__file__).parents[1]
        parser = build_parser()
        install_args = parser.parse_args(["install", "--project-root", str(root)])
        uninstall_args = parser.parse_args(["uninstall", "--project-root", str(root)])
        context = RuntimeContext.from_args(install_args)
        command_dir = str((root / "bin").resolve())

        with (
            patch("slime_cairn.cli._is_windows", return_value=True),
            patch.object(SlimeCli, "_read_windows_user_path", return_value=r"C:\\Tools"),
            patch.object(SlimeCli, "_write_windows_user_path") as write_path,
        ):
            SlimeCli(install_args, context).install()
            write_path.assert_called_once_with(rf"C:\\Tools;{command_dir}")

        with (
            patch("slime_cairn.cli._is_windows", return_value=True),
            patch.object(
                SlimeCli,
                "_read_windows_user_path",
                return_value=rf"C:\\Tools;{command_dir};D:\\Other",
            ),
            patch.object(SlimeCli, "_write_windows_user_path") as write_path,
        ):
            SlimeCli(uninstall_args, context).uninstall()
            write_path.assert_called_once_with(r"C:\\Tools;D:\\Other")

    @unittest.skipIf(os.name == "nt", "POSIX process-group integration test")
    def test_posix_background_process_group_is_stopped_from_state(self):
        root = Path(__file__).parents[1]
        args = build_parser().parse_args(["help", "--project-root", str(root)])
        context = RuntimeContext.from_args(args)
        cli = SlimeCli(args, context)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "out.log"
            error = Path(temporary) / "err.log"
            pid, pgid = cli._spawn_background(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                    "slime_cairn.service_main",
                ],
                output,
                error,
                context.environment(),
            )
            self.assertTrue(cli._pid_alive(pid))

            cli._stop_named_process(
                {"dispatcher_pid": pid, "dispatcher_pgid": pgid},
                "dispatcher",
            )

            self.assertFalse(cli._pid_alive(pid))

    @unittest.skipUnless(os.name == "nt", "Windows PID probe regression test")
    def test_windows_pid_probe_uses_tasklist(self):
        completed = subprocess.CompletedProcess(
            ["tasklist"],
            0,
            stdout='"python.exe","4242","Console","1","12,345 K"\r\n',
            stderr="",
        )
        with patch("slime_cairn.cli.subprocess.run", return_value=completed) as run:
            self.assertTrue(SlimeCli._pid_alive(4242))
            self.assertFalse(SlimeCli._pid_alive(4243))
        run.assert_any_call(
            ["tasklist", "/FI", "PID eq 4242", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration test")
    def test_windows_launcher_forwards_help_without_starting_services(self):
        root = Path(__file__).parents[1]
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "slime.cmd", "help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Slime Cairn", completed.stdout)

        launcher = (root / "bin" / "slime.cmd").read_text(encoding="utf-8")
        self.assertIn("slime_cairn.cli", launcher)
        self.assertNotIn("powershell", launcher.lower())

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration test")
    def test_windows_launcher_install_dry_run_does_not_change_user_path(self):
        import winreg

        def user_path() -> str:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return str(winreg.QueryValueEx(key, "Path")[0])

        root = Path(__file__).parents[1]
        before = user_path()
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "slime.cmd", "install", "-DryRun"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(
            "Would add to the current user PATH" in completed.stdout
            or "already installed" in completed.stdout
        )
        self.assertEqual(user_path(), before)


if __name__ == "__main__":
    unittest.main()
