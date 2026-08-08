from __future__ import annotations

from functools import lru_cache
from importlib import resources
import re
from typing import Mapping


PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "AGENTS.md": (),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{trigger}", "{initial_error}", "{context_json}"),
    "explore.md": (
        "{graph_yaml}",
        "{intent_id}",
        "{intent_description}",
        "{explore_shape}",
        "{benchmark_rule}",
        "{branch_checkpoint}",
    ),
    "explore_conclude.md": ("{trigger}", "{initial_error}", "{context_json}"),
    "reason.md": (
        "{graph_yaml}",
        "{fact_ids}",
        "{open_intents}",
        "{max_intents}",
        "{complete_shape}",
        "{benchmark_rule}",
    ),
}


def _group_directory(group: str):
    normalized = str(group).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", normalized) or normalized in {".", ".."}:
        raise ValueError(f"invalid prompt group: {group}")
    directory = resources.files("slime_cairn.prompts").joinpath(normalized)
    if not directory.is_dir():
        raise ValueError(f"missing prompt group: {normalized}")
    return directory


@lru_cache(maxsize=32)
def load_prompt(group: str, name: str) -> str:
    if name not in PROMPT_REQUIRED_TOKENS:
        raise ValueError(f"unsupported prompt resource: {name}")
    resource = _group_directory(group).joinpath(name)
    if not resource.is_file():
        raise ValueError(f"prompt group {group} missing resource: {name}")
    return resource.read_text(encoding="utf-8")


def validate_prompt_group(group: str) -> None:
    for name, required_tokens in PROMPT_REQUIRED_TOKENS.items():
        template = load_prompt(group, name)
        missing = [token for token in required_tokens if token not in template]
        if missing:
            raise ValueError(
                f"prompt group {group} resource {name} missing placeholders: "
                + ", ".join(missing)
            )


def render_prompt(template: str, replacements: Mapping[str, object]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered
