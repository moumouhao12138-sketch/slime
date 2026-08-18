"""Model catalog metadata for native CLI adapters.

Codex CLI ships a hard-coded catalog for OpenAI models.  When a custom relay
model (for example ``deepseek-v4-flash-0731``) is unknown to that catalog,
Codex falls back to generic metadata: it emits the "Model metadata not found"
warning, assumes a default context window, and keeps skills/plugin/apps
instruction blocks in every prompt.  Supplying ``model_catalog_json`` restores
correct metadata and drops those unrelated instruction blocks.

The generated entry is intentionally conservative and adapter-neutral: it only
asserts what the Cairn runtime controls (reasoning levels and tool mode) and
keeps OpenAI-specific capabilities disabled.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import uuid4


CATALOG_FILENAME = ".codex-model-catalog.json"

_lock = threading.Lock()


def build_model_catalog(
    model: str,
    *,
    context_window: int = 131_072,
) -> dict[str, object]:
    """Build a Codex ``model_catalog_json`` document for a relay model."""

    return {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "description": f"{model} via Cairn native adapter",
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "none", "description": "No thinking"},
                    {"effort": "low", "description": "Light reasoning"},
                    {"effort": "medium", "description": "Balanced reasoning"},
                    {"effort": "high", "description": "Deep reasoning"},
                    {"effort": "xhigh", "description": "Extra deep reasoning"},
                    {"effort": "max", "description": "Maximum reasoning"},
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 50,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "upgrade": None,
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": False,
                "include_apps_usage_instructions": False,
                # Required by Codex <= 0.146 and ignored by newer builds.
                # Relay models rarely support reasoning summaries, so keep
                # them disabled instead of letting Codex request them.
                "supports_reasoning_summaries": False,
                "default_reasoning_summary": "none",
                "support_verbosity": True,
                "default_verbosity": "low",
                "apply_patch_tool_type": "freeform",
                "web_search_tool_type": "text",
                "truncation_policy": {"mode": "bytes", "limit": 10_000},
                "supports_parallel_tool_calls": True,
                "supports_image_detail_original": False,
                "context_window": context_window,
                "max_context_window": context_window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "use_responses_lite": False,
                "base_instructions": (
                    "You are a helpful assistant working in a Kali Linux container."
                ),
            }
        ]
    }


def install_model_catalog(workspace_root: Path, model: str) -> str:
    """Install a Codex model catalog for ``model`` into the project workspace.

    Returns the workspace-relative filename.  The file is visible at
    ``/workspace/<name>`` inside every project Worker because the project
    workspace subdirectory is mounted at ``/workspace``.
    """

    if not str(model).strip():
        return ""
    root = workspace_root.resolve()
    path = (root / CATALOG_FILENAME).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"model catalog escapes workspace: {path}") from exc
    value = build_model_catalog(str(model))
    with _lock:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if existing == value:
            return CATALOG_FILENAME
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return CATALOG_FILENAME
