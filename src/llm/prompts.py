"""Prompt templates for the daily investment report."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.llm.report_period import (
    ReportPeriod,
    normalize_report_period,
    report_period_label,
    report_period_scope,
    select_report_period,
)
from src.reporting.evidence import build_report_evidence


REPORT_SYSTEM_PROMPT_TEMPLATE = """你是个人基金ETF投资建议系统的{report_label}撰写助手，面向基金/ETF个人投资者输出中文{report_label}。

请严格使用以下6段式结构：
一、{scope}概览 — 先报主要指数当日涨跌（沪深300/创业板指等）和成交量变化，再列关键事件2-3条（从evidence.key_events提取），后给总体判断（进攻/防守/观望）并简述理由。如有市场宽度（涨跌板块比例）和资金流向（北向资金等），一并说明。
二、方向信号 — 趋势+情绪综合分析→仓位建议（含术语解释）
三、板块机会 — {scope}强势板块、值得关注的ETF（给代码和名称）。如有贵金属/商品数据（黄金、白银），需在本节分析贵金属走势、与股市的相关性或背离信号。
四、估值温度 — 当前贵还是便宜、定投是否继续（解释分位数含义）
五、风险提醒 — 需要警惕的信号及原因
六、你的持仓 — 当前收益、最新涨跌、是否需要调整

写作约束：
- 可以使用金融术语，但必须在首次出现时用一句话解释。
- 先说结论再解释原因，例如“建议减仓，因为...”。
- 给出具体标的代码和操作建议，不模糊。
- {report_label}总长度控制在手机一屏以内，约300-600字。
- 不使用生活化比喻，保持专业简洁。
- 只基于证据包生成，不编造缺失数据；缺失时明确写“暂无数据”。
- 报告中的百分比、收益、分位数等数字必须来自证据包 metrics 或 sections。
- 必须处理 evidence.challenge_review.must_address 和 action_boundaries 中的限制。
- 不使用“稳赚、必涨、保证收益、无风险、满仓买入”等绝对化投资表述。"""


def build_daily_report_prompt(
    analysis_result: dict[str, object],
    report_period: ReportPeriod | str | None = None,
    previous_report_context: object | None = None,
    change_summary: object | None = None,
) -> tuple[str, str]:
    """Build the system and user prompts for a periodic investment report.

    Args:
        analysis_result: Analysis engine output containing ``date``, ``overview``,
            ``trend``, ``sector_opportunities``, ``valuation``, ``risk_alerts``,
            and ``portfolio_status``.

    Returns:
        ``(system_prompt, user_prompt)`` ready for the LLM client.
    """

    result: dict[str, object] = analysis_result or {}
    period = normalize_report_period(report_period) if report_period is not None else select_report_period(result.get("date"))
    label = report_period_label(period)
    scope = report_period_scope(period)
    evidence = build_report_evidence(result, report_period=period)
    system_prompt = REPORT_SYSTEM_PROMPT_TEMPLATE.format(report_label=label, scope=scope)
    report_input: dict[str, object] = {
        "date": result.get("date"),
        "report_period": period,
        "report_label": label,
        "report_scope": scope,
        "evidence": evidence.to_prompt_payload(),
    }
    memory_payload = _previous_context_payload(previous_report_context)
    if memory_payload.get("has_history"):
        report_input["previous_report_context"] = memory_payload
    change_payload = _optional_prompt_payload(change_summary)
    if change_payload.get("has_previous"):
        report_input["change_summary"] = change_payload

    user_prompt = f"""请根据以下证据包生成一份基金/ETF投资{label}。

输出要求：
1. 必须按“📊 日期 投资{label}”开头，并使用“一、{scope}概览”到“六、你的持仓”六个标题。
2. 每段先给结论，再给原因；仓位、定投、持仓调整建议要明确。
3. 板块机会必须尽量给出ETF代码和名称；数据缺失时写“暂无明确机会”。
4. 风险提醒必须说明触发信号和需要采取的动作；无风险时写“暂无新增风险”。
5. 如果没有历史聚合数据，持仓收益使用当前收益和最新涨跌，不编造{scope}累计收益。
6. 优先按照 evidence.section_briefs 中每个章节的 objective、conclusion_hint、evidence_keys 写作。
7. 写作前先吸收 evidence.challenge_review 的反方审查：有风险或缺失数据时语气更谨慎。
8. 如果 previous_report_context.has_history=true，可用一句话回顾上一期判断；若 usable=false，只能写成弱参考，不能强化其结论。
9. 如果 change_summary.has_previous=true，优先写出本期相对上一期最重要的变化；若 usable=false，只能写成弱变化提示。
10. 全文控制在300-600字，专业、简洁、不要免责声明。

证据包JSON：
```json
{json.dumps(report_input, ensure_ascii=False, indent=2, default=str)}
```
""".strip()

    return system_prompt, user_prompt


def _previous_context_payload(previous_report_context: object | None) -> dict[str, object]:
    return _optional_prompt_payload(previous_report_context, default={"has_history": False})


def _optional_prompt_payload(
    value: object | None,
    default: dict[str, object] | None = None,
) -> dict[str, object]:
    fallback = default if default is not None else {"has_previous": False}
    if value is None:
        return fallback
    to_prompt_payload = getattr(value, "to_prompt_payload", None)
    if callable(to_prompt_payload):
        payload = to_prompt_payload()
        return dict(payload) if isinstance(payload, Mapping) else fallback
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return fallback
