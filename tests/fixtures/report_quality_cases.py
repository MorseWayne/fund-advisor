"""Regression cases for evidence-grounded report quality."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReportQualityCase:
    name: str
    analysis: dict[str, object]
    report: str
    expected_passed: bool
    expected_grade: str
    expected_codes: set[str] = field(default_factory=set)
    min_score: int = 0
    max_score: int = 100


def base_analysis() -> dict[str, object]:
    return {
        "date": "2026-05-10",
        "overview": {
            "direction": "观望",
            "summary": "市场震荡",
            "key_events": [],
            "index_snapshot": [],
            "market_breadth": {},
        },
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


def previous_analysis() -> dict[str, object]:
    analysis = base_analysis()
    analysis["date"] = "2026-05-03"
    analysis["trend"] = {
        "ma_alignment": "震荡",
        "standing_line_ratio": 0.50,
        "sentiment": "中性",
        "confidence": 0.65,
    }
    analysis["valuation"] = {"overall_level": "合理", "pe_percentile": 40.0, "continue_sip": True}
    return analysis


def risk_heating_analysis() -> dict[str, object]:
    analysis = base_analysis()
    analysis["trend"] = {
        "ma_alignment": "震荡偏弱",
        "standing_line_ratio": 0.44,
        "sentiment": "偏弱",
        "confidence": 0.66,
    }
    analysis["valuation"] = {"overall_level": "偏贵", "pe_percentile": 52.0, "continue_sip": False}
    analysis["risk_alerts"] = [{"message": "主要指数波动放大"}]
    analysis["risk_metrics"] = {"max_drawdown": 0.08}
    return analysis


def missing_data_analysis() -> dict[str, object]:
    analysis = deepcopy(base_analysis())
    analysis["overview"] = {"direction": "观望"}
    analysis["trend"] = {"standing_line_ratio": 0.62, "confidence": 0.55}
    analysis["valuation"] = {"pe_percentile": 45.0}
    analysis["portfolio_status"] = {}
    return analysis


def six_section_report(
    *,
    date: str = "2026-05-10",
    standing_line_ratio: float = 62.0,
    pe_percentile: float = 45.0,
    portfolio_change: float = 12.0,
    risk_text: str = "暂无新增风险。",
    extra: str = "",
) -> str:
    return f"""📊 {date} 投资周报

一、本周概览
总体观望。

二、方向信号
站线比例为{standing_line_ratio:.2f}%。

三、板块机会
暂无明确机会。

四、估值温度
PE分位数为{pe_percentile:.2f}%。

五、风险提醒
{risk_text}

六、你的持仓
组合当前收益{portfolio_change:.2f}%。{extra}"""


def previous_six_section_report() -> str:
    return six_section_report(date="2026-05-03", standing_line_ratio=50.0, pe_percentile=40.0)


REGRESSION_CASES: tuple[ReportQualityCase, ...] = (
    ReportQualityCase(
        name="balanced_supported_report",
        analysis=base_analysis(),
        report=six_section_report(),
        expected_passed=True,
        expected_grade="A",
        min_score=90,
    ),
    ReportQualityCase(
        name="hallucinated_numeric_and_absolute_advice",
        analysis=base_analysis(),
        report=six_section_report(extra="下周收益18.00%，稳赚。"),
        expected_passed=False,
        expected_grade="D",
        expected_codes={"unsupported_numeric_claim", "absolute_advice"},
        max_score=59,
    ),
    ReportQualityCase(
        name="missing_data_without_clear_disclosure",
        analysis=missing_data_analysis(),
        report="""📊 2026-05-10 投资周报

一、本周概览
总体观望。

二、方向信号
站线比例为62.00%。

三、板块机会
继续关注低估板块。

四、估值温度
PE分位数为45.00%。

五、风险提醒
保持均衡。

六、你的持仓
组合保持不变。""",
        expected_passed=True,
        expected_grade="B",
        expected_codes={"missing_data_not_disclosed"},
        min_score=75,
        max_score=89,
    ),
)
