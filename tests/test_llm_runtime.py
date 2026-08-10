from __future__ import annotations

import asyncio
import http.client

import pytest

from toe_dac.llm.node.node import Message, MessageRole, RetryConfig, ToolCall, ToolType
from toe_dac.llm.openai_client import ModelTransportError, OpenAIClient


def test_lightweight_models_keep_expected_api():
    message = Message(role=MessageRole.USER, content="hello")
    call = ToolCall(id="call_1", type=ToolType.FUNCTION, function={"name": "submit"})
    retry = RetryConfig()

    assert message.model_dump()["content"] == "hello"
    assert call.function["name"] == "submit"
    assert retry.retry_on_status == [429, 500, 502, 503, 504]


def test_openai_client_uses_async_standard_library_transport(monkeypatch):
    captured = {}

    async def fake_post_json(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    monkeypatch.setattr("toe_dac.llm.openai_client.post_json", fake_post_json)
    client = OpenAIClient(api_key="test-key", api_base="https://example.test/v1", model="test-model")

    response = asyncio.run(client.generate([Message(role=MessageRole.USER, content="hello")]))

    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["timeout"] == 45
    assert response.content == "ok"
    assert response.usage == {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}


def test_openai_client_retries_transient_transport_then_succeeds(monkeypatch):
    calls = 0
    retries = []

    async def fake_post_json(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http.client.IncompleteRead(b'{"partial":', 20)
        if calls == 2:
            raise http.client.RemoteDisconnected("closed")
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("toe_dac.llm.openai_client.post_json", fake_post_json)
    monkeypatch.setattr("toe_dac.llm.openai_client.asyncio.sleep", no_sleep)
    client = OpenAIClient(
        api_key="key", api_base="https://example.test/v1", model="model",
        retry_config=RetryConfig(max_retries=2, base_delay=0.5, max_delay=2),
    )
    client.retry_callback = retries.append

    response = asyncio.run(client.generate([Message(role=MessageRole.USER, content="hello")]))

    assert response.content == "ok"
    assert calls == 3
    assert [item["attempt"] for item in retries] == [1, 2]


def test_openai_client_reports_transport_attempts_after_exhaustion(monkeypatch):
    async def fake_post_json(url, headers, payload, timeout):
        raise http.client.RemoteDisconnected("closed")

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("toe_dac.llm.openai_client.post_json", fake_post_json)
    monkeypatch.setattr("toe_dac.llm.openai_client.asyncio.sleep", no_sleep)
    client = OpenAIClient(
        api_key="key", api_base="https://example.test/v1", model="model",
        retry_config=RetryConfig(max_retries=1, base_delay=0),
    )

    with pytest.raises(ModelTransportError) as caught:
        asyncio.run(client.generate([Message(role=MessageRole.USER, content="hello")]))

    assert len(caught.value.attempts) == 2
    assert all(item["error_type"] == "RemoteDisconnected" for item in caught.value.attempts)
