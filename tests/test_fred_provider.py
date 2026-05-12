"""Tests for the FRED API provider."""

from __future__ import annotations

import httpx
import pytest

from src.data.collectors.providers.fred import FREDProvider


def make_provider(monkeypatch, observations_by_series, api_key="testkey"):
    monkeypatch.setenv("FRED_API_KEY", api_key)

    def handler(request: httpx.Request) -> httpx.Response:
        series_id = request.url.params.get("series_id")
        obs = observations_by_series.get(series_id, [])
        return httpx.Response(200, json={"observations": obs})

    transport = httpx.MockTransport(handler)

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("src.data.collectors.providers.fred.httpx.AsyncClient", PatchedAsyncClient)
    return FREDProvider()


@pytest.mark.asyncio
async def test_fred_treasury_uses_yield_pct(monkeypatch):
    provider = make_provider(monkeypatch, {
        "DGS10": [
            {"date": "2026-05-10", "value": "4.25"},
            {"date": "2026-05-09", "value": "4.20"},
        ]
    })
    result = await provider.fetch(["^TNX"])

    q = result["^TNX"]
    assert q.yield_pct == 4.25
    assert q.price is None
    assert q.previous_close == 4.20
    assert q.trade_date == "2026-05-10"
    assert q.source == "fred"


@pytest.mark.asyncio
async def test_fred_vix_uses_price_and_change_pct(monkeypatch):
    provider = make_provider(monkeypatch, {
        "VIXCLS": [
            {"date": "2026-05-10", "value": "20.00"},
            {"date": "2026-05-09", "value": "18.00"},
        ]
    })
    result = await provider.fetch(["^VIX"])

    q = result["^VIX"]
    assert q.price == 20.0
    assert q.yield_pct is None
    assert q.change_pct == pytest.approx((20 - 18) / 18 * 100)


@pytest.mark.asyncio
async def test_fred_skips_missing_observations(monkeypatch):
    provider = make_provider(monkeypatch, {"DGS10": [{"date": "2026-05-10", "value": "."}]})
    result = await provider.fetch(["^TNX"])
    assert result == {}


@pytest.mark.asyncio
async def test_fred_no_api_key_means_no_support(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    provider = FREDProvider()
    assert not provider.supports("^TNX")
    assert await provider.fetch(["^TNX"]) == {}
