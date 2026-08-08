from __future__ import annotations

import unittest

from slime_cairn.prompting import (
    PROMPT_REQUIRED_TOKENS,
    load_prompt,
    render_prompt,
    validate_prompt_group,
)


class PromptResourceTests(unittest.TestCase):
    def test_default_group_contains_every_worker_resource(self) -> None:
        validate_prompt_group("default")

        for name in PROMPT_REQUIRED_TOKENS:
            with self.subTest(name=name):
                self.assertTrue(load_prompt("default", name).strip())

    def test_renderer_replaces_only_declared_placeholders(self) -> None:
        rendered = render_prompt(
            '{"accepted": true, "data": {"description": "..."}}\n{value}',
            {"value": "confirmed"},
        )

        self.assertIn('{"accepted": true', rendered)
        self.assertTrue(rendered.endswith("confirmed"))

    def test_group_name_cannot_escape_packaged_resources(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid prompt group"):
            load_prompt("../default", "reason.md")
