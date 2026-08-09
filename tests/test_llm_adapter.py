from types import SimpleNamespace

import pytest

import json

from toe_dac.llm_adapter import AndybotLLMAdapter, LLMOutputError, TOEDACLLMAdapter


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
