"""Minimal OpenAI-compatible LLM client."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
        json_mode: bool = False,
    ) -> dict[str, object]:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

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
        """Pull text out of an OpenAI-compatible chat completion response.

        Standard path: ``choices[0].message.content``. For reasoning models
        (DeepSeek-R1, QwQ, deepseek-v*-pro, ...) some gateways route the
        answer to ``reasoning_content`` and leave ``content`` empty; we fall
        back to that, then to ``reasoning``. If everything is empty we raise
        a ValueError carrying a one-line snapshot of the response (finish
        reason, usage, present keys) so the log immediately shows whether
        the model was truncated, refused, or simply mis-routed.
        """
        choices_obj = response_json.get("choices")
        if not isinstance(choices_obj, list) or not choices_obj:
            raise ValueError(f"LLM response contains no choices: {_response_snapshot(response_json)}")
        choices = cast(list[object], choices_obj)
        first_choice = choices[0]
        if not isinstance(first_choice, Mapping):
            raise ValueError(f"LLM response choice is malformed: {_response_snapshot(response_json)}")
        first_choice_map = cast(Mapping[str, object], first_choice)
        message = first_choice_map.get("message")
        if not isinstance(message, Mapping):
            raise ValueError(f"LLM response message is malformed: {_response_snapshot(response_json)}")
        message_map = cast(Mapping[str, object], message)

        for field in ("content", "reasoning_content", "reasoning"):
            value = message_map.get(field)
            if isinstance(value, str) and value.strip():
                if field != "content":
                    logger.warning(
                        "LLM content empty, falling back to '{}' field "
                        "(reasoning model output not merged by gateway)",
                        field,
                    )
                return value.strip()

        raise ValueError(
            f"LLM response content is empty: {_response_snapshot(response_json)}"
        )

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> dict[str, object]:
        """Generate structured JSON output using chat completions.

        When *json_mode* is True (default), adds ``response_format`` to
        request JSON mode from providers that support it. The response is
        parsed as JSON, with fallback extraction from markdown code fences
        if the raw content is not valid JSON.

        Returns:
            Parsed JSON response as a dictionary.
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
            json_mode=json_mode,
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
                        "Calling LLM (JSON) provider={} model={} attempt={}/{}",
                        self.provider, self.model, attempt, max_attempts,
                    )
                    response = await client.post(endpoint, headers=headers, json=payload)
                    if response.status_code in self.RETRY_STATUS_CODES:
                        await self._retry_or_raise(response, attempt, max_attempts)
                        continue

                    _ = response.raise_for_status()
                    response_json = cast(dict[str, object], response.json())
                    content = self._extract_content(response_json)
                    parsed = _extract_json(content)
                    logger.info("LLM JSON generation completed, keys={}", len(parsed))
                    return parsed
                except httpx.TimeoutException as exc:
                    last_error = exc
                    await self._retry_request_error(exc, attempt, max_attempts)
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    logger.exception(
                        "LLM HTTP error provider={} status_code={} attempt={}/{}",
                        self.provider, exc.response.status_code, attempt, max_attempts,
                    )
                    raise LLMClientError(f"LLM request failed: {exc.response.text}") from exc
                except httpx.RequestError as exc:
                    last_error = exc
                    await self._retry_request_error(exc, attempt, max_attempts)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    last_error = exc
                    logger.exception(
                        "LLM JSON parse error provider={} attempt={}/{}",
                        self.provider, attempt, max_attempts,
                    )
                    if attempt >= max_attempts:
                        raise LLMClientError(
                            f"Failed to parse JSON response after {max_attempts} attempts: {exc}"
                        ) from exc
                    await asyncio.sleep(2.0 ** (attempt - 1))

        message = f"LLM JSON request failed after {max_attempts} attempts"
        logger.error(message)
        raise LLMClientError(message) from last_error


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _response_snapshot(response_json: dict[str, object]) -> str:
    """One-line diagnostic summary of an LLM response.

    Surfaces ``finish_reason``, ``usage``, and which message fields are present
    so empty-content failures show their root cause in the log without
    dumping the whole payload.
    """
    bits: list[str] = []
    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else None
        if first is not None:
            finish_reason = first.get("finish_reason")
            if finish_reason:
                bits.append(f"finish_reason={finish_reason}")
            message = first.get("message")
            if isinstance(message, Mapping):
                present = sorted(
                    k for k, v in message.items()
                    if isinstance(v, str) and v.strip() or (not isinstance(v, str) and v not in (None, [], {}))
                )
                bits.append(f"message_keys={present}")
            else:
                bits.append("message=None")
    else:
        bits.append("choices=empty")
    usage = response_json.get("usage")
    if isinstance(usage, Mapping):
        compact = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage}
        if compact:
            bits.append(f"usage={compact}")
    top_keys = sorted(k for k in response_json.keys() if k not in ("choices", "usage"))
    if top_keys:
        bits.append(f"top_keys={top_keys}")
    return " ".join(bits) if bits else "(empty response)"


def _extract_json(content: str) -> dict[str, object]:
    """Parse JSON from LLM response, with markdown fence fallback.

    Attempts in order:
    1. Direct JSON parse of the full content
    2. Extract and parse content inside ```json ... ``` fences
    3. Extract and parse content inside ``` ... ``` fences (any language)

    Raises ``json.JSONDecodeError`` if all attempts fail.
    """

    content = content.strip()

    # Attempt 1: direct parse
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return cast(dict[str, object], result)
    except json.JSONDecodeError:
        pass

    # Attempt 2: extract from ```json fences
    matches = _JSON_FENCE_RE.findall(content)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return cast(dict[str, object], result)
        except json.JSONDecodeError:
            continue

    # Last resort: try to find the first { ... } block
    brace_start = content.find("{")
    brace_end = content.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            result = json.loads(content[brace_start:brace_end + 1])
            if isinstance(result, dict):
                return cast(dict[str, object], result)
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError(
        f"Failed to extract valid JSON from response: {content[:200]}...",
        content, 0,
    )
