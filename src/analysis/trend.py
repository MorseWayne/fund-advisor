"""Trend tracking indicators for the analysis engine.

All functions in this module are pure computations: they accept already-processed
market data and return primitive Python values/dicts suitable for serialization.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when unavailable."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def calc_ma_alignment(prices: pd.Series, periods: list[int] = [5, 20, 60]) -> str:
    """Classify the latest moving-average alignment.

    Returns:
        ``"多头排列"`` when short MA > medium MA > long MA,
        ``"空头排列"`` when short MA < medium MA < long MA,
        otherwise ``"交叉震荡"``.
    """

    if len(prices) == 0 or len(periods) < 3:
        return "交叉震荡"

    numeric_values = [value for value in (_as_float(item) for item in prices.tolist()) if value is not None]
    numeric_prices = pd.Series(numeric_values, dtype="float64")
    if numeric_prices.empty:
        return "交叉震荡"

    ma_values: list[float] = []
    for period in periods[:3]:
        window = int(period)
        if window <= 0 or len(numeric_values) < window:
            return "交叉震荡"
        ma = float(np.mean(numeric_values[-window:]))
        if pd.isna(ma):
            return "交叉震荡"
        ma_values.append(ma)

    short_ma, mid_ma, long_ma = ma_values
    if short_ma > mid_ma > long_ma:
        return "多头排列"
    if short_ma < mid_ma < long_ma:
        return "空头排列"
    return "交叉震荡"


def calc_standing_line_ratio(etf_list: list[dict[str, Any]], period: int = 250) -> float:
    """Estimate the share of ETFs trading above their long moving average.

    The processed snapshot currently supplies latest price/change data rather than
    full histories for every ETF, so ``change_pct > 0`` is used as a conservative
    proxy for being above the ``period``-day moving average.
    """

    del period  # Kept in the public signature for future historical-data support.

    if not etf_list:
        return 0.0

    valid_count = 0
    above_count = 0
    for etf in etf_list:
        change_pct = _as_float(etf.get("change_pct"))
        if change_pct is None:
            continue
        valid_count += 1
        if change_pct > 0:
            above_count += 1

    if valid_count == 0:
        return 0.0
    return above_count / valid_count


def calc_sentiment(vix: float, advance_decline_ratio: float) -> dict[str, float | str]:
    """Combine VIX and advance/decline breadth into a sentiment score.

    VIX drives the base regime (``>25`` panic, ``<15`` greed). Market breadth then
    nudges the 0-100 score up/down without making network calls or external lookups.
    """

    vix_value = _as_float(vix)
    adr_value = _as_float(advance_decline_ratio)

    if vix_value is None:
        base_score = 50.0
    elif vix_value > 25:
        base_score = 25.0
    elif vix_value < 15:
        base_score = 75.0
    else:
        # Linearly map VIX 15..25 to score 75..25.
        base_score = 75.0 - ((vix_value - 15.0) / 10.0) * 50.0

    breadth_adjustment = 0.0
    if adr_value is not None:
        if adr_value > 1.2:
            breadth_adjustment = 10.0
        elif adr_value < 0.8:
            breadth_adjustment = -10.0

    score = float(np.clip(base_score + breadth_adjustment, 0.0, 100.0))

    if vix_value is not None and vix_value > 25 and score < 45:
        level = "恐慌"
    elif vix_value is not None and vix_value < 15 and score > 55:
        level = "贪婪"
    elif score < 40:
        level = "恐慌"
    elif score > 60:
        level = "贪婪"
    else:
        level = "中性"

    return {"level": level, "score": round(score, 2)}


# ---------------------------------------------------------------------------
# Volume-price confirmation and multi-timeframe signals (Task 3)
# ---------------------------------------------------------------------------


def calc_volume_confirmation(
    prices: list[float] | pd.Series,
    volumes: list[float] | pd.Series | None,
    *,
    volume_ma_days: int = 20,
    price_change_days: int = 5,
) -> dict[str, object]:
    """Determine if recent price movement is confirmed by trading volume.

    Price moves without volume support are more likely to reverse.
    Price moves with above-average volume suggest genuine institutional
    participation.

    Returns:
        Dict with keys: ``confirmation`` ("strong_bullish"|"weak_bullish"|
        "strong_bearish"|"weak_bearish"|"neutral"), ``volume_ratio``,
        ``price_change_pct``, ``assessment`` (human-readable).
    """

    prices_series = _to_series(prices)
    if prices_series.empty or len(prices_series) < price_change_days:
        return {
            "confirmation": "neutral",
            "volume_ratio": None,
            "price_change_pct": 0.0,
            "assessment": "数据不足，量价关系无法判断",
        }

    # Price change over recent period
    recent_prices = prices_series.tail(price_change_days)
    if len(recent_prices) < 2:
        return {
            "confirmation": "neutral",
            "volume_ratio": None,
            "price_change_pct": 0.0,
            "assessment": "数据不足",
        }

    price_change_pct = round(
        (float(recent_prices.iloc[-1]) - float(recent_prices.iloc[0]))
        / float(recent_prices.iloc[0]) * 100,
        2,
    ) if float(recent_prices.iloc[0]) != 0 else 0.0

    # Volume analysis
    if volumes is None:
        vols = _to_series([])
    else:
        vols = _to_series(volumes)

    volume_ratio: float | None = None
    if not vols.empty and len(vols) >= volume_ma_days:
        avg_vol = float(np.mean(vols.tail(volume_ma_days)))
        recent_vol = float(np.mean(vols.tail(min(price_change_days, len(vols)))))
        if avg_vol > 0:
            volume_ratio = round(recent_vol / avg_vol, 2)
    elif not vols.empty:
        volume_ratio = 1.0  # Insufficient history, assume neutral

    # Determine confirmation
    abs_change = abs(price_change_pct)
    threshold = 0.5  # Minimum 0.5% price change to classify

    if abs_change < threshold:
        confirmation = "neutral"
        assessment = "价格变化幅度极小，量价信号不显著"
    elif volume_ratio is None:
        confirmation = "weak_bullish" if price_change_pct > 0 else "weak_bearish"
        assessment = "无成交量数据，价格信号置信度降低"
    elif volume_ratio >= 1.2:
        # Above-average volume: genuine move
        if price_change_pct > 0:
            confirmation = "strong_bullish"
            assessment = f"放量上涨（量比{volume_ratio}），资金主动买入，涨势可信度较高"
        else:
            confirmation = "strong_bearish"
            assessment = f"放量下跌（量比{volume_ratio}），资金主动卖出，跌势可信度较高"
    elif volume_ratio <= 0.8:
        # Below-average volume: weak conviction
        if price_change_pct > 0:
            confirmation = "weak_bullish"
            assessment = f"缩量上涨（量比{volume_ratio}），上涨动力不足，警惕假突破"
        else:
            confirmation = "weak_bearish"
            assessment = f"缩量下跌（量比{volume_ratio}），抛压减轻但仍偏弱"
    else:
        confirmation = "weak_bullish" if price_change_pct > 0 else "weak_bearish"
        assessment = "量价配合中性，方向信号需要其他指标辅助判断"

    return {
        "confirmation": confirmation,
        "volume_ratio": volume_ratio,
        "price_change_pct": price_change_pct,
        "assessment": assessment,
    }


def calc_multi_timeframe_signal(
    daily_prices: list[float] | pd.Series,
    *,
    weekly_prices: list[float] | pd.Series | None = None,
    short_ma: int = 5,
    long_ma: int = 20,
) -> dict[str, object]:
    """Check whether daily and weekly trends align.

    When daily and weekly trends point in the same direction, the signal
    is stronger and more actionable. Divergent timeframes suggest
    choppy/range-bound conditions where trend-following is risky.

    Returns:
        Dict with: ``alignment`` ("bullish_aligned"|"bearish_aligned"|
        "divergent"|"unknown"), ``daily_trend``, ``weekly_trend``,
        ``assessment``.
    """

    daily_series = _to_series(daily_prices)

    # Daily trend: is short MA above long MA?
    daily_trend = "unknown"
    daily_ma_status = None
    if len(daily_series) >= long_ma:
        daily_short = float(np.mean(daily_series.tail(short_ma).tolist()))
        daily_long = float(np.mean(daily_series.tail(long_ma).tolist()))
        daily_ma_status = daily_short / daily_long if daily_long != 0 else 1.0
        if daily_ma_status > 1.005:
            daily_trend = "bullish"
        elif daily_ma_status < 0.995:
            daily_trend = "bearish"
        else:
            daily_trend = "neutral"

    # Weekly trend (resample daily to weekly if weekly data not provided)
    weekly_trend = "unknown"
    if weekly_prices is not None:
        weekly_series = _to_series(weekly_prices)
        if len(weekly_series) >= 4:
            weekly_short = float(np.mean(weekly_series.tail(min(2, len(weekly_series))).tolist()))
            weekly_long = float(np.mean(weekly_series.tail(min(4, len(weekly_series))).tolist()))
            weekly_ratio = weekly_short / weekly_long if weekly_long != 0 else 1.0
            if weekly_ratio > 1.005:
                weekly_trend = "bullish"
            elif weekly_ratio < 0.995:
                weekly_trend = "bearish"
            else:
                weekly_trend = "neutral"
    elif len(daily_series) >= long_ma * 5 + 5:
        # Approximate weekly from daily: take last 5 weeks of 5-day closes
        daily_prices_list = daily_series.tolist()
        weekly_approx: list[float] = []
        for i in range(len(daily_prices_list) - 5, -1, -5):
            if i >= 0:
                weekly_approx.append(daily_prices_list[i])
            if len(weekly_approx) >= 4:
                break
        weekly_approx.reverse()
        if len(weekly_approx) >= 4:
            weekly_short = float(np.mean(weekly_approx[-2:]))
            weekly_long = float(np.mean(weekly_approx[-4:]))
            weekly_ratio = weekly_short / weekly_long if weekly_long != 0 else 1.0
            if weekly_ratio > 1.005:
                weekly_trend = "bullish"
            elif weekly_ratio < 0.995:
                weekly_trend = "bearish"
            else:
                weekly_trend = "neutral"

    # Alignment assessment
    if daily_trend == "bullish" and weekly_trend == "bullish":
        alignment = "bullish_aligned"
        assessment = "日线和周线趋势一致向上，信号置信度高，适合积极仓位"
    elif daily_trend == "bearish" and weekly_trend == "bearish":
        alignment = "bearish_aligned"
        assessment = "日线和周线趋势一致向下，应以防御为主，减少新增仓位"
    elif daily_trend == "bullish" and weekly_trend not in ("bullish", "unknown"):
        alignment = "divergent"
        assessment = "日线偏多但周线不共振，可能仅为短期反弹，不宜追高"
    elif daily_trend == "bearish" and weekly_trend not in ("bearish", "unknown"):
        alignment = "divergent"
        assessment = "日线偏空但周线不共振，可能是短期回调而非趋势反转"
    elif daily_trend == "unknown":
        alignment = "unknown"
        assessment = "数据不足，无法判断多周期趋势一致性"
    else:
        alignment = "divergent"
        assessment = "多周期信号不一致，建议维持观望或降低仓位"

    return {
        "alignment": alignment,
        "daily_trend": daily_trend,
        "weekly_trend": weekly_trend,
        "assessment": assessment,
    }


def _to_series(data: list[float] | pd.Series) -> pd.Series:
    """Convert list or Series to a clean float Series."""
    if isinstance(data, pd.Series):
        return pd.Series([float(v) for v in data.tolist() if _as_float(v) is not None], dtype="float64")
    values = [float(v) for v in (data or []) if _as_float(v) is not None]
    return pd.Series(values, dtype="float64")
