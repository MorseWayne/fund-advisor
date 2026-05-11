"""
Render a StructuredReport to delivery-ready markdown text.

The renderer places the change summary prominently at the top, followed by
the six sections in order. It ensures the output is compact enough for
mobile chat reading while preserving all actionable information.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm.report_schema import ReportSection, StructuredReport


def render_structured_report(report: "StructuredReport") -> str:
    """Convert a StructuredReport into a markdown-formatted delivery string."""

    lines: list[str] = []

    # Header
    lines.append(f"📊 {report.date} 全球市场{report.period_label}")
    lines.append("")

    # Change summary — front-loaded, most important
    if report.change_summary:
        lines.append(f"📌 本期变化：{report.change_summary}")
        lines.append("")

    # Direction signal — prominent
    direction_emoji = {"进攻": "🟢", "防守": "🔴", "观望": "🟡"}.get(report.direction, "⚪")
    lines.append(f"**总体判断：{direction_emoji} {report.direction}** — {report.direction_reason}")
    lines.append("")

    # Sections
    for section in report.sections:
        lines.append(f"**{section.title}**")
        lines.append(section.body)
        if section.missing_data_disclosure:
            disclosure = "；".join(section.missing_data_disclosure[:2])
            lines.append(f"⚠️ 数据缺失：{disclosure}")
        lines.append("")

    # Action items
    if report.action_items:
        lines.append("**📋 操作建议**")
        for item in report.action_items[:3]:
            lines.append(f"• {item}")
        lines.append("")

    # Risk and data quality footer
    footer_parts: list[str] = [f"风险等级：{report.risk_level}"]
    if report.missing_data_summary:
        missing = "、".join(report.missing_data_summary[:3])
        footer_parts.append(f"缺失数据：{missing}")
    lines.append("—" * 20)
    lines.append("  ".join(footer_parts))

    return "\n".join(lines).strip()


def _section_emoji(section_id: str) -> str:
    return {
        "overview": "📋",
        "trend": "📈",
        "sector": "🔥",
        "valuation": "🌡️",
        "risk": "⚠️",
        "portfolio": "💼",
    }.get(section_id, "•")
