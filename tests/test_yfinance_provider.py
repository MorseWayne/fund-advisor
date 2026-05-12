"""Tests for the rebuilt yfinance provider (batch download)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.collectors.providers import yfinance as yfm
from src.data.collectors.providers.yfinance import YFinanceProvider


def _two_day_frame(close_t0: float, close_t1: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex(["2026-05-09", "2026-05-10"])
    return pd.DataFrame(
        {
            "Open": [close_t0, close_t1],
            "High": [close_t0, close_t1],
            "Low": [close_t0, close_t1],
            "Close": [close_t0, close_t1],
            "Volume": [100, 200],
        },
        index=idx,
    )


@pytest.mark.asyncio
async def test_batch_download_single_symbol(monkeypatch):
    frame = _two_day_frame(5000.0, 5040.0)

    def fake_download(**kwargs):
        return frame

    monkeypatch.setattr(yfm.yf, "download", fake_download)
    p = YFinanceProvider()
    out = await p.fetch(["^GSPC"])
    q = out["^GSPC"]
    assert q.price == 5040.0
    assert q.previous_close == 5000.0
    assert q.change_pct == pytest.approx(0.8)
    assert q.source == "yfinance"


@pytest.mark.asyncio
async def test_batch_download_multi_symbol_multiindex(monkeypatch):
    frame_a = _two_day_frame(100.0, 102.0)
    frame_b = _two_day_frame(20.0, 19.0)
    combined = pd.concat({"^GSPC": frame_a, "^IXIC": frame_b}, axis=1)

    def fake_download(**kwargs):
        return combined

    monkeypatch.setattr(yfm.yf, "download", fake_download)
    p = YFinanceProvider()
    out = await p.fetch(["^GSPC", "^IXIC"])
    assert out["^GSPC"].price == 102.0
    assert out["^IXIC"].change_pct == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_treasury_uses_yield_pct(monkeypatch):
    frame = _two_day_frame(4.20, 4.25)

    def fake_download(**kwargs):
        return frame

    monkeypatch.setattr(yfm.yf, "download", fake_download)
    p = YFinanceProvider()
    out = await p.fetch(["^TNX"])
    q = out["^TNX"]
    assert q.yield_pct == 4.25
    assert q.price is None


@pytest.mark.asyncio
async def test_empty_download_returns_empty(monkeypatch):
    def fake_download(**kwargs):
        return pd.DataFrame()

    monkeypatch.setattr(yfm.yf, "download", fake_download)
    p = YFinanceProvider()
    assert await p.fetch(["^GSPC"]) == {}
