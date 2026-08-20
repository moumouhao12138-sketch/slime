#!/usr/bin/env python3
"""Patch DSH's DeepSeek adapter and headless runner for Slime's task contract.

Some OpenAI-compatible gateways emit ``id: null`` and ``name: null`` on
continuation deltas.  dsh 0.1.0-rc.7 used those values to overwrite metadata
captured from the first delta, producing empty tool-call identifiers in the
next request.  Preserve only non-empty strings so the completed call remains
valid for the gateway's message schema.

The stock headless runner also creates a random session for every process.
Slime passes a task-local session ID and needs its bounded conclude invocation
to resume that persisted session after a timeout, so the runner is extended to
select ``agents.resume`` when ``DSH_RESUME_SESSION_ID`` is present.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


OLD_ID = "if (call.id !== void 0) block.callId = call.id;"
NEW_ID = 'if (typeof call.id === "string" && call.id.length > 0) block.callId = call.id;'
OLD_NAME = "if (call.function?.name !== void 0) block.name = call.function.name;"
NEW_NAME = 'if (typeof call.function?.name === "string" && call.function.name.length > 0) block.name = call.function.name;'

HEADLESS_OLD_RUN = """\tconst selection = defaultModel.currentSelection();
\tconst { agent } = await agents.create({
\t\tsessionId: SessionId(`session-${randomUUID()}`),
\t\tmeta: { cwd: process.cwd() },
\t\tagentOptions: {
\t\t\tprovider: selection.provider,
\t\t\tmodel: selection.model
\t\t},
\t\tsetup: (agentCtx) => {
\t\t\tinstallModelSelection(agentCtx, {
\t\t\t\tcurrent: selection,
\t\t\t\tassembled: void 0
\t\t\t});
\t\t}
\t});"""
HEADLESS_NEW_RUN = """\tconst selection = defaultModel.currentSelection();
\tconst agentOptions = {
\t\tprovider: selection.provider,
\t\tmodel: selection.model
\t};
\tconst setup = (agentCtx) => {
\t\tinstallModelSelection(agentCtx, {
\t\t\tcurrent: selection,
\t\t\tassembled: void 0
\t\t});
\t};
\tconst resumeSessionId = process.env.DSH_RESUME_SESSION_ID?.trim();
\tconst configuredSessionId = process.env.DSH_SESSION_ID?.trim();
\tconst handle = resumeSessionId
\t\t? await agents.resume({
\t\t\tresumeSessionId: SessionId(resumeSessionId),
\t\t\tagentOptions,
\t\t\tsetup
\t\t})
\t\t: await agents.create({
\t\t\tsessionId: SessionId(configuredSessionId || `session-${randomUUID()}`),
\t\t\tmeta: { cwd: process.cwd() },
\t\t\tagentOptions,
\t\t\tsetup
\t\t});
\tconst { agent } = handle;"""
HEADLESS_OLD_SUMMARY = "\tconst outcome = summarize(agent.session.events, firstSeq);\n\tio.stdout.write(outcome.text + \"\\n\");"
HEADLESS_NEW_SUMMARY = "\tconst outcome = summarize(agent.session.events, firstSeq);\n\tawait handle.dispose();\n\tio.stdout.write(outcome.text + \"\\n\");"


def package_version(package_root: Path) -> str:
    package_json = package_root / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"DeepSeek Harness package metadata is unreadable: {package_json}") from exc
    return str(data.get("version", "unknown"))


def _patch_source(source: Path, old: str, new: str, label: str) -> bool:
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"DeepSeek Harness {label} is unreadable: {source}") from exc
    if old not in text:
        if new in text:
            return False
        raise SystemExit(f"Unexpected DeepSeek Harness {label} source: {source}")
    if text.count(old) != 1:
        raise SystemExit(f"DeepSeek Harness {label} patch anchor is ambiguous: {source}")
    source.write_text(text.replace(old, new), encoding="utf-8")
    return True


def patch(package_root: Path) -> bool:
    source = package_root / "lib" / "index.js"
    changed = False
    changed |= _patch_source(source, OLD_ID, NEW_ID, "adapter")
    changed |= _patch_source(source, OLD_NAME, NEW_NAME, "adapter")

    headless_source = package_root.parent / "dsh-headless" / "lib" / "index.js"
    changed |= _patch_source(headless_source, HEADLESS_OLD_RUN, HEADLESS_NEW_RUN, "headless runner")
    changed |= _patch_source(
        headless_source,
        HEADLESS_OLD_SUMMARY,
        HEADLESS_NEW_SUMMARY,
        "headless runner summary",
    )
    return changed


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_deepseek_harness.py <dsh-llm-deepseek-package>")
    root = Path(sys.argv[1]).resolve()
    changed = patch(root)
    print(
        "deepseek-harness adapter: "
        f"version={package_version(root)} nullable-tool-metadata={'patched' if changed else 'already-patched'}"
    )


if __name__ == "__main__":
    main()
