"""Build compact, auditable evidence packets for report generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, cast

from src.llm.report_period import (
    ReportPeriod,
    normalize_report_period,
    report_period_label,
    report_period_scope,
    select_report_period,
)


@dataclass(frozen=True)
class EvidenceMetric:
    """A traceable number or fact the report may cite."""

    key: str
    label: str
    value: object
    source_path: str
    as_of: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class ReportSectionBrief:
    """A deterministic writing brief for one report section."""

    key: str
    title: str
    objective: str
    conclusion_hint: str
    evidence_keys: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportChallengeReview:
    """Skeptical review notes the writer must address before final wording."""

    posture: str
    must_address: list[str] = field(default_factory=list)
    skeptical_questions: list[str] = field(default_factory=list)
    action_boundaries: list[str] = field(default_factory=list)
    missing_data_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportEvidence:
    """LLM-facing report input with explicit source and quality metadata."""

    as_of_date: str
    report_period: str
    report_label: str
    report_scope: str
    sections: dict[str, object]
    metrics: list[EvidenceMetric] = field(default_factory=list)
    section_briefs: list[ReportSectionBrief] = field(default_factory=list)
    challenge_review: ReportChallengeReview | None = None
    missing_data: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    confidence: float = 0.7

    def to_prompt_payload(self) -> dict[str, object]:
        """Return a compact JSON-serializable payload for prompt construction."""

        return {
            "as_of_date": self.as_of_date,
            "report_period": self.report_period,
            "report_label": self.report_label,
            "report_scope": self.report_scope,
            "sections": self.sections,
            "metrics": [asdict(metric) for metric in self.metrics],
            "section_briefs": [asdict(brief) for brief in self.section_briefs],
            "challenge_review": asdict(self.challenge_review) if self.challenge_review else None,
            "missing_data": self.missing_data,
            "risk_flags": self.risk_flags,
            "source_notes": self.source_notes,
            "confidence": round(self.confidence, 2),
        }

    def supported_percent_values(self) -> list[float]:
        """Percent-form values that can support numeric claims in prose."""

        values: list[float] = []
        for metric in self.metrics:
            number = _number(metric.value)
            if number is None:
                continue
            key = metric.key.lower()
            unit = (metric.unit or "").lower()
            if unit == "percent" or any(token in key for token in ("pct", "percent", "ratio", "drawdown", "spread")):
                values.append(_to_percent(number))
        return values


def build_report_evidence(
    analysis_result: Mapping[str, object] | object,
    report_period: ReportPeriod | str | None = None,
) -> ReportEvidence:
    """Convert analysis output into an auditable evidence packet."""

    result = _record(analysis_result)
    date = str(result.get("date") or "今日")
    period = normalize_report_period(report_period) if report_period is not None else select_report_period(date)
    label = report_period_label(period)
    scope = report_period_scope(period)

    sections: dict[str, object] = {
        "overview": _record(result.get("overview")),
        "trend": _record(result.get("trend")),
        "sector_opportunities": _records(result.get("sector_opportunities")),
        "valuation": _record(result.get("valuation")),
        "risk_alerts": _records(result.get("risk_alerts")),
        "portfolio_status": _record(result.get("portfolio_status")),
        "risk_metrics": _record(result.get("risk_metrics")),
        "precious_metals": _record(result.get("precious_metals")),
    }

    metrics = _collect_metrics(sections, date)
    missing_data = _collect_missing_data(sections)
    risk_flags = _collect_risk_flags(sections, result)
    section_briefs = _build_section_briefs(scope, sections, metrics, missing_data, risk_flags)
    source_notes = _collect_source_notes(sections, metrics)
    confidence = _estimate_confidence(metrics, missing_data, risk_flags)
    challenge_review = _build_challenge_review(sections, missing_data, risk_flags, confidence)

    return ReportEvidence(
        as_of_date=date,
        report_period=period,
        report_label=label,
        report_scope=scope,
        sections=sections,
        metrics=metrics,
        section_briefs=section_briefs,
        challenge_review=challenge_review,
        missing_data=missing_data,
        risk_flags=risk_flags,
        source_notes=source_notes,
        confidence=confidence,
    )


def _collect_metrics(sections: Mapping[str, object], as_of: str) -> list[EvidenceMetric]:
    metrics: list[EvidenceMetric] = []

    trend = _record(sections.get("trend"))
    _add_metric(metrics, "trend.standing_line_ratio", "站线比例", trend.get("standing_line_ratio"), "sections.trend.standing_line_ratio", as_of, "percent")
    _add_metric(metrics, "trend.sentiment_score", "情绪分数", trend.get("sentiment_score"), "sections.trend.sentiment_score", as_of)
    _add_metric(metrics, "trend.confidence", "趋势置信度", trend.get("confidence"), "sections.trend.confidence", as_of)

    valuation = _record(sections.get("valuation"))
    _add_metric(metrics, "valuation.pe_percentile", "PE分位数", valuation.get("pe_percentile"), "sections.valuation.pe_percentile", as_of, "percent")
    _add_metric(metrics, "valuation.bond_equity_spread", "股债利差", valuation.get("bond_equity_spread"), "sections.valuation.bond_equity_spread", as_of, "percent")

    portfolio = _record(sections.get("portfolio_status"))
    _add_metric(metrics, "portfolio.total_change_pct", "组合收益", portfolio.get("total_change_pct"), "sections.portfolio_status.total_change_pct", as_of, "percent")
    _add_metric(metrics, "portfolio.total_profit_loss", "组合盈亏", portfolio.get("total_profit_loss"), "sections.portfolio_status.total_profit_loss", as_of)
    for index, holding in enumerate(_records(portfolio.get("holdings"))):
        code = str(holding.get("code") or holding.get("symbol") or index)
        _add_metric(metrics, f"portfolio.holdings.{code}.change_pct", f"{code} 最新涨跌", holding.get("change_pct"), f"sections.portfolio_status.holdings.{index}.change_pct", as_of, "percent")
        _add_metric(metrics, f"portfolio.holdings.{code}.profit_loss_pct", f"{code} 持仓收益", holding.get("profit_loss_pct"), f"sections.portfolio_status.holdings.{index}.profit_loss_pct", as_of, "percent")

    risk_metrics = _record(sections.get("risk_metrics"))
    _add_metric(metrics, "risk.max_drawdown", "最大回撤", risk_metrics.get("max_drawdown"), "sections.risk_metrics.max_drawdown", as_of, "percent")

    pm = _record(sections.get("precious_metals"))
    _add_metric(metrics, "pm.gold_spot_price", "Au99.99价格(元/克)", pm.get("gold_spot_price"), "sections.precious_metals.gold_spot_price", as_of, "yuan/g")
    _add_metric(metrics, "pm.gold_spot_change_5d", "Au99.99 5日涨跌", pm.get("gold_spot_change_5d"), "sections.precious_metals.gold_spot_change_5d", as_of, "percent")
    _add_metric(metrics, "pm.comex_gold_change_pct", "COMEX金涨跌", pm.get("comex_gold_change_pct"), "sections.precious_metals.comex_gold_change_pct", as_of, "percent")
    _add_metric(metrics, "pm.gold_concept_change_pct", "黄金概念涨跌", pm.get("gold_concept_change_pct"), "sections.precious_metals.gold_concept_change_pct", as_of, "percent")
    return metrics


def _build_section_briefs(
    scope: str,
    sections: Mapping[str, object],
    metrics: Sequence[EvidenceMetric],
    missing_data: Sequence[str],
    risk_flags: Sequence[str],
) -> list[ReportSectionBrief]:
    overview = _record(sections.get("overview"))
    trend = _record(sections.get("trend"))
    opportunities = _records(sections.get("sector_opportunities"))
    valuation = _record(sections.get("valuation"))
    risk_alerts = _records(sections.get("risk_alerts"))
    portfolio = _record(sections.get("portfolio_status"))

    return [
        ReportSectionBrief(
            key="overview",
            title=f"一、{scope}概览",
            objective="用一句结论说明市场状态，再解释关键事件和进攻/防守/观望判断。",
            conclusion_hint=str(overview.get("direction") or "观望"),
            evidence_keys=_metric_keys(metrics, ()),
            missing_data=_missing_for(missing_data, "overview"),
            risk_notes=risk_flags[:2],
        ),
        ReportSectionBrief(
            key="trend",
            title="二、方向信号",
            objective="把趋势、情绪和仓位建议连成因果链，首次出现术语时简短解释。",
            conclusion_hint=str(trend.get("position_advice") or "维持均衡仓位"),
            evidence_keys=_metric_keys(metrics, ("trend.",)),
            missing_data=_missing_for(missing_data, "trend"),
            risk_notes=[],
        ),
        ReportSectionBrief(
            key="sector_opportunities",
            title="三、板块机会",
            objective="只列证据支持的强势板块和ETF；没有机会时明确写暂无明确机会。",
            conclusion_hint="暂无明确机会" if not opportunities else f"关注{len(opportunities)}个板块机会",
            evidence_keys=[],
            missing_data=_missing_for(missing_data, "sector_opportunities"),
            risk_notes=[],
        ),
        ReportSectionBrief(
            key="valuation",
            title="四、估值温度",
            objective="说明当前贵还是便宜，并给出定投是否继续的理由。",
            conclusion_hint=str(valuation.get("overall_level") or "暂无估值判断"),
            evidence_keys=_metric_keys(metrics, ("valuation.",)),
            missing_data=_missing_for(missing_data, "valuation"),
            risk_notes=[],
        ),
        ReportSectionBrief(
            key="risk_alerts",
            title="五、风险提醒",
            objective="优先写触发信号和动作建议；无风险时写暂无新增风险。",
            conclusion_hint="暂无新增风险" if not risk_alerts and not risk_flags else "存在需要关注的风险信号",
            evidence_keys=_metric_keys(metrics, ("risk.",)),
            missing_data=[],
            risk_notes=risk_flags[:4],
        ),
        ReportSectionBrief(
            key="portfolio_status",
            title="六、你的持仓",
            objective="说明组合收益、持仓变化和是否需要调整；没有持仓明细时保持中性表述。",
            conclusion_hint="暂无持仓明细" if not portfolio.get("holdings") else "检查重点持仓",
            evidence_keys=_metric_keys(metrics, ("portfolio.",)),
            missing_data=_missing_for(missing_data, "portfolio"),
            risk_notes=[],
        ),
    ]


def _metric_keys(metrics: Sequence[EvidenceMetric], prefixes: Sequence[str]) -> list[str]:
    if not prefixes:
        return [metric.key for metric in metrics[:3]]
    return [metric.key for metric in metrics if any(metric.key.startswith(prefix) for prefix in prefixes)]


def _missing_for(missing_data: Sequence[str], prefix: str) -> list[str]:
    return [field for field in missing_data if field.startswith(prefix)]


def _build_challenge_review(
    sections: Mapping[str, object],
    missing_data: Sequence[str],
    risk_flags: Sequence[str],
    confidence: float,
) -> ReportChallengeReview:
    trend = _record(sections.get("trend"))
    opportunities = _records(sections.get("sector_opportunities"))
    portfolio = _record(sections.get("portfolio_status"))
    valuation = _record(sections.get("valuation"))

    must_address: list[str] = []
    skeptical_questions: list[str] = []
    action_boundaries: list[str] = [
        "不得承诺收益、不得使用必涨/稳赚/无风险等绝对化表述。",
        "仓位和定投建议必须写成条件性建议，并说明触发依据。",
    ]

    if missing_data:
        must_address.append("披露缺失数据，并降低相关结论的确定性。")
    if risk_flags:
        must_address.append("先解释风险信号，再给操作动作，避免只写机会。")
        skeptical_questions.append("这些风险是否足以推翻进攻性仓位建议？")
    if not opportunities:
        must_address.append("没有板块机会证据时，不编造强势板块或ETF代码。")
    if not portfolio:
        must_address.append("没有组合持仓数据时，不给出具体持仓调整。")
    if not valuation.get("overall_level"):
        skeptical_questions.append("估值数据不足时，定投建议是否应该保持观察？")
    if confidence < 0.65:
        must_address.append("整体证据置信度偏低，报告语气应偏谨慎。")

    position_advice = str(trend.get("position_advice") or "")
    if any(word in position_advice for word in ("加仓", "进攻", "提高")) and (risk_flags or missing_data):
        skeptical_questions.append("趋势建议偏积极，但风险或缺失数据是否要求降低仓位？")

    posture = "defensive" if risk_flags or confidence < 0.65 else "balanced"
    if not risk_flags:
        skeptical_questions.append("暂无风险仅表示未检测到新增风险，不代表风险消失。")

    return ReportChallengeReview(
        posture=posture,
        must_address=must_address,
        skeptical_questions=skeptical_questions,
        action_boundaries=action_boundaries,
        missing_data_warnings=list(missing_data),
    )


def _collect_missing_data(sections: Mapping[str, object]) -> list[str]:
    missing: list[str] = []
    if not _record(sections.get("overview")).get("summary"):
        missing.append("overview.summary")
    if not _record(sections.get("trend")).get("ma_alignment"):
        missing.append("trend.ma_alignment")
    if not _records(sections.get("sector_opportunities")):
        missing.append("sector_opportunities")
    if not _record(sections.get("valuation")).get("overall_level"):
        missing.append("valuation.overall_level")
    if not _record(sections.get("portfolio_status")):
        missing.append("portfolio_status")
    return missing


def _collect_risk_flags(sections: Mapping[str, object], result: Mapping[str, object]) -> list[str]:
    flags: list[str] = []
    for item in _records(sections.get("risk_alerts")):
        message = item.get("message") or item.get("alert_type") or item.get("type")
        if message:
            flags.append(str(message))
    for key in ("validation_errors", "validation_warnings"):
        for item in _sequence(result.get(key)):
            flags.append(f"{key}: {item}")
    return flags


def _collect_source_notes(sections: Mapping[str, object], metrics: Sequence[EvidenceMetric]) -> list[str]:
    notes = [f"{name}: {'available' if value else 'missing'}" for name, value in sections.items()]
    notes.extend(f"{metric.key} <- {metric.source_path}" for metric in metrics)
    return notes


def _estimate_confidence(metrics: Sequence[EvidenceMetric], missing_data: Sequence[str], risk_flags: Sequence[str]) -> float:
    score = 0.9
    score -= min(len(missing_data) * 0.06, 0.3)
    score -= min(len(risk_flags) * 0.03, 0.15)
    if len(metrics) < 3:
        score -= 0.1
    return max(0.35, min(0.95, score))


def _add_metric(
    metrics: list[EvidenceMetric],
    key: str,
    label: str,
    value: object,
    source_path: str,
    as_of: str,
    unit: str | None = None,
) -> None:
    if value is None:
        return
    metrics.append(EvidenceMetric(key=key, label=label, value=value, source_path=source_path, as_of=as_of, unit=unit))


def _record(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if is_dataclass(value) and not isinstance(value, type):
        return cast(dict[str, object], asdict(value))
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): item for key, item in mapping.items()}
    return {}


def _records(value: object) -> list[dict[str, object]]:
    return [_record(item) for item in _sequence(value)]


def _sequence(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(cast(Mapping[object, object], value).values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_percent(value: float) -> float:
    return value * 100 if abs(value) <= 1 else value
