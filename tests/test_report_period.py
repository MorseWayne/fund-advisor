import pytest

from src.llm.prompts import build_daily_report_prompt
from src.llm.report_generator import ReportGenerator
from src.llm.report_period import report_period_label, select_report_period


def test_select_report_period_prefers_month_end_over_weekend():
    assert select_report_period("2026-05-29") == "daily"
    assert select_report_period("2026-05-10") == "weekly"
    assert select_report_period("2026-05-31") == "monthly"
    assert report_period_label("monthly") == "月报"


def test_build_prompt_uses_weekly_report_language():
    system_prompt, user_prompt = build_daily_report_prompt({"date": "2026-05-10"})

    assert "周报撰写助手" in system_prompt
    assert "一、本周概览" in user_prompt
    assert "投资周报" in user_prompt


@pytest.mark.asyncio
async def test_fallback_report_uses_period_label_and_neutral_portfolio_wording():
    class FailingLLM:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("offline")

    generator = ReportGenerator(FailingLLM())
    report = await generator.generate_daily_report(
        {
            "date": "2026-05-10",
            "overview": {"direction": "观望", "summary": "数据不足", "key_events": []},
            "trend": {},
            "sector_opportunities": [],
            "valuation": {},
            "risk_alerts": [],
            "portfolio_status": {"total_change_pct": 0.12, "holdings": []},
        }
    )

    assert report.startswith("📊 2026-05-10 投资周报")
    assert "一、本周概览" in report
    assert "组合当前收益12.00%" in report