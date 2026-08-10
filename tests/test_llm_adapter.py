from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import json

from toe_dac.llm_adapter import AndybotLLMAdapter, LLMOutputError, TOEDACLLMAdapter
from toe_dac.llm.openai_client import ModelTransportError


def test_parse_andybot_tool_call():
    response = SimpleNamespace(
        tool_calls=[SimpleNamespace(function={
            "name": "submit_target",
            "arguments": '{"status":"needs_human","reason":"ambiguous","question":"scope?"}',
        })],
        content=None,
        model_id="glm-5",
        usage={"input": 10, "output": 5},
        finish_reason="tool_calls",
    )
    result = AndybotLLMAdapter._parse(response, "submit_target")
    assert result.data["status"] == "needs_human"
    assert result.model_id == "glm-5"


def test_parse_json_content_fallback():
    response = SimpleNamespace(
        tool_calls=None,
        content='```json\n{"status":"needs_human","reason":"x","question":"y"}\n```',
        model_id="model",
        usage=None,
        finish_reason="stop",
    )
    assert AndybotLLMAdapter._parse(response, "submit_target").data["question"] == "y"


def test_parse_rejects_unstructured_content():
    response = SimpleNamespace(
        tool_calls=None, content="I think you should clarify", model_id="model", usage=None, finish_reason="stop"
    )
    with pytest.raises(LLMOutputError):
        AndybotLLMAdapter._parse(response, "submit_target")


def test_parse_distinguishes_empty_response_from_non_object_output():
    empty = SimpleNamespace(
        tool_calls=None, content="", model_id="model", usage=None, finish_reason="stop",
    )
    with pytest.raises(LLMOutputError, match="empty response without required tool submit_target"):
        AndybotLLMAdapter._parse(empty, "submit_target")

    non_object = _tool_response('["not", "an", "object"]')
    with pytest.raises(LLMOutputError, match="arguments must be an object, got list"):
        AndybotLLMAdapter._parse(non_object, "submit_target")


def _tool_response(arguments, model_id="deepseek-v4-flash"):
    return SimpleNamespace(
        tool_calls=[SimpleNamespace(function={
            "name": "submit_target",
            "arguments": arguments,
        })],
        content=None,
        model_id=model_id,
        usage={"input": 10, "output": 5},
        finish_reason="tool_calls",
    )


def test_malformed_tool_arguments_are_repaired_once():
    adapter = object.__new__(TOEDACLLMAdapter)
    adapter._call = AsyncMock(side_effect=[
        _tool_response('{"status":"accepted" "reason":"missing comma"}'),
        _tool_response('{"status":"needs_human","reason":"ambiguous","question":"scope?"}'),
    ])

    result = __import__("asyncio").run(adapter.generate_structured(
        phase="target",
        system_prompt="target",
        payload={"conversation": []},
        tool_name="submit_target",
        schema={"type": "object"},
    ))

    assert result.repaired is True
    assert result.data["status"] == "needs_human"
    assert [item["status"] for item in result.repair_evidence] == ["failed", "succeeded"]
    assert adapter._call.await_count == 2
    assert adapter._call.await_args_list[1].kwargs["allow_runtime_tools"] is False


def test_repeated_malformed_tool_arguments_preserve_both_attempts():
    adapter = object.__new__(TOEDACLLMAdapter)
    adapter._call = AsyncMock(side_effect=[
        _tool_response('{"status":"accepted" "reason":"first"}'),
        _tool_response('{"status":"accepted" "reason":"second"}'),
    ])

    with pytest.raises(LLMOutputError) as caught:
        __import__("asyncio").run(adapter.generate_structured(
            phase="target",
            system_prompt="target",
            payload={"conversation": []},
            tool_name="submit_target",
            schema={"type": "object"},
        ))

    assert [item["stage"] for item in caught.value.attempts] == ["initial", "repair"]
    assert all(item["status"] == "failed" for item in caught.value.attempts)


def test_model_transport_failure_is_normalized_for_toe_dac_recovery():
    adapter = object.__new__(TOEDACLLMAdapter)
    adapter.client = SimpleNamespace(generate=AsyncMock(side_effect=ConnectionError("stream closed")))
    adapter.model_config = {"id": "test-model"}
    adapter.runtime_snapshot = SimpleNamespace(
        skills=[], available_skills=[],
        render=lambda system_prompt, phase: system_prompt,
    )
    adapter.llm_module = SimpleNamespace(
        Message=lambda **kwargs: kwargs,
        MessageRole=SimpleNamespace(USER="user", SYSTEM="system"),
    )
    adapter.skill_runtime = SimpleNamespace(tool_definitions=lambda names, phase: [])
    adapter._last_skill_events = []
    progress = []

    with pytest.raises(LLMOutputError) as caught:
        __import__("asyncio").run(adapter._call(
            "target", {"conversation": []}, "submit_target", {"type": "object"},
            phase="target", progress_callback=progress.append,
        ))

    assert "ConnectionError" in str(caught.value)
    assert caught.value.model_id == "test-model"
    assert caught.value.attempts[0]["stage"] == "model_call"
    assert progress[-1]["type"] == "model_call_failed"


def test_exhausted_model_transport_is_classified_and_preserves_attempts():
    transport_error = ModelTransportError([
        {"attempt": 1, "error_type": "IncompleteRead", "error": "partial"},
        {"attempt": 2, "error_type": "RemoteDisconnected", "error": "closed"},
    ])
    adapter = object.__new__(TOEDACLLMAdapter)
    adapter.client = SimpleNamespace(generate=AsyncMock(side_effect=transport_error))
    adapter.model_config = {"id": "test-model"}
    adapter.runtime_snapshot = SimpleNamespace(
        skills=[], available_skills=[], render=lambda system_prompt, phase: system_prompt,
    )
    adapter.llm_module = SimpleNamespace(
        Message=lambda **kwargs: kwargs,
        MessageRole=SimpleNamespace(USER="user", SYSTEM="system"),
    )
    adapter.skill_runtime = SimpleNamespace(tool_definitions=lambda names, phase: [])
    adapter._last_skill_events = []

    with pytest.raises(LLMOutputError) as caught:
        __import__("asyncio").run(adapter._call(
            "target", {"conversation": []}, "submit_target", {"type": "object"}, phase="target",
        ))

    assert caught.value.category == "model_transport"
    assert caught.value.attempts[0]["transport_attempts"] == transport_error.attempts


def test_local_model_config_creates_migrated_client_without_network(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({
        "models": [{
            "id": "test-model",
            "name": "Test Model",
            "vendor": "test",
            "apiKeyEnv": "TOE_DAC_TEST_API_KEY",
            "url": "https://example.invalid/v1/chat/completions",
            "maxInputTokens": 1000,
            "maxOutputTokens": 100,
            "supportsToolCall": True,
            "supportsImages": False,
            "enabled": True,
        }]
    }), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOE_DAC_TEST_API_KEY=local-secret\n", encoding="utf-8")
    adapter = TOEDACLLMAdapter(config, "test-model")
    assert adapter.client.model == "test-model"
    assert adapter.client.api_base == "https://example.invalid/v1/chat/completions"
    assert adapter.llm_module.__name__ == "toe_dac.llm"


def test_inline_api_key_is_not_supported(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "unsafe", "enabled": True, "apiKey": "must-not-be-used", "url": "https://example.invalid"
    }]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must declare apiKeyEnv"):
        TOEDACLLMAdapter(config, "unsafe")
