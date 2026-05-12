"""hhxg.top A-share sentiment collector.

Wraps ``https://hhxg.top/static/data/assistant/skill_snapshot.json`` — a single
JSON file published daily ~20:00 Asia/Shanghai by the 恢恢量化 aggregator. It
gives us the *sentiment dimensions* that AKShare does not surface directly:

- ``sentiment_index`` (0-100) + yesterday comparison — the single most useful
  A-share mood scalar we've ever had
- limit-up / fried / limit-down counts
- ladder (连板天梯) with per-streak promotion rates
- hot themes with top-stock net inflow
- focus / macro news list

Contract (mirrors ``AKShareCollector``):
- Every ``fetch_*`` returns a structurally-complete dict, possibly empty on
  failure. **Never raises** — callers use ``asyncio.gather`` with
  ``return_exceptions=True`` upstream.
- A single ``skill_snapshot.json`` fetch is cached on the instance for the
  lifetime of one ``DataPipeline.run_daily_collection`` call.

This is an auxiliary source — if hhxg.top is down, pipeline proceeds with
empty sentiment/ladder/themes sections and the rest of the report is
unaffected.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from src.data.collectors.retry import retry_with_backoff


DEFAULT_BASE_URL = "https://hhxg.top/static/data"
SNAPSHOT_PATH = "assistant/skill_snapshot.json"
SUPPORTED_SCHEMA_VERSION = 3

_HEADERS = {
    "User-Agent": "fund-advisor/1.0 (+https://github.com)",
    "Accept": "application/json",
}


class HhxgCollector:
    """Fetch A-share sentiment/ladder/themes/news from hhxg.top."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_attempted = False

    async def _get_snapshot(self) -> dict[str, Any]:
        """Fetch and cache skill_snapshot.json for the pipeline run."""
        async with self._snapshot_lock:
            if self._snapshot is not None:
                return self._snapshot
            if self._snapshot_attempted:
                return {}

            self._snapshot_attempted = True
            url = f"{self.base_url}/{SNAPSHOT_PATH}"

            async def _call() -> dict[str, Any]:
                async with httpx.AsyncClient(timeout=self.timeout, headers=_HEADERS) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.json()

            try:
                payload = await retry_with_backoff(
                    _call,
                    operation_name="hhxg.skill_snapshot",
                )
            except Exception as exc:
                logger.warning("hhxg snapshot fetch failed: {}", exc)
                return {}

            if not isinstance(payload, dict):
                logger.warning("hhxg snapshot returned non-object payload: {}", type(payload))
                return {}

            meta = payload.get("meta", {}) or {}
            version = meta.get("schema_version")
            if isinstance(version, int) and version > SUPPORTED_SCHEMA_VERSION:
                logger.warning(
                    "hhxg snapshot schema_version={} exceeds supported v{} — "
                    "fields may be missing or renamed",
                    version,
                    SUPPORTED_SCHEMA_VERSION,
                )

            self._snapshot = payload
            return payload

    async def fetch_sentiment(self) -> dict[str, Any]:
        """Market-wide A-share sentiment: index, limit-up counts, yesterday delta."""
        snap = await self._get_snapshot()
        market = snap.get("market") or {}
        if not isinstance(market, dict) or not market:
            return {}

        comparison = snap.get("comparison") or {}
        yesterday = comparison.get("yesterday") if isinstance(comparison, dict) else None

        out: dict[str, Any] = {
            "date": market.get("date") or snap.get("date"),
            "sentiment_index": _to_float(market.get("sentiment_index")),
            "sentiment_label": market.get("sentiment_label"),
            "limit_up": _to_int(market.get("limit_up")),
            "fried": _to_int(market.get("fried")),
            "limit_down": _to_int(market.get("limit_down")),
            "struct_diff": _to_int(market.get("struct_diff")),
            "promotion_rate": market.get("promotion_rate"),
            "total": _to_int(market.get("total")),
        }

        if isinstance(yesterday, dict) and yesterday:
            out["yesterday"] = {
                "sentiment_index": _to_float(yesterday.get("sentiment_index")),
                "limit_up": _to_int(yesterday.get("limit_up")),
                "fried": _to_int(yesterday.get("fried")),
            }

        if isinstance(comparison, dict):
            trend_label = comparison.get("trend_label")
            if trend_label:
                out["trend_label"] = trend_label

        ai_summary = snap.get("ai_summary")
        if isinstance(ai_summary, dict) and ai_summary:
            out["ai_summary"] = {
                k: v
                for k, v in ai_summary.items()
                if k in {"market_state", "focus_direction", "theme_focus", "hotmoney_state"}
                and isinstance(v, str)
                and v.strip()
            }

        return {k: v for k, v in out.items() if v is not None and v != {} and v != ""}

    async def fetch_ladder(self) -> dict[str, Any]:
        """Limit-up ladder with per-streak promotion rates."""
        snap = await self._get_snapshot()
        overview = snap.get("ladder") or {}
        detail = snap.get("ladder_detail") or {}
        if not isinstance(overview, dict) and not isinstance(detail, dict):
            return {}

        overview = overview if isinstance(overview, dict) else {}
        detail = detail if isinstance(detail, dict) else {}

        out: dict[str, Any] = {
            "total_limit_up": _to_int(overview.get("total_limit_up")),
            "max_streak": _to_int(overview.get("max_streak")),
        }

        top_streak = overview.get("top_streak")
        if isinstance(top_streak, dict) and top_streak:
            out["top_streak"] = {
                "name": top_streak.get("name"),
                "code": top_streak.get("code"),
                "industry": top_streak.get("industry"),
            }

        levels = detail.get("levels")
        if isinstance(levels, list):
            out["levels"] = [
                {
                    "boards": _to_int(lvl.get("boards")),
                    "count": _to_int(lvl.get("count")),
                    "stocks": _clean_stocks(lvl.get("stocks")),
                }
                for lvl in levels
                if isinstance(lvl, dict)
            ]

        rates = detail.get("lb_rates_map")
        if isinstance(rates, dict):
            out["lb_rates_map"] = {str(k): str(v) for k, v in rates.items()}

        return {k: v for k, v in out.items() if v not in (None, {}, [])}

    async def fetch_hot_themes(self) -> list[dict[str, Any]]:
        """Hot themes with limit-up counts, net inflow (亿), and leader stocks."""
        snap = await self._get_snapshot()
        themes = snap.get("hot_themes")
        if not isinstance(themes, list):
            return []

        out: list[dict[str, Any]] = []
        for theme in themes:
            if not isinstance(theme, dict):
                continue
            name = theme.get("name")
            if not name:
                continue
            record: dict[str, Any] = {
                "name": str(name),
                "limitup_count": _to_int(theme.get("limitup_count")),
                "net_yi": _to_float(theme.get("net_yi")),
            }
            top_stocks = theme.get("top_stocks")
            if isinstance(top_stocks, list):
                record["top_stocks"] = [
                    {"name": str(s.get("name", "")), "net_yi": _to_float(s.get("net_yi"))}
                    for s in top_stocks
                    if isinstance(s, dict) and s.get("name")
                ][:5]
            out.append({k: v for k, v in record.items() if v is not None and v != []})
        return out

    async def fetch_focus_news(self) -> list[dict[str, Any]]:
        """Focus + macro news combined. Deduplicated by (time, title)."""
        snap = await self._get_snapshot()
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for bucket in ("focus_news", "macro_news"):
            raw = snap.get(bucket)
            if not isinstance(raw, list):
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title") or "").strip()
                if not title:
                    continue
                timestamp = str(entry.get("t") or "")
                key = (timestamp, title)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "t": timestamp or None,
                        "cat": entry.get("cat"),
                        "title": title,
                        "bucket": bucket,
                    }
                )
        return items


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _clean_stocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not name:
            continue
        out.append(
            {
                "name": str(name),
                "code": s.get("code"),
                "industry": s.get("industry"),
            }
        )
    return out


__all__ = ["HhxgCollector"]
