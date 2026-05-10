import pytest

from src.llm.prompts import build_daily_report_prompt
from src.llm.report_generator import ReportGenerator
from src.reporting import (
    ReportAuditLog,
    ReportEvaluator,
    ReportMemoryContext,
    ReportVerifier,
    append_quality_notes,
    build_report_evidence,
    build_memory_context,
)


def _analysis_result():
    return {
        "date": "2026-05-10",
        "overview": {"direction": "观望", "summary": "市场震荡", "key_events": []},
        "trend": {
            "ma_alignment": "多头排列",
            "standing_line_ratio": 0.62,
            "sentiment": "中性",
            "confidence": 0.7,
        },
        "sector_opportunities": [],
        "valuation": {"overall_level": "合理", "pe_percentile": 45.0, "continue_sip": True},
        "risk_alerts": [],
        "portfolio_status": {"total_change_pct": 0.12, "holdings": []},
    }


def _six_section_report(extra: str = "") -> str:
    return f"""📊 2026-05-10 投资周报

一、本周概览
总体观望。

二、方向信号
站线比例为62.00%。

三、板块机会
暂无明确机会。

四、估值温度
PE分位数为45.00%。

五、风险提醒
暂无新增风险。

六、你的持仓
组合当前收益12.00%。{extra}"""


def test_evidence_packet_tracks_metrics_missing_data_and_confidence():
    evidence = build_report_evidence(_analysis_result())

    assert evidence.as_of_date == "2026-05-10"
    assert evidence.report_label == "周报"
    assert [brief.title for brief in evidence.section_briefs] == [
        "一、本周概览",
        "二、方向信号",
        "三、板块机会",
        "四、估值温度",
        "五、风险提醒",
        "六、你的持仓",
    ]
    assert "sector_opportunities" in evidence.missing_data
    assert evidence.confidence < 0.9
    assert any(metric.key == "portfolio.total_change_pct" for metric in evidence.metrics)


def test_prompt_payload_includes_section_briefs_for_llm_writing_plan():
    payload = build_report_evidence(_analysis_result()).to_prompt_payload()

    assert "section_briefs" in payload
    briefs = payload["section_briefs"]
    assert isinstance(briefs, list)
    assert briefs[1]["key"] == "trend"
    assert "trend.standing_line_ratio" in briefs[1]["evidence_keys"]
    assert briefs[2]["conclusion_hint"] == "暂无明确机会"


def test_evidence_payload_includes_challenge_review_for_risk_control():
    payload = build_report_evidence(_analysis_result()).to_prompt_payload()

    review = payload["challenge_review"]

    assert review["posture"] == "balanced"
    assert "不得承诺收益" in review["action_boundaries"][0]
    assert "sector_opportunities" in review["missing_data_warnings"]
    assert any("不编造强势板块" in item for item in review["must_address"])


def test_challenge_review_turns_defensive_when_risk_flags_exist():
    analysis = _analysis_result()
    analysis["risk_alerts"] = [
        {"alert_type": "异常波动", "message": "主要指数波动放大", "affected_assets": ["sh000001"]}
    ]

    review = build_report_evidence(analysis).challenge_review

    assert review is not None
    assert review.posture == "defensive"
    assert any("先解释风险信号" in item for item in review.must_address)


def test_daily_prompt_includes_challenge_review_payload():
    _, user_prompt = build_daily_report_prompt(_analysis_result())

    assert "challenge_review" in user_prompt
    assert "不得承诺收益" in user_prompt


def test_daily_prompt_includes_usable_previous_report_context():
    _, user_prompt = build_daily_report_prompt(
        _analysis_result(),
        previous_report_context=ReportMemoryContext(
            has_history=True,
            usable=True,
            previous_as_of_date="2026-05-03",
            previous_report_period="weekly",
            quality_grade="A",
            quality_score=96,
            summary="上一期周报日期为2026-05-03，质量为A/96，可作为弱复盘依据。",
        ),
    )

    assert "previous_report_context" in user_prompt
    assert "2026-05-03" in user_prompt
    assert "usable" in user_prompt


def test_daily_prompt_omits_previous_report_context_without_history():
    _, user_prompt = build_daily_report_prompt(
        _analysis_result(),
        previous_report_context=ReportMemoryContext(has_history=False),
    )

    assert '"previous_report_context":' not in user_prompt


def test_verifier_accepts_supported_percent_claims():
    evidence = build_report_evidence(_analysis_result())

    result = ReportVerifier().verify(_six_section_report(), evidence)

    assert result.passed
    assert not [finding for finding in result.findings if finding.code == "unsupported_numeric_claim"]


def test_verifier_flags_unsupported_percent_and_absolute_advice():
    evidence = build_report_evidence(_analysis_result())

    result = ReportVerifier().verify(_six_section_report("下周收益18.00%，稳赚。"), evidence)

    assert not result.passed
    assert {finding.code for finding in result.findings} >= {"unsupported_numeric_claim", "absolute_advice"}
    assert "数据质量提示" in append_quality_notes(_six_section_report("下周收益18.00%。"), result)


def test_report_evaluator_blocks_unsupported_numbers_and_absolute_advice():
    evidence = build_report_evidence(_analysis_result())
    report = _six_section_report("下周收益18.00%，稳赚。")
    verification = ReportVerifier().verify(report, evidence)

    score = ReportEvaluator().evaluate(report, evidence, verification)

    assert score.overall < 60
    assert score.grade == "D"
    assert score.blockers == ["unsupported_numeric_claim", "absolute_advice"]
    assert score.components["numeric_trace"] == 0
    assert score.components["action_safety"] == 0


def test_report_evaluator_scores_grounded_report_highly():
    evidence = build_report_evidence(_analysis_result())
    report = _six_section_report()
    verification = ReportVerifier().verify(report, evidence)

    score = ReportEvaluator().evaluate(report, evidence, verification)

    assert score.overall >= 90
    assert score.grade == "A"
    assert not score.blockers


def test_audit_log_builds_memory_context_from_previous_record(tmp_path):
    evidence = build_report_evidence(_analysis_result())
    verification = ReportVerifier().verify(_six_section_report(), evidence)
    audit_log = ReportAuditLog(tmp_path / "report-audit.jsonl")
    record = audit_log.append(report=_six_section_report(), evidence=evidence, verification=verification, source="llm")

    context = build_memory_context(record)

    assert context.has_history
    assert context.usable
    assert context.previous_as_of_date == "2026-05-10"
    assert context.quality_grade == "A"
    assert "上一期" in context.summary


def test_audit_log_filters_latest_context_by_period_and_date(tmp_path):
    audit_log = ReportAuditLog(tmp_path / "report-audit.jsonl")
    evidence = build_report_evidence(_analysis_result(), report_period="weekly")
    verification = ReportVerifier().verify(_six_section_report(), evidence)
    audit_log.append(report=_six_section_report(), evidence=evidence, verification=verification, source="llm")

    current_context = audit_log.latest_context(report_period="weekly", before_date="2026-05-10")
    future_context = audit_log.latest_context(report_period="weekly", before_date="2026-05-17")

    assert not current_context.has_history
    assert future_context.has_history
    assert future_context.previous_as_of_date == "2026-05-10"


def test_verifier_warns_when_section_body_is_empty():
    evidence = build_report_evidence(_analysis_result())
    report = """📊 2026-05-10 投资周报
一、本周概览
二、方向信号
站线比例为62.00%。
三、板块机会
暂无明确机会。
四、估值温度
PE分位数为45.00%。
五、风险提醒
暂无新增风险。
六、你的持仓
组合当前收益12.00%。"""

    result = ReportVerifier().verify(report, evidence)

    assert any(finding.code == "empty_section" for finding in result.findings)


@pytest.mark.asyncio
async def test_report_generator_appends_quality_notes_to_llm_output(tmp_path):
    class LLMWithUnsupportedClaim:
        max_tokens = 4096

        async def generate(self, *args, **kwargs):
            return _six_section_report("下周收益18.00%。")

    audit_log = ReportAuditLog(tmp_path / "report-audit.jsonl")
    report = await ReportGenerator(LLMWithUnsupportedClaim(), audit_log=audit_log).generate_daily_report(_analysis_result())

    assert "数据质量提示" in report
    assert "18.00%" in report


@pytest.mark.asyncio
async def test_report_generator_writes_audit_record(tmp_path):
    class StableLLM:
        max_tokens = 4096

        async def generate(self, *args, **kwargs):
            return _six_section_report()

    audit_log = ReportAuditLog(tmp_path / "report-audit.jsonl")

    await ReportGenerator(StableLLM(), audit_log=audit_log).generate_daily_report(_analysis_result())

    records = audit_log.read_recent(limit=1)
    assert len(records) == 1
    assert records[0].source == "llm"
    assert records[0].as_of_date == "2026-05-10"
    assert records[0].challenge_posture == "balanced"
    assert records[0].verification_passed
    assert records[0].quality_score["grade"] == "A"
    assert records[0].report_hash
