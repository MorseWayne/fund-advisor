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

    client = LLMClient(api_key="test-key", base_url="https://llm.example.com/v1", timeout_seconds=123, stream=False)
    result = await client.generate("hello", max_tokens=8)

    assert result == "OK"
    assert calls == {"count": 2, "timeout": 123}


def test_extract_content_normal_path():
    text = LLMClient._extract_content(
        {"choices": [{"message": {"content": "hello world"}}]}
    )
    assert text == "hello world"


def test_extract_content_ignores_reasoning_content():
    """reasoning_content carries chain-of-thought, not the answer.

    Returning it would serve the model's internal thinking to JSON parsing,
    which then fails because the thinking is narrative prose not a JSON
    object. The only defensible signal is: content empty => ValueError.
    """
    with pytest.raises(ValueError) as exc_info:
        LLMClient._extract_content(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "我们被要求生成一个JSON对象，格式基于...",
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        )
    msg = str(exc_info.value)
    assert "empty" in msg
    assert "finish_reason=length" in msg


def test_extract_content_empty_raises_with_snapshot():
    with pytest.raises(ValueError) as exc_info:
        LLMClient._extract_content(
            {
                "choices": [
                    {
                        "message": {"content": ""},
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


def _sse_bytes(chunks: list[dict]) -> bytes:
    """Encode a list of SSE chunks (and a terminating [DONE]) as raw bytes."""
    lines = [f"data: {client_module.json.dumps(c)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _install_stream_transport(monkeypatch, body: bytes, status_code: int = 200):
    """Patch httpx.AsyncClient so the streaming code path reads `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", PatchedAsyncClient)


@pytest.mark.asyncio
async def test_stream_assembles_content_chunks(monkeypatch, capsys):
    body = _sse_bytes([
        {"choices": [{"delta": {"content": "hello"}}]},
        {"choices": [{"delta": {"content": " world"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ])
    _install_stream_transport(monkeypatch, body)
    client = LLMClient(api_key="k", base_url="https://x.test/v1")
    result = await client.generate("hi")
    assert result == "hello world"
    err = capsys.readouterr().err
    assert "[llm-content]" in err
    assert "hello" in err and "world" in err


@pytest.mark.asyncio
async def test_stream_keeps_reasoning_and_content_separate(monkeypatch, capsys):
    """reasoning_content goes to the think buffer, content to the answer buffer."""
    body = _sse_bytes([
        {"choices": [{"delta": {"reasoning_content": "我先分析"}}]},
        {"choices": [{"delta": {"reasoning_content": "证据包..."}}]},
        {"choices": [{"delta": {"content": "{\"date\":"}}]},
        {"choices": [{"delta": {"content": " \"2026-05-12\"}"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])
    _install_stream_transport(monkeypatch, body)
    client = LLMClient(api_key="k", base_url="https://x.test/v1")
    result = await client.generate("hi")
    # _extract_content reads message.content only; reasoning is not the answer.
    assert result == '{"date": "2026-05-12"}'
    err = capsys.readouterr().err
    assert "[llm-think] 我先分析证据包..." in err
    # New label line after switching streams
    assert "[llm-content]" in err


@pytest.mark.asyncio
async def test_stream_done_terminator_ends_loop(monkeypatch):
    body = _sse_bytes([
        {"choices": [{"delta": {"content": "ok"}}]},
    ])  # [DONE] auto-appended by _sse_bytes
    _install_stream_transport(monkeypatch, body)
    client = LLMClient(api_key="k", base_url="https://x.test/v1")
    assert await client.generate("hi") == "ok"


@pytest.mark.asyncio
async def test_stream_ignores_malformed_chunks(monkeypatch):
    """A junk SSE line in the middle must not crash the stream."""
    pieces = [
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b'data: this-is-not-json\n\n',
        b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    _install_stream_transport(monkeypatch, b"".join(pieces))
    client = LLMClient(api_key="k", base_url="https://x.test/v1")
    assert await client.generate("hi") == "ab"


@pytest.mark.asyncio
async def test_stream_empty_response_raises_with_snapshot(monkeypatch):
    """If the model only streams reasoning_content (no answer), error must surface
    with a useful diagnostic — same contract as non-streaming."""
    body = _sse_bytes([
        {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ])
    _install_stream_transport(monkeypatch, body)
    client = LLMClient(api_key="k", base_url="https://x.test/v1")
    with pytest.raises(LLMClientError) as exc_info:
        await client.generate("hi")
    assert "empty" in str(exc_info.value)
    assert "finish_reason=length" in str(exc_info.value)
