"""Deterministic scoring rubric for generated reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.reporting.evidence import ReportEvidence
from src.reporting.verifier import VerificationResult


@dataclass(frozen=True)
class ReportQualityScore:
    """Weighted report-quality score suitable for audits and trend tracking."""

    overall: int
    grade: str
    components: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ReportEvaluator:
    """Score a report using evidence, verification findings, and risk context."""

    def evaluate(
        self,
        report: str,
        evidence: ReportEvidence,
        verification: VerificationResult,
    ) -> ReportQualityScore:
        codes = {finding.code for finding in verification.findings}
        components = {
            "structure": _structure_score(codes),
            "evidence": _evidence_score(evidence),
            "numeric_trace": 0 if "unsupported_numeric_claim" in codes else 20,
            "risk_handling": _risk_score(report, evidence),
            "missing_data": _missing_data_score(report, evidence),
            "action_safety": 0 if "absolute_advice" in codes else 10,
        }
        blockers = _blockers(codes)
        notes = _notes(report, evidence, codes)
        overall = max(0, min(100, sum(components.values())))
        if blockers:
            overall = min(overall, 59)
        return ReportQualityScore(
            overall=overall,
            grade=_grade(overall),
            components=components,
            blockers=blockers,
            notes=notes,
        )


def _structure_score(codes: set[str]) -> int:
    if "missing_structure" in codes:
        return 8
    if "empty_section" in codes:
        return 14
    return 20


def _evidence_score(evidence: ReportEvidence) -> int:
    score = 20
    if len(evidence.metrics) < 3:
        score -= 6
    if evidence.confidence < 0.65:
        score -= 5
    if not evidence.section_briefs:
        score -= 4
    return max(0, score)


def _risk_score(report: str, evidence: ReportEvidence) -> int:
    if not evidence.risk_flags:
        return 15
    if "风险" not in report:
        return 5
    if any(flag[:10] and flag[:10] in report for flag in evidence.risk_flags):
        return 15
    return 11


def _missing_data_score(report: str, evidence: ReportEvidence) -> int:
    if not evidence.missing_data:
        return 15
    if "暂无" in report or "缺失" in report or "不足" in report:
        return 15
    return 5


def _blockers(codes: set[str]) -> list[str]:
    blockers: list[str] = []
    if "unsupported_numeric_claim" in codes:
        blockers.append("unsupported_numeric_claim")
    if "absolute_advice" in codes:
        blockers.append("absolute_advice")
    return blockers


def _notes(report: str, evidence: ReportEvidence, codes: set[str]) -> list[str]:
    notes: list[str] = []
    if evidence.missing_data and not ("暂无" in report or "缺失" in report or "不足" in report):
        notes.append("missing data was not clearly disclosed")
    if evidence.risk_flags and "风险" not in report:
        notes.append("risk flags were not clearly discussed")
    if "empty_section" in codes:
        notes.append("one or more sections are too short")
    return notes


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"
