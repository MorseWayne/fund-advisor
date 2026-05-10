"""Minimal OpenAI-compatible LLM client."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import cast

import httpx
from loguru import logger


class LLMClientError(RuntimeError):
    """Raised when an LLM request fails after retry handling."""


class LLMClient:
    """Async client for OpenAI-compatible chat completion APIs."""

    RETRY_STATUS_CODES: set[int] = {429, 503}

    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.provider: str = provider
        self.model: str = model
        self.api_key: str | None = api_key or os.environ.get("LLM_API_KEY")
        self.base_url: str = base_url.rstrip("/")
        self.temperature: float = temperature
        self.max_tokens: int = max_tokens
        self.timeout_seconds: float = timeout_seconds

    @classmethod
    def from_config(cls, config: object, *, api_key: str | None = None) -> "LLMClient":
        """Build a client from an ``LLMConfig``-like object."""

        return cls(
            provider=str(getattr(config, "provider", "openai")),
            model=str(getattr(config, "model", "gpt-4o-mini")),
            api_key=api_key,
            base_url=str(getattr(config, "base_url", "https://api.openai.com/v1")),
            temperature=float(getattr(config, "temperature", 0.7)),
            max_tokens=int(getattr(config, "max_tokens", 4096)),
            timeout_seconds=float(getattr(config, "timeout_seconds", 180.0)),
        )

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate text from ``prompt`` using chat completions.

        Retries transient rate-limit/service-unavailable responses (HTTP 429/503)
        up to three attempts with exponential backoff.

        ``temperature`` and ``max_tokens`` may be overridden per-call; when
        omitted the instance defaults are used.
        """

        if not self.api_key:
            message = (
                f"Missing API key for provider '{self.provider}': "
                "set LLM_API_KEY or pass api_key."
            )
            logger.error(message)
            raise LLMClientError(message)

        endpoint = f"{self.base_url}/chat/completions"
        payload = self._build_payload(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        max_attempts = 3
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info(
                        "Calling LLM provider={} model={} attempt={}/{}",
                        self.provider,
                        self.model,
                        attempt,
                        max_attempts,
                    )
                    response = await client.post(endpoint, headers=headers, json=payload)
                    if response.status_code in self.RETRY_STATUS_CODES:
                        await self._retry_or_raise(response, attempt, max_attempts)
                        continue

                    _ = response.raise_for_status()
                    response_json = cast(dict[str, object], response.json())
                    content = self._extract_content(response_json)
                    logger.info("LLM generation completed, chars={}", len(content))
                    return content
                except httpx.TimeoutException as exc:
                    last_error = exc
                    await self._retry_request_error(exc, attempt, max_attempts)
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    logger.exception(
                        "LLM HTTP error provider={} status_code={} attempt={}/{}",
                        self.provider,
                        exc.response.status_code,
                        attempt,
                        max_attempts,
                    )
                    raise LLMClientError(f"LLM request failed: {exc.response.text}") from exc
                except httpx.RequestError as exc:
                    last_error = exc
                    await self._retry_request_error(exc, attempt, max_attempts)
                except (ValueError, KeyError, TypeError) as exc:
                    last_error = exc
                    logger.exception(
                        "LLM request/parsing error provider={} attempt={}/{}",
                        self.provider,
                        attempt,
                        max_attempts,
                    )
                    raise LLMClientError(str(exc)) from exc

        message = f"LLM request failed after {max_attempts} attempts"
        logger.error(message)
        raise LLMClientError(message) from last_error

    async def _retry_request_error(
        self,
        exc: httpx.RequestError,
        attempt: int,
        max_attempts: int,
    ) -> None:
        if attempt >= max_attempts:
            logger.exception(
                "LLM request error exhausted provider={} error_type={} attempt={}/{}",
                self.provider,
                type(exc).__name__,
                attempt,
                max_attempts,
            )
            raise LLMClientError(f"LLM request failed after {max_attempts} attempts: {type(exc).__name__}") from exc

        delay = 2.0 ** (attempt - 1)
        logger.warning(
            "LLM request error provider={} error_type={} attempt={}/{} delay={}s",
            self.provider,
            type(exc).__name__,
            attempt,
            max_attempts,
            delay,
        )
        await asyncio.sleep(delay)

    def _build_payload(
        self,
        *,
        prompt: str,
        system_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        return {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }

    async def _retry_or_raise(
        self,
        response: httpx.Response,
        attempt: int,
        max_attempts: int,
    ) -> None:
        if attempt >= max_attempts:
            logger.error(
                "LLM retryable status exhausted provider={} status_code={} body={}",
                self.provider,
                response.status_code,
                response.text,
            )
            _ = response.raise_for_status()

        delay = self._retry_delay(response, attempt)
        logger.warning(
            "LLM retryable status provider={} status_code={} attempt={}/{} delay={}s",
            self.provider,
            response.status_code,
            attempt,
            max_attempts,
            delay,
        )
        await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = str(response.headers.get("Retry-After") or "")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                logger.warning("Invalid Retry-After header: {}", retry_after)
        return 2.0 ** (attempt - 1)

    @staticmethod
    def _extract_content(response_json: dict[str, object]) -> str:
        choices_obj = response_json.get("choices")
        if not isinstance(choices_obj, list) or not choices_obj:
            raise ValueError("LLM response contains no choices")
        choices = cast(list[object], choices_obj)
        first_choice = choices[0]
        if not isinstance(first_choice, Mapping):
            raise ValueError("LLM response choice is malformed")
        first_choice_map = cast(Mapping[str, object], first_choice)
        message = first_choice_map.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("LLM response message is malformed")
        message_map = cast(Mapping[str, object], message)
        content = message_map.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM response content is empty")
        return content.strip()
