"""Analysis engine orchestrator.

The engine coordinates the pure indicator modules and returns plain dictionaries
whose top-level shape matches ``src.data.models.AnalysisResult`` field names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, cast

import numpy as np
import pandas as pd

from src.analysis.risk import (
    calc_correlation_matrix,
    calc_max_drawdown,
    check_drawdown_warning,
    detect_anomaly_volatility,
    detect_correlation_breakdown,
)
from src.analysis.rotation import (
    calc_fund_flow_analysis,
    calc_momentum_ranking,
    map_to_economic_cycle,
)
from src.analysis.trend import (
    calc_ma_alignment,
    calc_multi_timeframe_signal,
    calc_sentiment,
    calc_standing_line_ratio,
    calc_volume_confirmation,
)
from src.analysis.valuation import (
    assess_overall_valuation,
    calc_bond_equity_spread,
    calc_pe_percentile,
    detect_etf_premium_alerts,
)
from src.analysis.aggregator import apply_window_changes


DEFAULT_ANALYSIS_CONFIG: dict[str, Any] = {
    "trend": {"ma_periods": [5, 20, 60], "standing_line_threshold": 0.5},
    "risk": {
        "anomaly_threshold": 0.03,
        "max_drawdown_warning": 0.15,
        "correlation_warning": 0.8,
    },
    "valuation": {"pe_percentile_low": 30, "pe_percentile_high": 70},
}


class AnalysisEngine:
    """Coordinate trend, rotation, valuation, and risk calculations."""

    def __init__(self, config: Mapping[str, Any] | None = None, db: Any | None = None) -> None:
        self.config: dict[str, Any] = _deep_merge(DEFAULT_ANALYSIS_CONFIG, dict(config or {}))
        self.db = db

    def analyze(self, snapshot: Any, *, window_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run all indicator modules against a processed market snapshot.

        Missing inputs produce ``None`` metrics rather than exceptions. The return
        value uses plain dictionaries/lists and avoids Pydantic/dataclass models.

        If *window_snapshot* is provided, daily ``change_pct`` values on indices
        and ETFs are replaced with window-aggregated changes (e.g. weekly return).
        """

        snapshot = _record(snapshot)
        date = snapshot.get("date")
        indices = _records(snapshot.get("indices"), key_field="code")
        etfs = _records(snapshot.get("etfs"), key_field="code")
        sectors = _records(snapshot.get("sectors"), key_field="name")
        fund_flows = _record(snapshot.get("fund_flows"))
        macro = _record(snapshot.get("macro"))
        valuation_input = _record(snapshot.get("valuation"))
        precious_metals = _record(snapshot.get("precious_metals"))
        news = _news_headlines(snapshot)

        if window_snapshot:
            apply_window_changes(indices, etfs, window_snapshot)

        if self.db is not None:
            self._load_history_from_db(snapshot, indices, etfs)

        trend_result = self._analyze_trend(snapshot, indices, etfs, macro)
        rotation_result = self._analyze_rotation(sectors, fund_flows, macro)
        valuation_result = self._analyze_valuation(indices, etfs, macro, valuation_input)
        risk_alerts, risk_metrics = self._analyze_risk(snapshot, indices, etfs)
        gold_result = self._analyze_precious_metals(precious_metals)

        overview = self._build_overview(indices, sectors, fund_flows, macro, news, trend_result, valuation_result, risk_alerts)
        sector_opportunities = self._build_sector_opportunities(rotation_result.get("momentum_ranking"))

        return {
            "date": date,
            "overview": overview,
            "trend": trend_result,
            "sector_opportunities": sector_opportunities,
            "valuation": valuation_result,
            "risk_alerts": risk_alerts,
            "portfolio_status": None,
            "daily_report_text": "",
            "rotation": rotation_result,
            "risk_metrics": risk_metrics,
            "precious_metals": gold_result,
        }

    def _load_history_from_db(
        self,
        snapshot: dict[str, Any],
        indices: list[dict[str, Any]],
        etfs: list[dict[str, Any]],
    ) -> None:
        """Inject historical prices and ETF returns from the optional MarketDB."""

        db = self.db
        if db is None:
            return

        for index in indices:
            code = str(index.get("code") or index.get("symbol") or "")
            if not code:
                continue
            history = db.get_historical_index(code, days=252)
            if history and not _has_price_history(index):
                index["price_history"] = history

        etf_returns: dict[str, list[float]] = {}
        for etf in etfs:
            code = str(etf.get("code") or etf.get("symbol") or "")
            if not code:
                continue
            history = db.get_historical_etf(code, days=60)
            if history and not _has_price_history(etf):
                etf["price_history"] = history
            returns = _returns_from_price_history(etf.get("price_history") or history)
            if returns:
                etf_returns[code] = returns

        if indices:
            snapshot["indices"] = indices
        if etfs:
            snapshot["etfs"] = etfs
        if etf_returns:
            existing_returns = _returns_from_snapshot(snapshot, etfs)
            snapshot["etf_returns"] = {**existing_returns, **etf_returns}

    def _analyze_trend(
        self,
        snapshot: Mapping[str, Any],
        indices: list[dict[str, Any]],
        etfs: list[dict[str, Any]],
        macro: Mapping[str, Any],
    ) -> dict[str, Any]:
        ma_periods = list(_config_get(self.config, "trend", "ma_periods", default=[5, 20, 60]))
        price_series = _price_series_from_snapshot(snapshot, indices, db=self.db)
        ma_alignment = None
        if price_series is not None and len(price_series.dropna()) >= max(ma_periods):
            ma_alignment = calc_ma_alignment(price_series, ma_periods)

        standing_line_ratio = calc_standing_line_ratio(etfs) if etfs else None
        vix = _first_number(macro, ("vix", "VIX"))
        advance_decline_ratio = _advance_decline_ratio(macro, etfs)
        sentiment = (
            calc_sentiment(vix if vix is not None else 20.0, advance_decline_ratio if advance_decline_ratio is not None else 1.0)
            if vix is not None or advance_decline_ratio is not None
            else None
        )

        standing_threshold = float(_config_get(self.config, "trend", "standing_line_threshold", default=0.5))
        position_advice = _position_advice(ma_alignment, standing_line_ratio, standing_threshold)
        confidence = _trend_confidence(ma_alignment, standing_line_ratio, sentiment)

        # Volume-price confirmation
        volume_confirmation = None
        if price_series is not None and len(price_series) >= 20:
            vol_series = _volume_series_from_snapshot(snapshot, indices, db=self.db)
            volume_confirmation = calc_volume_confirmation(
                price_series, vol_series,
            )

        # Multi-timeframe signal
        multi_timeframe = None
        if price_series is not None and len(price_series) >= 125:
            multi_timeframe = calc_multi_timeframe_signal(price_series)

        return {
            "ma_alignment": ma_alignment,
            "standing_line_ratio": standing_line_ratio,
            "vix_level": vix,
            "sentiment": sentiment.get("level") if sentiment else None,
            "sentiment_score": sentiment.get("score") if sentiment else None,
            "position_advice": position_advice,
            "confidence": confidence,
            "volume_confirmation": volume_confirmation,
            "multi_timeframe": multi_timeframe,
        }

    def _analyze_rotation(
        self,
        sectors: list[dict[str, Any]],
        fund_flows: Mapping[str, Any],
        macro: Mapping[str, Any],
    ) -> dict[str, Any]:
        momentum_ranking = calc_momentum_ranking(sectors) if sectors else None
        fund_flow_analysis = calc_fund_flow_analysis(dict(fund_flows)) if fund_flows else None

        cpi_change = _first_number(macro, ("cpi_change", "cpi_yoy_change", "inflation_change"))
        gdp_growth = _first_number(macro, ("gdp_growth", "gdp_change", "growth_change"))
        economic_cycle = map_to_economic_cycle(cpi_change, gdp_growth) if cpi_change is not None and gdp_growth is not None else None

        return {
            "momentum_ranking": momentum_ranking,
            "fund_flow_analysis": fund_flow_analysis,
            "economic_cycle": economic_cycle,
        }

    def _analyze_valuation(
        self,
        indices: list[dict[str, Any]],
        etfs: list[dict[str, Any]],
        macro: Mapping[str, Any],
        valuation_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        primary_index = _primary_index(indices)
        current_pe = _first_number(valuation_input, ("current_pe", "pe_current", "index_pe", "pe_ratio"))
        if current_pe is None and primary_index:
            current_pe = _first_number(primary_index, ("pe_ratio", "pe_current"))

        pe_percentile = _first_number(valuation_input, ("pe_percentile", "index_pe_percentile"))
        if pe_percentile is None and primary_index:
            pe_percentile = _first_number(primary_index, ("pe_percentile",))
        if pe_percentile is None and current_pe is not None:
            historical_pe = _number_list(valuation_input.get("historical_pe") or valuation_input.get("pe_history"))
            if historical_pe:
                pe_percentile = calc_pe_percentile(historical_pe, current_pe)

        bond_yield = _first_number(macro, ("us10y", "cn10y", "bond_yield", "treasury_10y"))
        if bond_yield is not None and bond_yield > 1:
            bond_yield = bond_yield / 100.0
        bond_spread = calc_bond_equity_spread(current_pe, bond_yield) if current_pe is not None and bond_yield is not None else None
        etf_premium_alerts = detect_etf_premium_alerts(etfs) if etfs else []
        overall_level = assess_overall_valuation(pe_percentile, bond_spread) if pe_percentile is not None and bond_spread is not None else None

        return {
            "overall_level": overall_level,
            "pe_percentile": pe_percentile,
            "bond_equity_spread": bond_spread,
            "etf_premium_alerts": etf_premium_alerts,
            "continue_sip": overall_level in (None, "便宜", "合理"),
        }

    def _analyze_risk(
        self,
        snapshot: Mapping[str, Any],
        indices: list[dict[str, Any]],
        etfs: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        anomaly_threshold = float(_config_get(self.config, "risk", "anomaly_threshold", default=0.03))
        drawdown_threshold = float(_config_get(self.config, "risk", "max_drawdown_warning", default=0.15))
        correlation_threshold = float(_config_get(self.config, "risk", "correlation_warning", default=0.8))

        alerts: list[dict[str, Any]] = []

        # Only check major indices and held ETFs for anomaly volatility,
        # not the entire 1400+ ETF universe (which produces excessive noise).
        # Read portfolio codes directly since portfolio_status is added after analysis
        holdings_codes = _load_portfolio_codes()

        major_index_codes = {
            "sh000001", "sh000300", "sh000016", "sh000688",
            "sz399001", "sz399006", "^GSPC", "^IXIC", "^HSI", "^N225",
        }

        assets_to_check: list[dict[str, Any]] = []
        for idx in indices:
            code = str(idx.get("code") or idx.get("symbol") or "")
            if code in major_index_codes:
                assets_to_check.append(idx)
        for etf in etfs:
            code = str(etf.get("code") or "")
            if code in holdings_codes:
                assets_to_check.append(etf)

        for asset in assets_to_check:
            change_pct = _first_number(asset, ("change_pct",))
            if change_pct is None or not detect_anomaly_volatility(change_pct, anomaly_threshold):
                continue
            code = str(asset.get("code") or asset.get("symbol") or asset.get("name") or "未知资产")
            name = str(asset.get("name") or code)
            # change_pct is a ratio (e.g. 0.031 for 3.1%), format as percentage
            pct_display = change_pct * 100
            alerts.append({
                "level": "warning",
                "alert_type": "异常波动",
                "message": f"{name} 单日涨跌幅 {pct_display:+.2f}% 超过阈值({anomaly_threshold*100:.0f}%)",
                "affected_assets": [code],
            })

        price_series = _price_series_from_snapshot(snapshot, indices, db=self.db)
        max_drawdown = calc_max_drawdown([float(item) for item in price_series.tolist()]) if price_series is not None and len(price_series) > 0 else None
        if max_drawdown is not None and check_drawdown_warning(max_drawdown, drawdown_threshold):
            alerts.append({
                "level": "warning",
                "alert_type": "最大回撤",
                "message": f"主要指数最大回撤 {max_drawdown:.2%} 超过预警阈值",
                "affected_assets": [],
            })

        etf_returns = _returns_from_snapshot(snapshot, etfs)
        correlation_result = calc_correlation_matrix(etf_returns) if etf_returns else {"average_correlation": None}
        avg_correlation = correlation_result.get("average_correlation")
        if avg_correlation is not None and detect_correlation_breakdown(avg_correlation, correlation_threshold):
            alerts.append({
                "level": "warning",
                "alert_type": "相关性过高",
                "message": f"ETF平均相关性 {avg_correlation:.2f}，分散化效果下降",
                "affected_assets": list(etf_returns.keys()),
            })

        return alerts, {
            "max_drawdown": max_drawdown,
            "average_correlation": avg_correlation,
            "anomaly_threshold": anomaly_threshold,
            "drawdown_warning_threshold": drawdown_threshold,
            "correlation_warning_threshold": correlation_threshold,
        }

    @staticmethod
    @staticmethod
    def _build_overview(
        indices: list[dict[str, Any]],
        sectors: list[dict[str, Any]],
        fund_flows: Mapping[str, Any],
        macro: Mapping[str, Any],
        news: list[str],
        trend_result: Mapping[str, Any],
        valuation_result: Mapping[str, Any],
        risk_alerts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        ma_alignment = trend_result.get("ma_alignment")
        standing_ratio = trend_result.get("standing_line_ratio")
        valuation_level = valuation_result.get("overall_level")

        if risk_alerts:
            direction = "防守"
        elif ma_alignment == "多头排列" and (standing_ratio is None or standing_ratio >= 0.5):
            direction = "进攻"
        elif ma_alignment == "空头排列" or valuation_level in ("偏贵", "贵"):
            direction = "防守"
        else:
            direction = "观望"

        parts: list[str] = []
        if ma_alignment:
            parts.append(f"趋势{ma_alignment}")
        if valuation_level:
            parts.append(f"估值{valuation_level}")
        if risk_alerts:
            parts.append(f"触发{len(risk_alerts)}项风险提示")
        summary = "，".join(parts) if parts else "数据不足，维持观望。"

        index_snapshot = _build_index_snapshot(indices)
        market_breadth = _build_market_breadth(sectors)
        fund_flow_direction = _build_fund_flow_direction(fund_flows)
        key_events = news[:3] if news else []
        global_context = _build_global_context(indices, macro)

        return {
            "summary": summary,
            "direction": direction,
            "key_events": key_events,
            "index_snapshot": index_snapshot,
            "market_breadth": market_breadth,
            "fund_flow_direction": fund_flow_direction,
            "global_context": global_context,
        }

    @staticmethod
    def _build_sector_opportunities(momentum_ranking: Any) -> list[dict[str, Any]]:
        if not momentum_ranking:
            return []
        opportunities: list[dict[str, Any]] = []
        for sector in momentum_ranking[:3]:
            name = str(sector.get("name") or "未知板块")
            rank = int(sector.get("rank") or 0)
            score = float(sector.get("composite_score") or 0.0)
            opportunities.append({
                "sector_name": name,
                "etf_code": str(sector.get("etf_code") or ""),
                "etf_name": str(sector.get("etf_name") or ""),
                "reason": f"动量综合得分 {score:.2f}，排名第 {rank}",
                "momentum_rank": rank,
            })
        return opportunities

    @staticmethod
    def _analyze_precious_metals(pm: dict[str, Any]) -> dict[str, Any]:
        """Build gold/precious metals summary from collected data."""
        result: dict[str, Any] = {}

        gold_spot = pm.get("gold_spot") or {}
        result["gold_spot_price"] = gold_spot.get("price")
        result["gold_spot_change_5d"] = gold_spot.get("change_5d")

        comex = pm.get("comex_gold") or {}
        result["comex_gold_price"] = comex.get("price")
        result["comex_gold_change_pct"] = comex.get("change_pct")
        result["comex_gold_name"] = comex.get("name")
        comex_silver = pm.get("comex_silver") or {}
        result["comex_silver_price"] = comex_silver.get("price")
        result["comex_silver_change_pct"] = comex_silver.get("change_pct")

        concept = pm.get("gold_concept") or {}
        latest_concept = concept.get("latest") or {}
        concept_records = concept.get("records") or []
        concept_change_pct = None
        if concept_records and len(concept_records) >= 2:
            try:
                prev_close = float(concept_records[-2].get("close") or 0)
                curr_close = float(concept_records[-1].get("close") or 0)
                if prev_close != 0:
                    concept_change_pct = round((curr_close - prev_close) / prev_close * 100, 2)
            except (TypeError, ValueError):
                pass
        result["gold_concept_change_pct"] = concept_change_pct

        gold_etfs = pm.get("gold_etfs") or []
        result["gold_etfs"] = gold_etfs

        # Determine gold direction signal
        spot_5d = result.get("gold_spot_change_5d")
        comex_pct = result.get("comex_gold_change_pct")
        concept_pct = result.get("gold_concept_change_pct")

        signals = []
        if spot_5d is not None:
            signals.append("spot" if spot_5d >= 0 else "spot")
        if comex_pct is not None:
            signals.append(comex_pct >= 0)
        if concept_pct is not None:
            signals.append(concept_pct >= 0)

        if signals:
            up_count = sum(1 for s in signals if s)
            if up_count == len(signals):
                direction = "gold_up"
            elif up_count == 0:
                direction = "gold_down"
            else:
                direction = "gold_mixed"
        else:
            direction = None

        result["direction"] = direction
        return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = {key: dict(value) if isinstance(value, dict) else value for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _config_get(config: Mapping[str, Any], section: str, key: str, *, default: Any) -> Any:
    section_value = config.get(section, {})
    if isinstance(section_value, Mapping):
        return section_value.get(key, default)
    return default


def _record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, type) and is_dataclass(value):
        return dict(asdict(cast(Any, value)))
    return {}


def _records(value: Any, *, key_field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        records: list[dict[str, Any]] = []
        for key, item in value.items():
            record = _record(item)
            if key_field not in record:
                record[key_field] = str(key)
            records.append(record)
        return records
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_record(item) for item in value]
    return []


def _as_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _first_number(source: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        number = _as_number(source.get(key))
        if number is not None:
            return number
    return None


def _number_list(value: Any) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[float] = []
    for item in value:
        if isinstance(item, Mapping):
            number = _first_number(item, ("pe_ratio", "pe", "pe_current", "value"))
        else:
            number = _as_number(item)
        if number is not None:
            result.append(number)
    return result


def _primary_index(indices: list[dict[str, Any]]) -> dict[str, Any]:
    preferred_codes = ("sh000300", "000300", "399300", "沪深300")
    for index in indices:
        code = str(index.get("code") or "")
        name = str(index.get("name") or "")
        if any(token in code or token in name for token in preferred_codes):
            return index
    return indices[0] if indices else {}


def _price_series_from_snapshot(
    snapshot: Mapping[str, Any],
    indices: list[dict[str, Any]],
    db: Any | None = None,
) -> pd.Series | None:
    candidates = [
        snapshot.get("price_series"),
        snapshot.get("index_prices"),
        snapshot.get("historical_prices"),
    ]
    primary_index = _primary_index(indices)
    candidates.extend([
        primary_index.get("price_history"),
        primary_index.get("prices"),
        primary_index.get("history"),
    ])

    for candidate in candidates:
        series = _series_from_history(candidate, value_keys=("price", "close", "value"))
        if series is not None and not series.empty:
            return series

    if db is not None and primary_index:
        code = str(primary_index.get("code") or primary_index.get("symbol") or "")
        get_history = getattr(db, "get_historical_index", None)
        if code and callable(get_history):
            series = _series_from_history(get_history(code, days=252), value_keys=("price", "close", "value"))
            if series is not None and not series.empty:
                return series
    return None


def _has_price_history(record: Mapping[str, Any]) -> bool:
    for key in ("price_history", "prices", "history"):
        series = _series_from_history(record.get(key), value_keys=("price", "close", "value"))
        if series is not None and not series.empty:
            return True
    return False


def _returns_from_price_history(candidate: Any) -> list[float]:
    series = _series_from_history(candidate, value_keys=("price", "close", "value"))
    if series is None or len(series.dropna()) < 2:
        return []
    returns = series.astype("float64").pct_change().dropna()
    return [float(item) for item in returns.tolist() if np.isfinite(item)]


def _volume_series_from_snapshot(
    snapshot: Mapping[str, Any],
    indices: list[dict[str, Any]],
    db: Any | None = None,
) -> list[float] | None:
    """Extract volume history from snapshot, indices, or DB.
    
    Returns a list of volume values in chronological order, or None.
    """
    # Try volume data from primary index
    primary_index = _primary_index(indices)
    if primary_index:
        for key in ("volume_history", "volumes"):
            series = _series_from_history(primary_index.get(key), value_keys=("volume", "vol"))
            if series is not None and not series.empty:
                return series.tolist()

    # Try DB
    if db is not None and primary_index:
        code = str(primary_index.get("code") or primary_index.get("symbol") or "")
        get_history = getattr(db, "get_historical_index", None)
        if code and callable(get_history):
            series = _series_from_history(
                get_history(code, days=252), value_keys=("volume", "vol")
            )
            if series is not None and not series.empty:
                return series.tolist()

    return None


def _series_from_history(candidate: Any, *, value_keys: Sequence[str]) -> pd.Series | None:
    if candidate is None:
        return None
    if isinstance(candidate, pd.Series):
        return _series_from_values(candidate.tolist())
    if isinstance(candidate, pd.DataFrame):
        for key in value_keys:
            if key in candidate.columns:
                frame = candidate.copy()
                if "date" in frame.columns:
                    frame = frame.sort_values("date")
                return _series_from_values(frame[key].tolist())
        return None
    if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
        values: list[float] = []
        sortable_records: list[Mapping[str, Any]] = []
        for item in candidate:
            if isinstance(item, Mapping):
                sortable_records.append(item)
            else:
                number = _as_number(item)
                if number is not None:
                    values.append(number)
        if sortable_records:
            records = sorted(sortable_records, key=lambda item: str(item.get("date") or ""))
            for item in records:
                number = _first_number(item, value_keys)
                if number is not None:
                    values.append(number)
        return pd.Series(values, dtype="float64") if values else None
    return None


def _series_from_values(values: Sequence[Any]) -> pd.Series:
    numeric_values = [value for value in (_as_number(item) for item in values) if value is not None]
    return pd.Series(numeric_values, dtype="float64")


def _advance_decline_ratio(macro: Mapping[str, Any], etfs: list[dict[str, Any]]) -> float | None:
    ratio = _first_number(macro, ("advance_decline_ratio", "adv_dec_ratio", "ad_ratio"))
    if ratio is not None:
        return ratio

    advances = 0
    declines = 0
    for etf in etfs:
        change_pct = _first_number(etf, ("change_pct",))
        if change_pct is None:
            continue
        if change_pct > 0:
            advances += 1
        elif change_pct < 0:
            declines += 1

    if advances == 0 and declines == 0:
        return None
    return advances / max(declines, 1)


_KEY_INDEX_CODES = {
    # A股核心指数
    "sh000300", "000300",  # 沪深300
    "sh000001",            # 上证指数
    "sz399001", "399001",  # 深证成指
    "sz399006", "399006",  # 创业板指
    # 全球主要指数
    "^GSPC",               # 标普500
    "^IXIC",               # 纳斯达克
    "^HSI",                # 恒生指数
    "^N225",               # 日经225
    "^STOXX50E",           # 欧洲斯托克50
}


def _news_headlines(snapshot: Mapping[str, Any]) -> list[str]:
    raw = snapshot.get("news_headlines")
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    return []


def _build_index_snapshot(indices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not indices:
        return []

    key_map: dict[str, dict[str, Any]] = {}
    for idx in indices:
        code = str(idx.get("code") or "")
        if code in _KEY_INDEX_CODES:
            key_map[code] = idx

    result: list[dict[str, Any]] = []
    for code in _KEY_INDEX_CODES:
        if code in key_map:
            result.append(_compact_index(key_map[code]))

    if not result:
        for idx in indices[:3]:
            result.append(_compact_index(idx))
    return result


def _compact_index(idx: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": idx.get("code"),
        "name": idx.get("name"),
        "price": idx.get("price"),
        "change_pct": idx.get("change_pct"),
        "pe_ratio": idx.get("pe_ratio"),
    }


def _build_market_breadth(sectors: list[dict[str, Any]]) -> dict[str, Any]:
    if not sectors:
        return {}
    up = 0
    down = 0
    for s in sectors:
        change = _as_number(s.get("change_pct"))
        if change is None:
            continue
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
    total = len(sectors)
    up_pct = round(up / total * 100, 1) if total > 0 else 0
    return {"total": total, "up": up, "down": down, "up_pct": up_pct}


def _build_fund_flow_direction(fund_flows: Mapping[str, Any]) -> str | None:
    nb = _as_number(fund_flows.get("north_bound"))
    if nb is None:
        return None
    if nb > 0:
        return "north_inflow"
    if nb < 0:
        return "north_outflow"
    return "north_flat"


_GLOBAL_INDEX_CODES = {"^GSPC", "^IXIC", "^HSI", "^N225", "^STOXX50E"}


def _build_global_context(
    indices: list[dict[str, Any]],
    macro: Mapping[str, Any],
) -> dict[str, Any]:
    vix_value = _as_number(macro.get("vix"))
    usdcny_value = _as_number(macro.get("usdcny"))
    us10y_value = _as_number(macro.get("us10y"))

    global_snapshot: dict[str, dict[str, Any]] = {}
    for idx in indices:
        code = str(idx.get("code") or "")
        if code in _GLOBAL_INDEX_CODES:
            global_snapshot[code] = _compact_index(idx)

    context: dict[str, Any] = {"global_indices": global_snapshot}
    if vix_value is not None:
        vix_level = "恐慌" if vix_value > 25 else ("警惕" if vix_value > 20 else "平稳")
        context["vix"] = {"value": round(vix_value, 2), "level": vix_level}
    if usdcny_value is not None:
        context["usdcny"] = round(usdcny_value, 4)
    if us10y_value is not None:
        context["us10y"] = round(us10y_value, 2)

    return context


def _returns_from_snapshot(snapshot: Mapping[str, Any], etfs: list[dict[str, Any]]) -> dict[str, list[float]]:
    raw_returns = snapshot.get("etf_returns") or snapshot.get("returns")
    if isinstance(raw_returns, Mapping):
        return {str(code): _number_list(values) for code, values in raw_returns.items() if _number_list(values)}

    result: dict[str, list[float]] = {}
    for etf in etfs:
        code = str(etf.get("code") or etf.get("symbol") or "")
        if not code:
            continue
        returns = _number_list(etf.get("returns"))
        if returns:
            result[code] = returns
    return result


def _position_advice(ma_alignment: str | None, standing_ratio: float | None, standing_threshold: float) -> str:
    if ma_alignment == "多头排列" and (standing_ratio is None or standing_ratio >= standing_threshold):
        return "市场趋势偏强，可维持或逐步提高权益仓位"
    if ma_alignment == "空头排列" or (standing_ratio is not None and standing_ratio < standing_threshold):
        return "市场趋势偏弱，建议控制仓位并关注风险"
    return "趋势信号不明确，建议维持均衡配置"


def _trend_confidence(
    ma_alignment: str | None,
    standing_ratio: float | None,
    sentiment: Mapping[str, Any] | None,
) -> float:
    score = 0.0
    if ma_alignment is not None:
        score += 0.4
    if standing_ratio is not None:
        score += 0.3
    if sentiment is not None:
        score += 0.3
    return round(score, 2)


def _load_portfolio_codes() -> set[str]:
    """Read portfolio.yaml and return the set of held ETF codes."""
    from pathlib import Path
    import yaml

    path = Path("portfolio.yaml")
    if not path.exists():
        return set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        holdings = raw.get("holdings", []) if raw else []
        return {str(h["code"]) for h in holdings if isinstance(h, dict) and "code" in h}
    except Exception:
        return set()
