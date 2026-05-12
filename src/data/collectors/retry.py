"""Async retry helpers for data collectors."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar, cast

from loguru import logger

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {429, 503}
_RATE_LIMIT_STATUS_CODES = {429}
_RATE_LIMIT_TOKENS = ("429", "too many requests", "rate limit", "ratelimit")
_TRANSIENT_TOKENS = ("503", "service unavailable", "502", "504", "bad gateway", "gateway timeout")


async def retry_with_backoff(
    operation: Callable[[], T | Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    rate_limit_base_delay: float = 10.0,
    rate_limit_max_retries: int = 5,
    operation_name: str = "collector_call",
) -> T:
    """Run an operation with retryable HTTP backoff and jitter.

    Two retry regimes are used:

    - ``429 / rate limit`` → slow path: up to ``rate_limit_max_retries`` retries
      with exponential backoff starting at ``rate_limit_base_delay`` seconds.
    - ``503 / transient network`` → fast path: up to ``max_retries`` retries
      starting at ``base_delay`` seconds (1s, 2s, 4s by default).

    Jitter of up to 10% is added to each delay. ``Retry-After`` headers take
    precedence over both regimes when present.
    """

    last_error: Exception | None = None
    rate_limit_attempts = 0
    transient_attempts = 0
    while True:
        try:
            result = operation()
            if inspect.isawaitable(result):
                return await cast(Awaitable[T], result)
            return cast(T, result)
        except Exception as exc:
            last_error = exc
            kind = _classify_error(exc)
            if kind is None:
                raise

            if kind == "rate_limit":
                rate_limit_attempts += 1
                if rate_limit_attempts > rate_limit_max_retries:
                    raise
                delay = _retry_delay(
                    exc,
                    rate_limit_attempts,
                    base_delay=rate_limit_base_delay,
                    max_delay=max_delay,
                )
                attempt_label = f"{rate_limit_attempts}/{rate_limit_max_retries}"
            else:
                transient_attempts += 1
                if transient_attempts > max_retries:
                    raise
                delay = _retry_delay(
                    exc,
                    transient_attempts,
                    base_delay=base_delay,
                    max_delay=max_delay,
                )
                attempt_label = f"{transient_attempts}/{max_retries}"

            jitter = random.uniform(0, delay * 0.1)
            total_delay = min(delay + jitter, max_delay)
            logger.warning(
                "Retryable collector call failed operation={} kind={} attempt={} delay={:.2f}s error={}",
                operation_name,
                kind,
                attempt_label,
                total_delay,
                exc,
            )
            await asyncio.sleep(total_delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{operation_name} failed without an exception")


def _classify_error(exc: Exception) -> str | None:
    """Return ``"rate_limit"``, ``"transient"``, or ``None`` (non-retryable)."""
    status_code = _extract_status_code(exc)
    if status_code in _RATE_LIMIT_STATUS_CODES:
        return "rate_limit"
    if status_code in _RETRYABLE_STATUS_CODES:
        return "transient"

    message = str(exc).lower()
    if any(token in message for token in _RATE_LIMIT_TOKENS):
        return "rate_limit"
    if any(token in message for token in _TRANSIENT_TOKENS):
        return "transient"
    return None


def _is_retryable_error(exc: Exception) -> bool:
    return _classify_error(exc) is not None


def _retry_delay(exc: Exception, retry_number: int, *, base_delay: float, max_delay: float) -> float:
    retry_after = _extract_retry_after(exc)
    if retry_after is not None:
        return min(max(retry_after, 0.0), max_delay)
    return min(base_delay * (2.0 ** (retry_number - 1)), max_delay)


def _extract_status_code(exc: Exception) -> int | None:
    response = _get_attr(exc, "response")
    for source in (response, exc):
        if source is None:
            continue
        status_code = _get_attr(source, "status_code")
        if isinstance(status_code, int):
            return status_code
        code = _get_attr(source, "code")
        if isinstance(code, int):
            return code
    return None


def _extract_retry_after(exc: Exception) -> float | None:
    response = _get_attr(exc, "response")
    if response is None:
        return None

    headers = _get_attr(response, "headers")
    if not isinstance(headers, Mapping):
        return None

    header_map = cast(Mapping[str, object], headers)
    retry_after = header_map.get("Retry-After")
    if retry_after is None:
        return None

    retry_after_text = str(retry_after).strip()
    if not retry_after_text:
        return None
    try:
        return max(float(retry_after_text), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(retry_after_text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
        except (TypeError, ValueError, IndexError, OverflowError):
            logger.warning("Invalid Retry-After header: {}", retry_after_text)
            return None


def _get_attr(obj: object, name: str) -> object | None:
    try:
        return cast(object, object.__getattribute__(obj, name))
    except AttributeError:
        return None


__all__ = ["retry_with_backoff"]