"""Append-only audit log for generated investment reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.reporting.evidence import ReportEvidence
from src.reporting.verifier import VerificationResult


DEFAULT_REPORT_AUDIT_PATH = "data/reports/report-audit.jsonl"


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
    report_text: str = ""
    evidence_payload: dict[str, object] = field(default_factory=dict)


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

    def read_recent(self, limit: int = 20) -> list[ReportAuditRecord]:
        """Read recent records from the audit log, newest last."""

        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[ReportAuditRecord] = []
        for line in lines[-max(limit, 0):]:
            if not line.strip():
                continue
            records.append(ReportAuditRecord(**json.loads(line)))
        return records


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
        report_text=report,
        evidence_payload=evidence_payload,
    )


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
