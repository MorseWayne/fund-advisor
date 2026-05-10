"""Detect meaningful changes between consecutive report evidence packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from src.reporting.audit import ReportAuditRecord, ReportMemoryContext
from src.reporting.evidence import ReportEvidence


TRACKED_METRICS: dict[str, float] = {
    "trend.standing_line_ratio": 5.0,
    "valuation.pe_percentile": 3.0,
    "portfolio.total_change_pct": 2.0,
    "risk.max_drawdown": 2.0,
}


@dataclass(frozen=True)
class ReportMetricChange:
    """One changed metric between the previous and current report."""

    key: str
    label: str
    previous: float
    current: float
    delta: float
    direction: str


@dataclass(frozen=True)
class ReportChangeSummary:
    """Prompt-safe summary of what changed since the previous report."""

    has_previous: bool
    usable: bool = False
    key_changes: list[str] = field(default_factory=list)
    improved_signals: list[str] = field(default_factory=list)
    deteriorated_signals: list[str] = field(default_factory=list)
    new_risks: list[str] = field(default_factory=list)
    resolved_risks: list[str] = field(default_factory=list)
    metric_changes: list[ReportMetricChange] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    def to_prompt_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["metric_changes"] = [asdict(item) for item in self.metric_changes]
        return payload


def build_change_summary(
    current: ReportEvidence,
    previous_record: ReportAuditRecord | None,
    previous_context: ReportMemoryContext | None = None,
) -> ReportChangeSummary:
    """Compare current evidence with the latest previous audit record."""

    if previous_record is None:
        return ReportChangeSummary(has_previous=False)

    usable = previous_context.usable if previous_context is not None else _record_usable(previous_record)
    previous_payload = previous_record.evidence_payload or {}
    previous_risks = set(str(item) for item in previous_record.risk_flags)
    current_risks = set(str(item) for item in current.risk_flags)
    new_risks = sorted(current_risks - previous_risks)
    resolved_risks = sorted(previous_risks - current_risks)
    metric_changes = _metric_changes(current, previous_payload)
    posture_change = _posture_change(current, previous_payload)

    key_changes: list[str] = []
    if posture_change:
        key_changes.append(posture_change)
    key_changes.extend(_metric_change_text(item) for item in metric_changes[:3])
    key_changes.extend(f"新增风险：{risk}" for risk in new_risks[:2])
    key_changes.extend(f"风险解除或未再出现：{risk}" for risk in resolved_risks[:2])

    improved, deteriorated = _classify_signals(metric_changes, new_risks, resolved_risks)
    cautions = list(previous_context.cautions) if previous_context is not None else []
    if not usable:
        cautions.append("上一期报告质量不足，变化总结只能作为弱参考。")

    return ReportChangeSummary(
        has_previous=True,
        usable=usable,
        key_changes=key_changes,
        improved_signals=improved,
        deteriorated_signals=deteriorated,
        new_risks=new_risks,
        resolved_risks=resolved_risks,
        metric_changes=metric_changes,
        cautions=cautions,
    )


def _metric_changes(current: ReportEvidence, previous_payload: Mapping[str, object]) -> list[ReportMetricChange]:
    previous_metrics = _metrics_by_key(previous_payload.get("metrics"))
    current_metrics = {metric.key: metric for metric in current.metrics}
    changes: list[ReportMetricChange] = []
    for key, threshold in TRACKED_METRICS.items():
        previous_metric = previous_metrics.get(key)
        current_metric = current_metrics.get(key)
        if previous_metric is None or current_metric is None:
            continue
        previous_value = _metric_number(previous_metric)
        current_value = _metric_number(current_metric)
        if previous_value is None or current_value is None:
            continue
        previous_percent = _to_percent(previous_value)
        current_percent = _to_percent(current_value)
        delta = current_percent - previous_percent
        if abs(delta) < threshold:
            continue
        changes.append(ReportMetricChange(
            key=key,
            label=str(getattr(current_metric, "label", key)),
            previous=round(previous_percent, 2),
            current=round(current_percent, 2),
            delta=round(delta, 2),
            direction="up" if delta > 0 else "down",
        ))
    return changes


def _metrics_by_key(raw_metrics: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(raw_metrics, list):
        return {}
    result: dict[str, Mapping[str, object]] = {}
    for item in raw_metrics:
        if isinstance(item, Mapping):
            key = str(item.get("key") or "")
            if key:
                result[key] = item
    return result


def _metric_number(metric: object) -> float | None:
    value = metric.get("value") if isinstance(metric, Mapping) else getattr(metric, "value", None)
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_percent(value: float) -> float:
    return value * 100 if abs(value) <= 1 else value


def _posture_change(current: ReportEvidence, previous_payload: Mapping[str, object]) -> str:
    current_posture = current.challenge_review.posture if current.challenge_review else None
    previous_review = previous_payload.get("challenge_review")
    previous_posture = previous_review.get("posture") if isinstance(previous_review, Mapping) else None
    if not current_posture or not previous_posture or current_posture == previous_posture:
        return ""
    return f"审查姿态由{previous_posture}变为{current_posture}"


def _metric_change_text(change: ReportMetricChange) -> str:
    direction_text = "上升" if change.direction == "up" else "下降"
    return f"{change.label}{direction_text}{abs(change.delta):.2f}个百分点"


def _classify_signals(
    metric_changes: list[ReportMetricChange],
    new_risks: list[str],
    resolved_risks: list[str],
) -> tuple[list[str], list[str]]:
    improved: list[str] = [f"风险解除：{risk}" for risk in resolved_risks[:2]]
    deteriorated: list[str] = [f"新增风险：{risk}" for risk in new_risks[:2]]
    for change in metric_changes:
        if change.key in {"trend.standing_line_ratio", "portfolio.total_change_pct"}:
            target = improved if change.delta > 0 else deteriorated
            target.append(_metric_change_text(change))
        elif change.key in {"valuation.pe_percentile", "risk.max_drawdown"}:
            target = deteriorated if change.delta > 0 else improved
            target.append(_metric_change_text(change))
    return improved, deteriorated


def _record_usable(record: ReportAuditRecord) -> bool:
    quality = record.quality_score or {}
    blockers = quality.get("blockers")
    return record.verification_passed and not blockers
