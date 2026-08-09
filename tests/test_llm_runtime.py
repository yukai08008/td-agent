from __future__ import annotations

import asyncio

from toe_dac.llm.node.node import Message, MessageRole, RetryConfig, ToolCall, ToolType
from toe_dac.llm.openai_client import OpenAIClient


def test_lightweight_models_keep_expected_api():
    message = Message(role=MessageRole.USER, content="hello")
    call = ToolCall(id="call_1", type=ToolType.FUNCTION, function={"name": "submit"})
    retry = RetryConfig()

    assert message.model_dump()["content"] == "hello"
    assert call.function["name"] == "submit"
    assert retry.retry_on_status == [429, 500, 502, 503, 504]


def test_openai_client_uses_async_standard_library_transport(monkeypatch):
    captured = {}

    async def fake_post_json(url, headers, payload):
        captured.update(url=url, headers=headers, payload=payload)
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
    assert response.content == "ok"
    assert response.usage == {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
