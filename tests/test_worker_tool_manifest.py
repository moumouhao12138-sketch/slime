from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "worker" / "scripts" / "build_tool_manifest.py"


def load_manifest_module():
    spec = importlib.util.spec_from_file_location("build_tool_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerToolManifestTests(unittest.TestCase):
    def test_version_probe_does_not_write_to_runtime_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_home = root / "runtime-home"
            runtime_home.mkdir()
            executable = root / "stateful-version"
            executable.write_text(
                "#!/bin/sh\n"
                "mkdir -p \"$HOME/.stateful-version\"\n"
                "touch \"$HOME/.stateful-version/created\"\n"
                "printf 'stateful-version 1.0\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

            module = load_manifest_module()
            with patch.dict(os.environ, {"HOME": str(runtime_home)}):
                version = module.version_of(str(executable))

            self.assertEqual(version, "stateful-version 1.0")
            self.assertEqual(list(runtime_home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
