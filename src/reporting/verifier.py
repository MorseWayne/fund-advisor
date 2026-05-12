"""Deterministic report quality checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.reporting.evidence import ReportEvidence


PERCENT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
ABSOLUTE_ADVICE_PATTERNS = ("必涨", "稳赚", "保证收益", "无风险", "满仓买入", "绝对安全")
NEGATIVE_PERCENT_CONTEXT = ("跌", "下跌", "下降", "回落", "减少", "亏损", "流出", "折价")


@dataclass(frozen=True)
class VerificationFinding:
    """One deterministic report-quality finding."""

    level: str
    code: str
    message: str


@dataclass(frozen=True)
class VerificationResult:
    """Result of checking a generated report against its evidence."""

    passed: bool
    confidence: float
    findings: list[VerificationFinding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


class ReportVerifier:
    """Verify that generated reports stay grounded in the evidence packet."""

    def verify(self, report: str, evidence: ReportEvidence, *, source: str = "") -> VerificationResult:
        findings: list[VerificationFinding] = []
        text = report or ""

        self._check_required_structure(text, evidence, findings)
        self._check_empty_sections(text, evidence, findings)
        self._check_freshness(text, evidence, findings)
        self._check_missing_data_disclosure(text, evidence, findings)

        # For structured reports, skip regex-based numeric trace since
        # numbers are validated via Pydantic schema. Only text-based
        # reports (fallback) need the heuristic check.
        if "structured" not in source:
            self._check_numeric_trace(text, evidence, findings)

        self._check_advice_safety(text, findings)

        errors = [finding for finding in findings if finding.level == "error"]
        confidence = _score_confidence(evidence.confidence, findings)
        return VerificationResult(passed=not errors, confidence=confidence, findings=findings)

    def _check_required_structure(
        self,
        report: str,
        evidence: ReportEvidence,
        findings: list[VerificationFinding],
    ) -> None:
        expected = [
            f"投资{evidence.report_label}",
            f"一、{evidence.report_scope}概览",
            "二、方向信号",
            "三、板块机会",
            "四、估值温度",
            "五、风险提醒",
            "六、你的持仓",
        ]
        missing = [item for item in expected if item not in report]
        if missing:
            findings.append(VerificationFinding("warning", "missing_structure", f"报告缺少结构项：{', '.join(missing[:3])}"))

    def _check_empty_sections(
        self,
        report: str,
        evidence: ReportEvidence,
        findings: list[VerificationFinding],
    ) -> None:
        titles = [
            f"一、{evidence.report_scope}概览",
            "二、方向信号",
            "三、板块机会",
            "四、估值温度",
            "五、风险提醒",
            "六、你的持仓",
        ]
        empty: list[str] = []
        for index, title in enumerate(titles):
            start = report.find(title)
            if start < 0:
                continue
            next_positions = [report.find(item, start + len(title)) for item in titles[index + 1:]]
            next_positions = [position for position in next_positions if position >= 0]
            end = min(next_positions) if next_positions else len(report)
            body = report[start + len(title):end].strip()
            if len(body) < 6:
                empty.append(title)
        if empty:
            findings.append(VerificationFinding("warning", "empty_section", f"报告章节正文过短：{', '.join(empty[:3])}"))

    def _check_freshness(
        self,
        report: str,
        evidence: ReportEvidence,
        findings: list[VerificationFinding],
    ) -> None:
        if evidence.as_of_date and evidence.as_of_date != "今日" and evidence.as_of_date not in report:
            findings.append(VerificationFinding("warning", "missing_as_of_date", f"报告未展示数据日期 {evidence.as_of_date}"))

    def _check_missing_data_disclosure(
        self,
        report: str,
        evidence: ReportEvidence,
        findings: list[VerificationFinding],
    ) -> None:
        if evidence.missing_data and "暂无" not in report:
            fields = ", ".join(evidence.missing_data[:4])
            findings.append(VerificationFinding("warning", "missing_data_not_disclosed", f"存在缺失数据但报告未明确披露：{fields}"))

    def _check_numeric_trace(
        self,
        report: str,
        evidence: ReportEvidence,
        findings: list[VerificationFinding],
    ) -> None:
        reported = _reported_percent_values(report)
        if not reported:
            return

        supported = evidence.supported_percent_values()
        unsupported = [
            value for value in reported
            if not any(abs(value - known) <= 0.05 for known in supported)
        ]
        if unsupported:
            sample = ", ".join(f"{value:.2f}%" for value in unsupported[:4])
            findings.append(VerificationFinding("error", "unsupported_numeric_claim", f"报告包含证据包未支持的百分比数字：{sample}"))

    def _check_advice_safety(self, report: str, findings: list[VerificationFinding]) -> None:
        hits = [pattern for pattern in ABSOLUTE_ADVICE_PATTERNS if pattern in report]
        if hits:
            findings.append(VerificationFinding("error", "absolute_advice", f"报告包含绝对化投资表述：{', '.join(hits)}"))


def append_quality_notes(report: str, result: VerificationResult) -> str:
    """Append concise quality notes without replacing the generated report."""

    if not result.findings:
        return report.strip()

    important = [finding for finding in result.findings if finding.level == "error"]
    if not important:
        important = result.findings
    messages = "；".join(finding.message for finding in important[:3])
    return f"{report.strip()}\n\n数据质量提示：{messages}。"


def _score_confidence(base: float, findings: list[VerificationFinding]) -> float:
    score = base
    for finding in findings:
        score -= 0.15 if finding.level == "error" else 0.05
    return max(0.1, min(0.95, score))


def _reported_percent_values(report: str) -> list[float]:
    values: list[float] = []
    for match in PERCENT_RE.finditer(report or ""):
        raw = match.group(1)
        value = float(raw)
        if value >= 0 and not raw.startswith("+"):
            context = report[max(0, match.start() - 8):match.start()]
            if any(token in context for token in NEGATIVE_PERCENT_CONTEXT):
                value = -value
        values.append(value)
    return values
