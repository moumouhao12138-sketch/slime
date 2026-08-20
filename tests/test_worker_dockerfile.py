from __future__ import annotations

from pathlib import Path
import re
import unittest


DOCKERFILE = Path(__file__).parents[1] / "worker" / "Dockerfile"


class WorkerDockerfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_python_native_build_fallback_has_required_tools(self) -> None:
        for package in ("build-essential", "cmake", "pkg-config"):
            with self.subTest(package=package):
                self.assertIn(package, self.text)

    def test_python_native_dependencies_are_pinned_and_verified(self) -> None:
        for name, version in (
            ("PWNTOOLS_VERSION", "4.15.0"),
            ("PYMONGO_VERSION", "4.17.0"),
            ("UNICORN_VERSION", "2.1.2"),
        ):
            with self.subTest(name=name):
                self.assertRegex(self.text, rf"ARG {name}={re.escape(version)}")

        self.assertIn('"unicorn==${UNICORN_VERSION}"', self.text)
        self.assertIn('"pwntools==${PWNTOOLS_VERSION}"', self.text)
        self.assertIn('"pymongo==${PYMONGO_VERSION}"', self.text)
        self.assertIn("import importlib.metadata as metadata, pwn, pymongo, unicorn", self.text)


if __name__ == "__main__":
    unittest.main()
