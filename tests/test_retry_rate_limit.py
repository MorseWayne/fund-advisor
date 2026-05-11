"""Tests for differentiated rate-limit retry strategy."""

from __future__ import annotations

import pytest

from src.data.collectors.retry import retry_with_backoff


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class HTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = FakeResponse(status)


@pytest.mark.asyncio
async def test_rate_limit_uses_longer_delay(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("src.data.collectors.retry.asyncio.sleep", fake_sleep)

    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise HTTPError(429)
        return "ok"

    out = await retry_with_backoff(
        op,
        max_retries=3,
        base_delay=1.0,
        rate_limit_base_delay=10.0,
        rate_limit_max_retries=5,
        operation_name="test",
    )
    assert out == "ok"
    # First retry should start at >= 10s (rate-limit base), not 1s
    assert sleeps[0] >= 10.0


@pytest.mark.asyncio
async def test_transient_uses_fast_delay(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("src.data.collectors.retry.asyncio.sleep", fake_sleep)

    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise HTTPError(503)
        return "ok"

    out = await retry_with_backoff(
        op,
        max_retries=3,
        base_delay=1.0,
        rate_limit_base_delay=10.0,
        operation_name="test",
    )
    assert out == "ok"
    # 503 should use the fast base_delay (≈1s), not 10s
    assert sleeps[0] < 5.0


@pytest.mark.asyncio
async def test_non_retryable_error_raised_immediately(monkeypatch):
    async def fake_sleep(delay):
        raise AssertionError("should not sleep")

    monkeypatch.setattr("src.data.collectors.retry.asyncio.sleep", fake_sleep)

    async def op():
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        await retry_with_backoff(op, operation_name="test")


@pytest.mark.asyncio
async def test_rate_limit_exhaustion_raises(monkeypatch):
    async def fake_sleep(delay):
        return None

    monkeypatch.setattr("src.data.collectors.retry.asyncio.sleep", fake_sleep)

    async def op():
        raise HTTPError(429)

    with pytest.raises(HTTPError):
        await retry_with_backoff(
            op,
            rate_limit_base_delay=0.001,
            rate_limit_max_retries=2,
            operation_name="test",
        )
