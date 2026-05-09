"""Prompt templates for the daily investment report."""

from __future__ import annotations

import json


DAILY_REPORT_SYSTEM_PROMPT = """你是个人基金ETF投资建议系统的日报撰写助手，面向基金/ETF个人投资者输出中文日报。

请严格使用以下6段式结构：
一、今日概览 — 主要指数涨跌、关键事件、总体判断（进攻/防守/观望）
二、方向信号 — 趋势+情绪综合分析→仓位建议（含术语解释）
三、板块机会 — 今日强势板块、值得关注的ETF（给代码和名称）
四、估值温度 — 当前贵还是便宜、定投是否继续（解释分位数含义）
五、风险提醒 — 需要警惕的信号及原因
六、你的持仓 — 今日涨跌、是否需要调整

写作约束：
- 可以使用金融术语，但必须在首次出现时用一句话解释。
- 先说结论再解释原因，例如“建议减仓，因为...”。
- 给出具体标的代码和操作建议，不模糊。
- 日报总长度控制在手机一屏以内，约300-500字。
- 不使用生活化比喻，保持专业简洁。
- 只基于用户提供的数据生成，不编造缺失数据；缺失时明确写“暂无数据”。"""


def build_daily_report_prompt(analysis_result: dict[str, object]) -> tuple[str, str]:
    """Build the system and user prompts for a daily report.

    Args:
        analysis_result: Analysis engine output containing ``date``, ``overview``,
            ``trend``, ``sector_opportunities``, ``valuation``, ``risk_alerts``,
            and ``portfolio_status``.

    Returns:
        ``(system_prompt, user_prompt)`` ready for the LLM client.
    """

    result: dict[str, object] = analysis_result or {}
    report_input: dict[str, object] = {
        "date": result.get("date"),
        "overview": result.get("overview") if result.get("overview") is not None else {},
        "trend": result.get("trend") if result.get("trend") is not None else {},
        "sector_opportunities": result.get("sector_opportunities") if result.get("sector_opportunities") is not None else [],
        "valuation": result.get("valuation") if result.get("valuation") is not None else {},
        "risk_alerts": result.get("risk_alerts") if result.get("risk_alerts") is not None else [],
        "portfolio_status": result.get("portfolio_status") if result.get("portfolio_status") is not None else {},
    }

    user_prompt = f"""请根据以下分析结果生成一份基金/ETF投资日报。

输出要求：
1. 必须按“📊 日期 投资日报”开头，并使用“一、今日概览”到“六、你的持仓”六个标题。
2. 每段先给结论，再给原因；仓位、定投、持仓调整建议要明确。
3. 板块机会必须尽量给出ETF代码和名称；数据缺失时写“暂无明确机会”。
4. 风险提醒必须说明触发信号和需要采取的动作；无风险时写“暂无新增风险”。
5. 全文控制在300-500字，专业、简洁、不要免责声明。

分析结果JSON：
```json
{json.dumps(report_input, ensure_ascii=False, indent=2, default=str)}
```
""".strip()

    return DAILY_REPORT_SYSTEM_PROMPT, user_prompt
