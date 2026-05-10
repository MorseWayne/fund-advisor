import pytest
import httpx

import src.llm.client as client_module
from src.llm.client import LLMClient, LLMClientError


def test_from_config_reads_unified_llm_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "llm-test-key")

    class Config:
        provider = "siliconflow"
        model = "Qwen/Qwen3-32B"
        base_url = "https://api.siliconflow.cn/v1/"
        temperature = 0.2
        max_tokens=512
        timeout_seconds = 240

    client = LLMClient.from_config(Config())

    assert client.provider == "siliconflow"
    assert client.model == "Qwen/Qwen3-32B"
    assert client.api_key == "llm-test-key"
    assert client.base_url == "https://api.siliconflow.cn/v1"
    assert client.temperature == 0.2
    assert client.max_tokens == 512
    assert client.timeout_seconds == 240


@pytest.mark.asyncio
async def test_missing_api_key_error_names_configured_provider_and_env(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = LLMClient(
        provider="moonshot",
        model="moonshot-v1-8k",
        base_url="https://api.moonshot.cn/v1",
    )

    with pytest.raises(LLMClientError) as exc_info:
        await client.generate("hello")

    message = str(exc_info.value)
    assert "moonshot" in message
    assert "LLM_API_KEY" in message
    assert "DeepSeek" not in message


def test_build_payload_uses_standard_chat_completions_shape():
    client = LLMClient(
        provider="openai-compatible",
        model="custom-chat-model",
        api_key="test-key",
        base_url="https://llm.example.com/v1",
        temperature=0.1,
        max_tokens=256,
    )

    payload = client._build_payload(prompt="write report", system_prompt="be concise")

    assert payload == {
        "model": "custom-chat-model",
        "messages": [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": "write report"},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }


@pytest.mark.asyncio
async def test_generate_retries_read_timeout(monkeypatch):
    calls = {"count": 0, "timeout": None}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, endpoint, *, headers, json):
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ReadTimeout("slow response")
            request = httpx.Request("POST", endpoint)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "OK"}}]},
                request=request,
            )

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(client_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)

    client = LLMClient(api_key="test-key", base_url="https://llm.example.com/v1", timeout_seconds=123)
    result = await client.generate("hello", max_tokens=8)

    assert result == "OK"
    assert calls == {"count": 2, "timeout": 123}
