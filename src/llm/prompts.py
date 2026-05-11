"""Prompt templates for structured JSON investment report generation."""

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

你必须输出一个严格的 JSON 对象，格式如下：

```json
{{
  "date": "YYYY-MM-DD",
  "period": "daily|weekly|monthly",
  "period_label": "{report_label}",
  "change_summary": "1-2句话说明本期相对上期最重要的变化，如方向转换、关键指标变动。首期留空。",
  "direction": "进攻|防守|观望",
  "direction_reason": "简明理由",
  "risk_level": "低|中|高",
  "sections": [
    {{
      "section_id": "overview",
      "title": "一、{scope}概览",
      "conclusion": "一句话结论",
      "body": "正文，50-150字",
      "cited_metrics": [
        {{"evidence_key": "overview.index.sh000300_change_pct", "label": "沪深300涨跌", "value": 0.5, "previous_value": null}}
      ],
      "missing_data_disclosure": []
    }},
    {{
      "section_id": "trend",
      "title": "二、方向信号",
      "conclusion": "一句话结论",
      "body": "正文",
      "cited_metrics": [],
      "missing_data_disclosure": []
    }},
    {{
      "section_id": "sector",
      "title": "三、板块机会",
      "conclusion": "一句话结论",
      "body": "正文",
      "cited_metrics": [],
      "missing_data_disclosure": []
    }},
    {{
      "section_id": "valuation",
      "title": "四、估值温度",
      "conclusion": "一句话结论",
      "body": "正文",
      "cited_metrics": [],
      "missing_data_disclosure": []
    }},
    {{
      "section_id": "risk",
      "title": "五、风险提醒",
      "conclusion": "一句话结论",
      "body": "正文",
      "cited_metrics": [],
      "missing_data_disclosure": []
    }},
    {{
      "section_id": "portfolio",
      "title": "六、你的持仓",
      "conclusion": "一句话结论",
      "body": "正文",
      "cited_metrics": [],
      "missing_data_disclosure": []
    }}
  ],
  "action_items": ["具体可操作建议1", "具体可操作建议2"],
  "missing_data_summary": ["缺失字段1", "缺失字段2"]
}}
```

写作约束：
- change_summary 是本报告最重要的部分，必须基于 evidence.change_summary 和 previous_report_context 提炼本期最关键的变化。如果 change_summary.has_previous=true 且 usable=true，优先写变化；如果首期则留空。
- cited_metrics 中每个 metric 的 evidence_key 必须精确匹配 evidence.metrics 中的 key 字段。value 必须来自 evidence.metrics 中对应的 value。previous_value 若有则填，否则 null。
- 可以使用金融术语，但首次出现时必须用一句话解释。
- 先说结论再解释原因。
- 给出具体标的代码和操作建议，不模糊。
- 只基于证据包生成，不编造缺失数据；缺失时在 missing_data_disclosure 中写"暂无数据"。
- 报告中的百分比、收益、分位数等数字必须来自证据包 metrics 或 sections。
- 必须处理 evidence.challenge_review.must_address 和 action_boundaries 中的限制。
- 不使用"稳赚、必涨、保证收益、无风险、满仓买入"等绝对化投资表述。
- action_items 必须是具体、可执行的操作建议，如"定投沪深300维持每月5000元"、"科创50若跌破0.78则减仓50%"。"""


REPORT_USER_PROMPT_TEMPLATE = """请根据以下证据包生成一份结构化的基金/ETF投资{report_label} JSON。

输出要求：
1. 严格遵循 JSON schema，所有字段必填。
2. 优先按照 evidence.section_briefs 中每个章节的 objective、conclusion_hint、evidence_keys 写作。
3. 写作前先吸收 evidence.challenge_review 的反方审查：有风险或缺失数据时语气更谨慎。
4. 如果 previous_report_context.has_history=true，可在 change_summary 中引用上一期判断；若 usable=false，只能写成弱参考。
5. 如果 change_summary.has_previous=true，优先在 change_summary 中写出本期相对上一期最重要的变化。
6. cited_metrics 只引用证据包中实际存在的指标，evidence_key 必须精确匹配。
7. 板块机会必须尽量给出ETF代码和名称；数据缺失时在 missing_data_disclosure 中写"暂无明确机会"。
8. 风险提醒必须说明触发信号和需要采取的动作；无风险时 body 写"暂无新增风险"并设置 risk_level 为"低"。

证据包JSON：
```json
{evidence_json}
```"""


def build_structured_report_prompt(
    analysis_result: dict[str, object],
    report_period: ReportPeriod | str | None = None,
    previous_report_context: object | None = None,
    change_summary: object | None = None,
) -> tuple[str, str]:
    """Build system and user prompts for a structured JSON investment report.

    Returns:
        ``(system_prompt, user_prompt)`` ready for ``LLMClient.generate_json()``.
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

    evidence_json = json.dumps(report_input, ensure_ascii=False, indent=2, default=str)
    user_prompt = REPORT_USER_PROMPT_TEMPLATE.format(
        report_label=label,
        evidence_json=evidence_json,
    )

    return system_prompt, user_prompt


# ---------------------------------------------------------------------------
# Backward-compatible original prompt builder (text-based, kept for fallback)
# ---------------------------------------------------------------------------


REPORT_SYSTEM_PROMPT_TEXT = """你是个人基金ETF投资建议系统的{report_label}撰写助手，面向基金/ETF个人投资者输出中文{report_label}。

请严格使用以下6段式结构：
一、{scope}概览 — 先报主要指数当日涨跌（沪深300/创业板指等）和成交量变化，再列关键事件2-3条（从evidence.key_events提取），后给总体判断（进攻/防守/观望）并简述理由。如有市场宽度（涨跌板块比例）和资金流向（北向资金等），一并说明。
二、方向信号 — 趋势+情绪综合分析→仓位建议（含术语解释）
三、板块机会 — {scope}强势板块、值得关注的ETF（给代码和名称）。如有贵金属/商品数据（黄金、白银），需在本节分析贵金属走势、与股市的相关性或背离信号。
四、估值温度 — 当前贵还是便宜、定投是否继续（解释分位数含义）
五、风险提醒 — 需要警惕的信号及原因
六、你的持仓 — 当前收益、最新涨跌、是否需要调整

写作约束：
- 可以使用金融术语，但必须在首次出现时用一句话解释。
- 先说结论再解释原因，例如"建议减仓，因为..."。
- 给出具体标的代码和操作建议，不模糊。
- {report_label}总长度控制在手机一屏以内，约300-600字。
- 不使用生活化比喻，保持专业简洁。
- 只基于证据包生成，不编造缺失数据；缺失时明确写"暂无数据"。
- 报告中的百分比、收益、分位数等数字必须来自证据包 metrics 或 sections。
- 必须处理 evidence.challenge_review.must_address 和 action_boundaries 中的限制。
- 不使用"稳赚、必涨、保证收益、无风险、满仓买入"等绝对化投资表述。"""


def build_daily_report_prompt(
    analysis_result: dict[str, object],
    report_period: ReportPeriod | str | None = None,
    previous_report_context: object | None = None,
    change_summary: object | None = None,
) -> tuple[str, str]:
    """Build text-based prompts for backward compatibility (fallback).
    
    Prefer ``build_structured_report_prompt()`` for new callers.
    """

    result: dict[str, object] = analysis_result or {}
    period = normalize_report_period(report_period) if report_period is not None else select_report_period(result.get("date"))
    label = report_period_label(period)
    scope = report_period_scope(period)
    evidence = build_report_evidence(result, report_period=period)
    system_prompt = REPORT_SYSTEM_PROMPT_TEXT.format(report_label=label, scope=scope)
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
1. 必须按"📊 日期 投资{label}"开头，并使用"一、{scope}概览"到"六、你的持仓"六个标题。
2. 每段先给结论，再给原因；仓位、定投、持仓调整建议要明确。
3. 板块机会必须尽量给出ETF代码和名称；数据缺失时写"暂无明确机会"。
4. 风险提醒必须说明触发信号和需要采取的动作；无风险时写"暂无新增风险"。
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
