"""
Pydantic models for structured investment report output.

The schema enforces the six-section format plus a front-loaded change summary.
Numbers must reference evidence metrics — the LLM must fill fields from the
evidence packet, not invent values.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KeyMetric(BaseModel):
    """A single traceable data point the report cites.

    The LLM must fill ``evidence_key`` to match a metric key in the evidence
    packet so the verifier can confirm the value is genuine.
    """

    evidence_key: str = Field(default="", description="Matching key in evidence.metrics")
    label: str = Field(default="", description="Human-readable label for this metric")
    value: float | None = Field(default=None, description="Current value")
    previous_value: float | None = Field(default=None, description="Previous value for change comparison")


class ReportSection(BaseModel):
    """One section of the six-part investment report."""

    section_id: str = Field(description="One of: overview, trend, sector, valuation, risk, portfolio")
    title: str = Field(description="Section heading, e.g. '一、今日概览'")
    conclusion: str = Field(description="One-sentence conclusion for this section")
    body: str = Field(description="Full section body, 50-150 characters")
    cited_metrics: list[KeyMetric] = Field(default_factory=list, description="Metrics cited in this section")
    missing_data_disclosure: list[str] = Field(default_factory=list, description="Missing data explicitly disclosed")


class StructuredReport(BaseModel):
    """Complete structured investment report ready for rendering and verification."""

    date: str = Field(description="Report date, YYYY-MM-DD")
    period: str = Field(description="daily, weekly, or monthly")
    period_label: str = Field(description="日报, 周报, or 月报")

    # Front-loaded change summary — the most important info first
    change_summary: str = Field(
        default="",
        description=(
            "1-2 sentence summary of key changes vs previous report. "
            "E.g. '方向由防守转为进攻，沪深300站上20日均线，PE分位从45%升至52%。' "
            "Empty if this is the first report."
        ),
    )

    direction: str = Field(description="Overall position: 进攻, 防守, or 观望")
    direction_reason: str = Field(description="Brief reason for the direction signal, max 40 chars")

    sections: list[ReportSection] = Field(description="Six report sections in order")

    risk_level: str = Field(default="中", description="Overall risk assessment: 低, 中, or 高")
    action_items: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable suggestions: e.g. '定投沪深300维持', '科创50若跌破0.78止损'",
    )
    missing_data_summary: list[str] = Field(
        default_factory=list,
        description="Fields where data is unavailable, for disclosure",
    )

    def to_markdown(self) -> str:
        """Render the structured report to markdown text for delivery."""
        from src.llm.report_renderer import render_structured_report
        return render_structured_report(self)
