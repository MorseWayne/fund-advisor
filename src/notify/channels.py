"""Notification channels for pushing reports and alerts to chat platforms."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TypeAlias, cast

import httpx
import yaml
from loguru import logger


WECHAT_WORK_WEBHOOK_ENV = "WECHAT_WORK_WEBHOOK_URL"
FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"

WECHAT_WORK_MAX_CONTENT_LENGTH = 4096
FEISHU_MAX_CONTENT_LENGTH = 30_000
WECHAT_WORK_SPLIT_INTERVAL_SECONDS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0

JsonPayload: TypeAlias = dict[str, object]


def _load_notify_config() -> dict[str, object]:
    """Load raw notify config without requiring typed config support."""

    config_path = Path("config/config.yaml")
    if not config_path.exists():
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as file:
            loaded = cast(object, yaml.safe_load(file))
    except Exception as exc:
        logger.warning(f"Failed to load notify config from {config_path}: {exc}")
        return {}

    if not isinstance(loaded, dict):
        return {}

    raw = cast(dict[object, object], loaded)
    notify = raw.get("notify", {})
    if not isinstance(notify, dict):
        return {}

    notify_config = cast(dict[object, object], notify)
    return {key: value for key, value in notify_config.items() if isinstance(key, str)}


def _get_config_webhook_url(channel_name: str, default_env_var: str) -> str:
    """Resolve webhook URL from the raw config fallback."""

    channel_config_obj = _load_notify_config().get(channel_name, {})
    if not isinstance(channel_config_obj, dict):
        return ""

    channel_config = cast(dict[object, object], channel_config_obj)

    configured_url = channel_config.get("webhook_url", "")
    if isinstance(configured_url, str) and configured_url.strip():
        return configured_url.strip()

    env_var_name = channel_config.get("webhook_url_env", default_env_var)
    if isinstance(env_var_name, str) and env_var_name.strip():
        return os.getenv(env_var_name.strip(), "").strip()

    return ""


def _resolve_webhook_url(webhook_url: str, env_var: str, channel_name: str) -> str:
    """Resolve webhook URL using constructor > environment > config order."""

    if webhook_url.strip():
        return webhook_url.strip()

    env_url = os.getenv(env_var, "").strip()
    if env_url:
        return env_url

    return _get_config_webhook_url(channel_name, env_var)


def _split_content(content: str, max_length: int) -> list[str]:
    """Split content into size-bounded chunks, preferring newline boundaries."""

    if len(content) <= max_length:
        return [content]

    chunks: list[str] = []
    remaining = content

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_length]
            split_at = max_length

        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks


class WeChatWorkChannel:
    """WeChat Work group robot webhook notification channel."""

    name: str = "wechat_work"

    def __init__(self, webhook_url: str = "") -> None:
        self.webhook_url: str = _resolve_webhook_url(
            webhook_url,
            WECHAT_WORK_WEBHOOK_ENV,
            self.name,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, content: str, msg_type: str = "markdown") -> bool:
        """Send markdown or text content, splitting messages above 4096 chars."""

        if not self.enabled:
            logger.warning("WeChat Work webhook URL is not configured")
            return False

        chunks = _split_content(content, WECHAT_WORK_MAX_CONTENT_LENGTH)
        success = True

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            for index, chunk in enumerate(chunks):
                chunk_success = await self._send_chunk(client, chunk, msg_type)
                success = success and chunk_success

                if index < len(chunks) - 1:
                    await asyncio.sleep(WECHAT_WORK_SPLIT_INTERVAL_SECONDS)

        return success

    async def _send_chunk(self, client: httpx.AsyncClient, content: str, msg_type: str) -> bool:
        message_type = "markdown" if msg_type == "markdown" else "text"
        payload = self._build_payload(content, message_type)

        if await self._post(client, payload):
            return True

        if message_type == "markdown":
            logger.warning("WeChat Work markdown send failed; retrying as text")
            return await self._post(client, self._build_payload(content, "text"))

        return False

    @staticmethod
    def _build_payload(content: str, msg_type: str) -> JsonPayload:
        if msg_type == "markdown":
            return {"msgtype": "markdown", "markdown": {"content": content}}

        return {"msgtype": "text", "text": {"content": content}}

    async def _post(self, client: httpx.AsyncClient, payload: JsonPayload) -> bool:
        try:
            response = await client.post(self.webhook_url, json=payload)
            _ = response.raise_for_status()
            result_obj = cast(object, response.json())
        except Exception as exc:
            logger.warning(f"WeChat Work webhook request failed: {exc}")
            return False

        if not isinstance(result_obj, dict):
            logger.warning(f"WeChat Work webhook returned non-object response: {result_obj}")
            return False

        result = cast(dict[object, object], result_obj)
        if result.get("errcode") == 0:
            return True

        logger.warning(f"WeChat Work webhook returned error: {result}")
        return False


class FeishuChannel:
    """Feishu/Lark group robot webhook notification channel."""

    name: str = "feishu"

    def __init__(self, webhook_url: str = "") -> None:
        self.webhook_url: str = _resolve_webhook_url(
            webhook_url,
            FEISHU_WEBHOOK_ENV,
            self.name,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, content: str, title: str = "") -> bool:
        """Send content as an interactive card, falling back to text on failure."""

        if not self.enabled:
            logger.warning("Feishu webhook URL is not configured")
            return False

        chunks = _split_content(content, FEISHU_MAX_CONTENT_LENGTH)
        success = True

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            for index, chunk in enumerate(chunks):
                chunk_title = self._chunk_title(title, index, len(chunks))
                chunk_success = await self._send_chunk(client, chunk, chunk_title)
                success = success and chunk_success

        return success

    async def _send_chunk(self, client: httpx.AsyncClient, content: str, title: str) -> bool:
        if await self._post(client, self._build_card_payload(content, title)):
            return True

        logger.warning("Feishu interactive card send failed; retrying as text")
        return await self._post(client, self._build_text_payload(content, title))

    @staticmethod
    def _chunk_title(title: str, index: int, total: int) -> str:
        if total <= 1:
            return title

        base_title = title or "通知"
        return f"{base_title} ({index + 1}/{total})"

    @staticmethod
    def _build_card_payload(content: str, title: str) -> JsonPayload:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": title,
                    },
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content,
                    },
                ],
            },
        }

    @staticmethod
    def _build_text_payload(content: str, title: str) -> JsonPayload:
        text = f"{title}\n\n{content}" if title else content
        return {"msg_type": "text", "content": {"text": text}}

    async def _post(self, client: httpx.AsyncClient, payload: JsonPayload) -> bool:
        try:
            response = await client.post(self.webhook_url, json=payload)
            _ = response.raise_for_status()
            result_obj = cast(object, response.json())
        except Exception as exc:
            logger.warning(f"Feishu webhook request failed: {exc}")
            return False

        if not isinstance(result_obj, dict):
            logger.warning(f"Feishu webhook returned non-object response: {result_obj}")
            return False

        result = cast(dict[object, object], result_obj)
        if _is_feishu_success(result):
            return True

        logger.warning(f"Feishu webhook returned error: {result}")
        return False


def _is_feishu_success(result: dict[object, object]) -> bool:
    code = result.get("code", result.get("StatusCode", result.get("status_code")))
    return code in (0, "0", None) and result.get("StatusCode", 0) in (0, "0")


class NotificationManager:
    """Small registry that broadcasts notifications to enabled channels."""

    def __init__(self) -> None:
        self.channels: dict[str, WeChatWorkChannel | FeishuChannel] = {}

    def add_channel(self, name: str, channel: WeChatWorkChannel | FeishuChannel) -> None:
        """Register a notification channel by name."""

        self.channels[name] = channel

    async def broadcast(self, content: str, title: str = "") -> dict[str, bool]:
        """Send content to all enabled channels and return per-channel success."""

        enabled_channels = {
            name: channel
            for name, channel in self.channels.items()
            if getattr(channel, "enabled", True)
        }
        if not enabled_channels:
            return {}

        tasks = {
            name: asyncio.create_task(self._send_to_channel(channel, content, title))
            for name, channel in enabled_channels.items()
        }

        results: dict[str, bool] = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception as exc:
                logger.warning(f"Notification channel {name} failed: {exc}")
                results[name] = False

        return results

    @staticmethod
    async def _send_to_channel(channel: WeChatWorkChannel | FeishuChannel, content: str, title: str) -> bool:
        if isinstance(channel, FeishuChannel):
            return bool(await channel.send(content, title=title))

        return bool(await channel.send(content))
