import pytest

from src.reporting import (
    ReportAuditLog,
    ReportEvaluator,
    ReportVerifier,
    build_change_summary,
    build_memory_context,
    build_report_evidence,
)
from tests.fixtures.report_quality_cases import (
    REGRESSION_CASES,
    previous_analysis,
    previous_six_section_report,
    risk_heating_analysis,
    six_section_report,
)


@pytest.mark.parametrize("case", REGRESSION_CASES, ids=lambda case: case.name)
def test_report_quality_regression_cases(case):
    evidence = build_report_evidence(case.analysis)
    verification = ReportVerifier().verify(case.report, evidence)
    score = ReportEvaluator().evaluate(case.report, evidence, verification)
    codes = {finding.code for finding in verification.findings}

    assert verification.passed is case.expected_passed
    assert case.expected_codes <= codes
    assert score.grade == case.expected_grade
    assert case.min_score <= score.overall <= case.max_score


def test_change_summary_regression_for_risk_heating(tmp_path):
    audit_log = ReportAuditLog(tmp_path / "report-audit.jsonl")
    previous_evidence = build_report_evidence(previous_analysis(), report_period="weekly")
    previous_verification = ReportVerifier().verify(previous_six_section_report(), previous_evidence)
    previous_record = audit_log.append(
        report=previous_six_section_report(),
        evidence=previous_evidence,
        verification=previous_verification,
        source="llm",
    )
    current_evidence = build_report_evidence(risk_heating_analysis(), report_period="weekly")

    summary = build_change_summary(current_evidence, previous_record, build_memory_context(previous_record))

    assert summary.has_previous
    assert summary.usable
    assert "主要指数波动放大" in summary.new_risks
    assert any("站线比例下降" in item for item in summary.key_changes)
    assert any("PE分位数上升" in item for item in summary.key_changes)
    assert any("新增风险" in item for item in summary.deteriorated_signals)


def test_supported_percent_values_stay_traceable_for_fixture_reports():
    evidence = build_report_evidence(risk_heating_analysis())
    supported_values = evidence.supported_percent_values()
    report = six_section_report(
        standing_line_ratio=44.0,
        pe_percentile=52.0,
        portfolio_change=12.0,
        risk_text="主要指数波动放大，最大回撤8.00%。",
    )

    verification = ReportVerifier().verify(report, evidence)

    assert {44.0, 52.0, 12.0, 8.0} <= {round(value, 2) for value in supported_values}
    assert verification.passed
