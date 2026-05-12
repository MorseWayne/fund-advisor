import pytest
import httpx

import src.llm.client as client_module
from src.llm.client import LLMClient, LLMClientError, _response_snapshot


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


def test_extract_content_normal_path():
    text = LLMClient._extract_content(
        {"choices": [{"message": {"content": "hello world"}}]}
    )
    assert text == "hello world"


def test_extract_content_falls_back_to_reasoning_content(caplog):
    """Reasoning models (DeepSeek-R1, QwQ, ...) sometimes leave content empty
    and route output to reasoning_content. We must not error out in that case."""
    text = LLMClient._extract_content(
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "the answer is 42"},
                    "finish_reason": "stop",
                }
            ]
        }
    )
    assert text == "the answer is 42"


def test_extract_content_falls_back_to_reasoning_field():
    text = LLMClient._extract_content(
        {"choices": [{"message": {"content": None, "reasoning": "fallback text"}}]}
    )
    assert text == "fallback text"


def test_extract_content_empty_raises_with_snapshot():
    with pytest.raises(ValueError) as exc_info:
        LLMClient._extract_content(
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "   "},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 4096, "total_tokens": 4196},
            }
        )
    msg = str(exc_info.value)
    assert "empty" in msg
    assert "finish_reason=length" in msg
    assert "completion_tokens" in msg


def test_extract_content_no_choices_includes_snapshot():
    with pytest.raises(ValueError) as exc_info:
        LLMClient._extract_content({"error": {"message": "rate limited"}})
    msg = str(exc_info.value)
    assert "no choices" in msg
    assert "top_keys=['error']" in msg


def test_response_snapshot_compact_shape():
    snap = _response_snapshot(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "id": "abc",
        }
    )
    assert "finish_reason=stop" in snap
    assert "completion_tokens" in snap
    assert "top_keys=['id']" in snap
