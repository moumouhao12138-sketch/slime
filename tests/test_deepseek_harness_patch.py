from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "worker" / "scripts" / "patch_deepseek_harness.py"


def load_module():
    spec = importlib.util.spec_from_file_location("patch_deepseek_harness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeepSeekHarnessPatchTests(unittest.TestCase):
    def test_nullable_tool_metadata_does_not_overwrite_first_delta(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "dsh-llm-deepseek"
            root = package_root
            (root / "lib").mkdir(parents=True)
            (root / "package.json").write_text(
                '{"name":"@deepseek-ai/dsh-llm-deepseek","version":"0.1.0-rc.7"}',
                encoding="utf-8",
            )
            source = root / "lib" / "index.js"
            source.write_text(
                f"{module.OLD_ID}\n{module.OLD_NAME}\n",
                encoding="utf-8",
            )
            headless = package_root.parent / "dsh-headless"
            (headless / "lib").mkdir(parents=True)
            (headless / "lib" / "index.js").write_text(
                module.HEADLESS_OLD_RUN + "\n" + module.HEADLESS_OLD_SUMMARY,
                encoding="utf-8",
            )

            self.assertTrue(module.patch(root))
            patched = source.read_text(encoding="utf-8")
            self.assertIn(module.NEW_ID, patched)
            self.assertIn(module.NEW_NAME, patched)
            self.assertFalse(module.patch(root))

    def test_unexpected_adapter_source_fails_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "dsh-llm-deepseek"
            root = package_root
            (root / "lib").mkdir(parents=True)
            (root / "package.json").write_text('{"version":"fixture"}', encoding="utf-8")
            (root / "lib" / "index.js").write_text("unexpected\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                module.patch(root)

    def test_headless_runner_accepts_stable_session_and_resume_id(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "dsh-llm-deepseek"
            root = package_root
            (root / "lib").mkdir(parents=True)
            (root / "package.json").write_text(
                '{"name":"@deepseek-ai/dsh-llm-deepseek","version":"fixture"}',
                encoding="utf-8",
            )
            (root / "lib" / "index.js").write_text(
                f"{module.OLD_ID}\n{module.OLD_NAME}\n",
                encoding="utf-8",
            )
            headless = package_root.parent / "dsh-headless"
            (headless / "lib").mkdir(parents=True)
            (headless / "lib" / "index.js").write_text(
                module.HEADLESS_OLD_RUN + "\n" + module.HEADLESS_OLD_SUMMARY,
                encoding="utf-8",
            )

            self.assertTrue(module.patch(root))
            patched = (headless / "lib" / "index.js").read_text(encoding="utf-8")
            self.assertIn("DSH_SESSION_ID", patched)
            self.assertIn("DSH_RESUME_SESSION_ID", patched)
            self.assertIn("agents.resume", patched)
            self.assertIn("resumeSessionId: SessionId(resumeSessionId)", patched)
            self.assertIn("await handle.dispose()", patched)
            self.assertFalse(module.patch(root))


if __name__ == "__main__":
    unittest.main()
