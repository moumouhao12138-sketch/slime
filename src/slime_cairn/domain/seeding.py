from __future__ import annotations

from typing import Any

from .models import FactCandidate, Project
from ..server.blackboard import Blackboard


def seed_project_context_facts(board: Blackboard, project: Project) -> None:
    """Create the stable Origin/Goal anchors used by every Cairn project.

    With Bootstrap enabled, the anchors remain seed-only context until the
    Bootstrap Worker reports real evidence.  With Bootstrap disabled, the same
    anchors deliberately count as the first Reason signal, matching Cairn's
    direct initial Reason path instead of leaving a new project idle.
    """

    bootstrap_enabled = bool(project.scope.get("bootstrap_enabled", True))
    seeds: list[tuple[str, str, str, dict[str, Any]]] = [
        (
            "project_origin",
            str(project.scope.get("origin") or project.target),
            "origin",
            {
                "pinned": True,
                "seed": bootstrap_enabled,
                "role": "origin",
            },
        ),
        (
            "project_goal",
            project.goal,
            "goal",
            {
                "pinned": True,
                "seed": bootstrap_enabled,
                "role": "goal",
            },
        ),
    ]
    for predicate, obj, suffix, attributes in seeds:
        evidence_ref = f"project://{project.id}/{suffix}"
        board.register_evidence(
            project.id,
            evidence_ref,
            "project_directive",
            metadata={"source": "project_seed", "role": attributes["role"]},
        )
        board.add_fact(
            project.id,
            FactCandidate(
                subject=project.target,
                predicate=predicate,
                object=obj,
                confidence=1.0,
                evidence_refs=[evidence_ref],
                attributes=attributes,
            ),
        )
