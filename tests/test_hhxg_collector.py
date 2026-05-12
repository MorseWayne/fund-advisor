"""Tests for the hhxg.top sentiment collector."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.data.collectors.hhxg_collector import HhxgCollector


def _make_collector(monkeypatch, *, payload: Any = None, status_code: int = 200):
    """Patch httpx.AsyncClient so the collector talks to a mock transport.

    A non-dict payload triggers ``ValueError`` -> retry.  A None payload simulates
    an HTTP error response with the given status_code (no JSON body).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if payload is None:
            return httpx.Response(status_code)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(
        "src.data.collectors.hhxg_collector.httpx.AsyncClient", PatchedAsyncClient
    )
    return HhxgCollector(timeout_seconds=1.0)


def _sample_snapshot() -> dict[str, Any]:
    return {
        "meta": {"schema_version": 3, "generated_at": "2026-05-12T20:00:00"},
        "date": "2026-05-12",
        "ai_summary": {
            "market_state": "震荡上行,赚钱效应回升",
            "focus_direction": "科技 + 周期",
            "theme_focus": "AI + 储能",
            "hotmoney_state": "活跃",
            "cta": "了解更多",
        },
        "market": {
            "date": "2026-05-12",
            "sentiment_index": 65.5,
            "sentiment_label": "强",
            "limit_up": 85,
            "fried": 12,
            "limit_down": 3,
            "struct_diff": 70,
            "promotion_rate": "65%",
            "total": 5300,
        },
        "comparison": {
            "yesterday": {"limit_up": 60, "sentiment_index": 55.0, "fried": 18},
            "trend_label": "近7日高位区间",
        },
        "ladder": {
            "total_limit_up": 85,
            "max_streak": 7,
            "top_streak": {"name": "某连板股", "code": "300000", "industry": "电子"},
        },
        "ladder_detail": {
            "levels": [
                {
                    "boards": 2,
                    "count": 30,
                    "stocks": [{"name": "A", "code": "1", "industry": "AI"}],
                },
                {"boards": 3, "count": 10, "stocks": []},
            ],
            "lb_rates_map": {"2": "60.0%", "3": "40.0%"},
        },
        "hot_themes": [
            {
                "name": "AI算力",
                "limitup_count": 12,
                "net_yi": 8.5,
                "top_stocks": [
                    {"name": "龙头A", "net_yi": 3.1},
                    {"name": "龙头B", "net_yi": 1.2},
                ],
            },
            {"name": "储能", "limitup_count": 5, "net_yi": 2.0, "top_stocks": []},
        ],
        "focus_news": [
            {"t": "2026-05-12T14:30:00", "cat": "政策", "title": "新政发布"},
        ],
        "macro_news": [
            {"t": "2026-05-12T09:00:00", "cat": "宏观", "title": "PMI 数据公布"},
            {"t": "2026-05-12T14:30:00", "cat": "政策", "title": "新政发布"},  # dup
        ],
    }


@pytest.mark.asyncio
async def test_sentiment_parses_index_label_and_yesterday(monkeypatch):
    collector = _make_collector(monkeypatch, payload=_sample_snapshot())
    sentiment = await collector.fetch_sentiment()

    assert sentiment["sentiment_index"] == 65.5
    assert sentiment["sentiment_label"] == "强"
    assert sentiment["limit_up"] == 85
    assert sentiment["fried"] == 12
    assert sentiment["limit_down"] == 3
    assert sentiment["yesterday"] == {
        "sentiment_index": 55.0,
        "limit_up": 60,
        "fried": 18,
    }
    assert sentiment["trend_label"] == "近7日高位区间"
    assert sentiment["ai_summary"]["market_state"].startswith("震荡上行")
    assert "cta" not in sentiment["ai_summary"]


@pytest.mark.asyncio
async def test_ladder_parses_levels_and_rates(monkeypatch):
    collector = _make_collector(monkeypatch, payload=_sample_snapshot())
    ladder = await collector.fetch_ladder()

    assert ladder["total_limit_up"] == 85
    assert ladder["max_streak"] == 7
    assert ladder["top_streak"]["name"] == "某连板股"
    assert len(ladder["levels"]) == 2
    assert ladder["levels"][0]["boards"] == 2
    assert ladder["levels"][0]["count"] == 30
    assert ladder["lb_rates_map"] == {"2": "60.0%", "3": "40.0%"}


@pytest.mark.asyncio
async def test_hot_themes_returns_ordered_list(monkeypatch):
    collector = _make_collector(monkeypatch, payload=_sample_snapshot())
    themes = await collector.fetch_hot_themes()

    assert [t["name"] for t in themes] == ["AI算力", "储能"]
    assert themes[0]["limitup_count"] == 12
    assert themes[0]["net_yi"] == 8.5
    assert themes[0]["top_stocks"][0]["name"] == "龙头A"
    # No top_stocks key when input list is empty
    assert "top_stocks" not in themes[1]


@pytest.mark.asyncio
async def test_focus_news_dedups_focus_and_macro(monkeypatch):
    collector = _make_collector(monkeypatch, payload=_sample_snapshot())
    news = await collector.fetch_focus_news()

    assert len(news) == 2  # dup with same (t, title) collapsed
    titles = [n["title"] for n in news]
    assert "PMI 数据公布" in titles
    assert "新政发布" in titles


@pytest.mark.asyncio
async def test_snapshot_fetched_once_for_multiple_calls(monkeypatch):
    """Four fetch_* methods share a single HTTP request."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample_snapshot())

    transport = httpx.MockTransport(handler)

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(
        "src.data.collectors.hhxg_collector.httpx.AsyncClient", PatchedAsyncClient
    )
    collector = HhxgCollector(timeout_seconds=1.0)

    await collector.fetch_sentiment()
    await collector.fetch_ladder()
    await collector.fetch_hot_themes()
    await collector.fetch_focus_news()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_404_returns_empty_results(monkeypatch):
    collector = _make_collector(monkeypatch, status_code=404)
    assert await collector.fetch_sentiment() == {}
    assert await collector.fetch_ladder() == {}
    assert await collector.fetch_hot_themes() == []
    assert await collector.fetch_focus_news() == []


@pytest.mark.asyncio
async def test_malformed_payload_returns_empty(monkeypatch):
    collector = _make_collector(monkeypatch, payload=["not", "a", "dict"])
    assert await collector.fetch_sentiment() == {}
    assert await collector.fetch_ladder() == {}
    assert await collector.fetch_hot_themes() == []
    assert await collector.fetch_focus_news() == []


@pytest.mark.asyncio
async def test_schema_version_warning_does_not_fail(monkeypatch, caplog):
    payload = _sample_snapshot()
    payload["meta"]["schema_version"] = 99
    collector = _make_collector(monkeypatch, payload=payload)
    # Newer schema versions still attempt to parse known fields.
    sentiment = await collector.fetch_sentiment()
    assert sentiment["sentiment_index"] == 65.5


@pytest.mark.asyncio
async def test_partial_payload_only_returns_present_sections(monkeypatch):
    payload = {
        "meta": {"schema_version": 3},
        "date": "2026-05-12",
        "market": {"sentiment_index": 50, "sentiment_label": "中"},
    }
    collector = _make_collector(monkeypatch, payload=payload)
    assert (await collector.fetch_sentiment())["sentiment_index"] == 50.0
    assert await collector.fetch_ladder() == {}
    assert await collector.fetch_hot_themes() == []
    assert await collector.fetch_focus_news() == []
