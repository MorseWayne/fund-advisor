"""Append-only audit log for generated investment reports."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.reporting.evidence import ReportEvidence
from src.reporting.evaluation import ReportEvaluator
from src.reporting.verifier import VerificationResult


DEFAULT_REPORT_AUDIT_PATH = "data/reports/report-audit.jsonl"


@dataclass(frozen=True)
class ReportMemoryContext:
    """Compact previous-report context for the next report prompt."""

    has_history: bool
    usable: bool = False
    previous_as_of_date: str | None = None
    previous_report_period: str | None = None
    quality_grade: str | None = None
    quality_score: int | None = None
    challenge_posture: str | None = None
    finding_codes: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    summary: str = ""
    cautions: list[str] = field(default_factory=list)

    def to_prompt_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ReportAuditRecord:
    """One generated report with evidence and verification metadata."""

    generated_at: str
    as_of_date: str
    report_period: str
    source: str
    report_hash: str
    evidence_hash: str
    verification_passed: bool
    verification_confidence: float
    finding_codes: list[str] = field(default_factory=list)
    findings: list[dict[str, object]] = field(default_factory=list)
    challenge_posture: str | None = None
    missing_data: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    quality_score: dict[str, object] = field(default_factory=dict)
    report_text: str = ""
    evidence_payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportAuditSummary:
    """Roll-up view of recent report quality records."""

    total: int
    latest_as_of_date: str | None = None
    latest_report_period: str | None = None
    latest_score: int | None = None
    latest_grade: str | None = None
    average_score: float | None = None
    verification_pass_rate: float = 0.0
    blocker_counts: dict[str, int] = field(default_factory=dict)
    common_missing_data: dict[str, int] = field(default_factory=dict)
    recent_scores: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ReportAuditLog:
    """Write report generation records to a local JSONL audit trail."""

    def __init__(self, path: str | Path = DEFAULT_REPORT_AUDIT_PATH) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        report: str,
        evidence: ReportEvidence,
        verification: VerificationResult,
        source: str,
    ) -> ReportAuditRecord:
        """Append a report audit record and return the persisted payload."""

        record = build_audit_record(
            report=report,
            evidence=evidence,
            verification=verification,
            source=source,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return record

    def read_recent(
        self,
        limit: int = 20,
        *,
        report_period: str | None = None,
        before_date: str | None = None,
    ) -> list[ReportAuditRecord]:
        """Read recent records from the audit log, newest last."""

        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[ReportAuditRecord] = []
        for line in lines:
            if not line.strip():
                continue
            record = ReportAuditRecord(**json.loads(line))
            if report_period is not None and record.report_period != report_period:
                continue
            if before_date is not None and before_date != "今日" and record.as_of_date >= before_date:
                continue
            records.append(record)
        if limit <= 0:
            return []
        return records[-limit:]

    def summary(
        self,
        limit: int = 20,
        *,
        report_period: str | None = None,
    ) -> ReportAuditSummary:
        """Summarize recent report quality records for dashboards."""

        records = self.read_recent(limit=limit, report_period=report_period)
        if not records:
            return ReportAuditSummary(total=0)

        scores = [_quality_overall(record) for record in records]
        valid_scores = [score for score in scores if score is not None]
        blocker_counts: Counter[str] = Counter()
        missing_counts: Counter[str] = Counter()
        recent_scores: list[dict[str, object]] = []

        for record, score in zip(records, scores, strict=False):
            quality = record.quality_score or {}
            blockers = quality.get("blockers", [])
            if isinstance(blockers, list):
                blocker_counts.update(str(item) for item in blockers if item)
            missing_counts.update(item for item in record.missing_data if item)
            recent_scores.append({
                "as_of_date": record.as_of_date,
                "report_period": record.report_period,
                "source": record.source,
                "score": score,
                "grade": _quality_grade(record),
                "verification_passed": record.verification_passed,
                "finding_codes": list(record.finding_codes),
            })

        latest = records[-1]
        return ReportAuditSummary(
            total=len(records),
            latest_as_of_date=latest.as_of_date,
            latest_report_period=latest.report_period,
            latest_score=_quality_overall(latest),
            latest_grade=_quality_grade(latest),
            average_score=round(sum(valid_scores) / len(valid_scores), 2) if valid_scores else None,
            verification_pass_rate=round(
                sum(1 for record in records if record.verification_passed) / len(records),
                2,
            ),
            blocker_counts=dict(blocker_counts.most_common()),
            common_missing_data=dict(missing_counts.most_common()),
            recent_scores=recent_scores,
        )

    def latest_context(
        self,
        *,
        report_period: str,
        before_date: str | None = None,
    ) -> ReportMemoryContext:
        """Build prompt-safe context from the latest prior same-period record."""

        records = self.read_recent(limit=50, report_period=report_period, before_date=before_date)
        if not records:
            return ReportMemoryContext(has_history=False, summary="暂无可用历史报告。")
        return build_memory_context(records[-1])


def build_audit_record(
    *,
    report: str,
    evidence: ReportEvidence,
    verification: VerificationResult,
    source: str,
) -> ReportAuditRecord:
    """Build a deterministic audit record without writing it to disk."""

    evidence_payload = evidence.to_prompt_payload()
    challenge = evidence.challenge_review
    quality_score = ReportEvaluator().evaluate(report, evidence, verification)
    return ReportAuditRecord(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        as_of_date=evidence.as_of_date,
        report_period=evidence.report_period,
        source=source,
        report_hash=_hash_json({"report": report}),
        evidence_hash=_hash_json(evidence_payload),
        verification_passed=verification.passed,
        verification_confidence=round(verification.confidence, 2),
        finding_codes=[finding.code for finding in verification.findings],
        findings=[asdict(finding) for finding in verification.findings],
        challenge_posture=challenge.posture if challenge else None,
        missing_data=list(evidence.missing_data),
        risk_flags=list(evidence.risk_flags),
        quality_score=quality_score.to_dict(),
        report_text=report,
        evidence_payload=evidence_payload,
    )


def build_memory_context(record: ReportAuditRecord) -> ReportMemoryContext:
    """Convert one audit record into a compact prompt context."""

    quality = record.quality_score or {}
    overall = _int_or_none(quality.get("overall"))
    grade = str(quality.get("grade") or "") or None
    blockers = [str(item) for item in quality.get("blockers", [])] if isinstance(quality.get("blockers"), list) else []
    usable = record.verification_passed and (overall is None or overall >= 60) and not blockers
    cautions: list[str] = []
    if not record.verification_passed:
        cautions.append("上一期报告未通过验证，只能作为弱参考。")
    if blockers:
        cautions.append(f"上一期报告存在阻断问题：{', '.join(blockers[:3])}。")
    if overall is not None and overall < 60:
        cautions.append("上一期报告质量评分偏低，不应强化其结论。")
    if record.missing_data:
        cautions.append("上一期报告存在缺失数据，回顾时需要保留不确定性。")

    return ReportMemoryContext(
        has_history=True,
        usable=usable,
        previous_as_of_date=record.as_of_date,
        previous_report_period=record.report_period,
        quality_grade=grade,
        quality_score=overall,
        challenge_posture=record.challenge_posture,
        finding_codes=list(record.finding_codes),
        missing_data=list(record.missing_data),
        risk_flags=list(record.risk_flags),
        summary=_memory_summary(record, grade, overall, usable),
        cautions=cautions,
    )


def _memory_summary(
    record: ReportAuditRecord,
    grade: str | None,
    overall: int | None,
    usable: bool,
) -> str:
    quality_text = f"{grade}/{overall}" if grade and overall is not None else "暂无评分"
    usable_text = "可作为弱复盘依据" if usable else "仅作审计参考"
    return f"上一期{record.report_period}日期为{record.as_of_date}，质量为{quality_text}，{usable_text}。"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _quality_overall(record: ReportAuditRecord) -> int | None:
    return _int_or_none((record.quality_score or {}).get("overall"))


def _quality_grade(record: ReportAuditRecord) -> str | None:
    grade = (record.quality_score or {}).get("grade")
    return str(grade) if grade else None


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
