from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .blackboard import Blackboard
from .models import FactCandidate, HypothesisCandidate, IntentProposal, Project


@dataclass(frozen=True, slots=True)
class ValidationResult:
    accepted: bool
    reason: str


class ScopeMatcher:
    @staticmethod
    def allows(project: Project, entity: str) -> bool:
        entity = entity.strip().lower()
        allowed = [str(item).strip().lower() for item in project.scope.get("targets", [])]
        if project.target.lower() not in allowed:
            allowed.append(project.target.lower())
        if entity in allowed:
            return True
        parsed = urlparse(entity if "://" in entity else f"//{entity}")
        entity_host = parsed.hostname
        for target in allowed:
            target_parsed = urlparse(target if "://" in target else f"//{target}")
            if entity_host and target_parsed.hostname and entity_host == target_parsed.hostname:
                return True
            if entity.startswith(f"{target}/") or entity.startswith(f"{target}:"):
                return True
        return False


class FactEvidenceGate:
    def __init__(self, board: Blackboard, project: Project, minimum_confidence: float = 0.5) -> None:
        self.board = board
        self.project = project
        self.minimum_confidence = minimum_confidence

    def review(self, candidate: FactCandidate) -> ValidationResult:
        if not candidate.subject.strip() or not candidate.predicate.strip() or not candidate.object.strip():
            return ValidationResult(False, "Fact 的 subject、predicate、object 均不能为空")
        if not ScopeMatcher.allows(self.project, candidate.subject):
            return ValidationResult(False, f"Fact subject 不在授权范围内: {candidate.subject}")
        if candidate.confidence < self.minimum_confidence or candidate.confidence > 1.0:
            return ValidationResult(False, "Fact 置信度不在接受范围")
        if not candidate.evidence_refs:
            return ValidationResult(False, "Fact 没有 evidence_refs，应改存为 Hypothesis")
        missing = [
            evidence_ref
            for evidence_ref in candidate.evidence_refs
            if not self.board.evidence_exists(self.project.id, evidence_ref)
        ]
        if missing:
            return ValidationResult(False, f"Fact 引用了未登记证据: {missing}")
        records = [self.board.get_evidence(self.project.id, item) for item in candidate.evidence_refs]
        if records and all(record and record["kind"] == "control_artifact" for record in records):
            return ValidationResult(False, "Fact 只能引用控制/查询记录，应改存为 Hypothesis")
        return ValidationResult(True, "evidence_gate_passed")


class HypothesisGate:
    def __init__(self, board: Blackboard, project_id: str) -> None:
        self.board = board
        self.project_id = project_id

    def review(self, candidate: HypothesisCandidate) -> ValidationResult:
        if not candidate.statement.strip():
            return ValidationResult(False, "Hypothesis statement 不能为空")
        known_ids = {fact.id for fact in self.board.list_facts(self.project_id)}
        unknown = set(candidate.supporting_fact_ids) - known_ids
        if unknown:
            return ValidationResult(False, f"Hypothesis 引用了未知 Fact: {sorted(unknown)}")
        if not 0.0 <= candidate.confidence <= 1.0:
            return ValidationResult(False, "Hypothesis 置信度必须位于 0..1")
        return ValidationResult(True, "hypothesis_gate_passed")


class IntentGate:
    def __init__(self, board: Blackboard, project: Project) -> None:
        self.board = board
        self.project = project

    def review(self, proposal: IntentProposal) -> ValidationResult:
        if not proposal.objective.strip() or not proposal.kind.strip():
            return ValidationResult(False, "Intent kind 和 objective 不能为空")
        if not ScopeMatcher.allows(self.project, proposal.target_entity):
            return ValidationResult(False, f"Intent target 不在授权范围内: {proposal.target_entity}")
        known_ids = {fact.id for fact in self.board.list_facts(self.project.id)}
        unknown_parents = set(proposal.parent_fact_ids) - known_ids
        if unknown_parents:
            return ValidationResult(False, f"Intent 引用了未知父级 Fact: {sorted(unknown_parents)}")
        supporting = set(proposal.provenance.get("supporting_fact_ids", proposal.parent_fact_ids))
        unknown_support = supporting - known_ids
        if unknown_support:
            return ValidationResult(False, f"Intent provenance 引用了未知 Fact: {sorted(unknown_support)}")
        if not str(proposal.provenance.get("hypothesis", proposal.objective)).strip():
            return ValidationResult(False, "Intent 缺少可审计 hypothesis")
        return ValidationResult(True, "intent_gate_passed")
