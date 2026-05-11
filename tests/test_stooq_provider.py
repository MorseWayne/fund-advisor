"""Tests for the Stooq CSV provider."""

from __future__ import annotations

import httpx
import pytest

from src.data.collectors.providers.stooq import StooqProvider


CSV_TWO_DAYS = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-05-09,4980.00,5010.00,4970.00,5000.00,123456\n"
    "2026-05-10,5005.00,5050.00,4995.00,5040.00,234567\n"
)


def make_provider(monkeypatch, response_map):
    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("s")
        body = response_map.get(symbol, "")
        if body == "ERROR":
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("src.data.collectors.providers.stooq.httpx.AsyncClient", PatchedAsyncClient)
    return StooqProvider(timeout_seconds=2.0, max_concurrency=2)


@pytest.mark.asyncio
async def test_stooq_parses_csv_and_computes_change_pct(monkeypatch):
    provider = make_provider(monkeypatch, {"^spx": CSV_TWO_DAYS})
    result = await provider.fetch(["^GSPC"])

    q = result["^GSPC"]
    assert q.symbol == "^GSPC"
    assert q.asset_type == "global_index"
    assert q.price == 5040.0
    assert q.previous_close == 5000.0
    assert q.change_pct == pytest.approx(0.8)
    assert q.trade_date == "2026-05-10"
    assert q.source == "stooq"


@pytest.mark.asyncio
async def test_stooq_skips_unsupported_symbols(monkeypatch):
    provider = make_provider(monkeypatch, {})
    result = await provider.fetch(["^TNX"])
    assert result == {}


@pytest.mark.asyncio
async def test_stooq_empty_csv_returns_no_quote(monkeypatch):
    provider = make_provider(monkeypatch, {"^spx": "No data"})
    result = await provider.fetch(["^GSPC"])
    assert result == {}


def test_stooq_supports_only_mapped_symbols():
    provider = StooqProvider()
    assert provider.supports("SPY")
    assert provider.supports("^GSPC")
    assert provider.supports("USDCNY=X")
    assert not provider.supports("^TNX")
