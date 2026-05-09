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


async def retry_with_backoff(
    operation: Callable[[], T | Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    operation_name: str = "collector_call",
) -> T:
    """Run an operation with retryable HTTP backoff and jitter.

    ``max_retries=3`` means one initial attempt plus three retries. When no
    valid ``Retry-After`` header is present, delays are 1s, 2s, and 4s by
    default, with up to 10% random jitter added.
    """

    last_error: Exception | None = None
    for retry_index in range(max_retries + 1):
        try:
            result = operation()
            if inspect.isawaitable(result):
                return await cast(Awaitable[T], result)
            return cast(T, result)
        except Exception as exc:
            last_error = exc
            if retry_index >= max_retries or not _is_retryable_error(exc):
                raise

            retry_number = retry_index + 1
            delay = _retry_delay(exc, retry_number, base_delay=base_delay, max_delay=max_delay)
            jitter = random.uniform(0, delay * 0.1)
            total_delay = min(delay + jitter, max_delay)
            logger.warning(
                "Retryable collector call failed operation={} attempt={}/{} delay={:.2f}s error={}",
                operation_name,
                retry_number,
                max_retries,
                total_delay,
                exc,
            )
            await asyncio.sleep(total_delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{operation_name} failed without an exception")


def _is_retryable_error(exc: Exception) -> bool:
    status_code = _extract_status_code(exc)
    if status_code in _RETRYABLE_STATUS_CODES:
        return True

    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "429",
            "too many requests",
            "rate limit",
            "ratelimit",
            "503",
            "service unavailable",
        )
    )


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
