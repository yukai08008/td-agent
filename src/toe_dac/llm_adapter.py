from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import llm as local_llm
from .environment import load_environment


@dataclass
class StructuredLLMResult:
    data: dict[str, Any]
    model_id: str | None
    usage: dict[str, int]
    finish_reason: str | None
    raw_content: str | None
    repaired: bool = False


class LLMOutputError(ValueError):
    pass


class TOEDACLLMAdapter:
    """Adapter over the LLM client migrated from Andybot."""

    def __init__(self, model_config_path: str | Path, model_id: str):
        self.model_config_path = Path(model_config_path).resolve()
        self.model_config = self._load_model_config(model_id)
        environment_root = (
            self.model_config_path.parent.parent
            if self.model_config_path.parent.name == "config"
            else self.model_config_path.parent
        )
        environment = load_environment(environment_root)
        api_key_env = str(self.model_config.get("apiKeyEnv", "")).strip()
        if not api_key_env:
            raise ValueError(f"model {model_id} must declare apiKeyEnv; inline apiKey is not supported")
        api_key = environment.get(api_key_env, "")
        if not api_key:
            raise ValueError(f"missing API key environment variable: {api_key_env}")
        self.llm_module = local_llm
        provider = self.llm_module.LLMProvider.OPENAI
        self.client = self.llm_module.LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self.model_config["url"],
            model=self.model_config["id"],
        )

    async def generate_structured(
        self,
        *,
        phase: str,
        system_prompt: str,
        payload: dict[str, Any],
        tool_name: str,
        schema: dict[str, Any],
        allow_repair: bool = True,
    ) -> StructuredLLMResult:
        result = await self._call(system_prompt, payload, tool_name, schema)
        try:
            return self._parse(result, tool_name)
        except LLMOutputError:
            if not allow_repair:
                raise
            repair_payload = {
                "phase": phase,
                "instruction": "上一次输出不符合工具 Schema。只调用指定工具重新提交，不要解释。",
                "invalid_content": result.content,
                "required_schema": schema,
                "original_input": payload,
            }
            repaired = await self._call(system_prompt, repair_payload, tool_name, schema)
            parsed = self._parse(repaired, tool_name)
            parsed.repaired = True
            return parsed

    async def _call(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        tool_name: str,
        schema: dict[str, Any],
    ):
        messages = [
            self.llm_module.Message(role=self.llm_module.MessageRole.SYSTEM, content=system_prompt),
            self.llm_module.Message(
                role=self.llm_module.MessageRole.USER,
                content=json.dumps(payload, ensure_ascii=False, indent=2),
            ),
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "提交当前 TOE-DAC 阶段的结构化结果",
                "parameters": schema,
            },
        }]
        return await self.client.generate(messages, tools=tools)

    @staticmethod
    def _parse(response: Any, tool_name: str) -> StructuredLLMResult:
        data = None
        for call in response.tool_calls or []:
            function = call.function
            if function.get("name") != tool_name:
                continue
            arguments = function.get("arguments", {})
            data = json.loads(arguments) if isinstance(arguments, str) else arguments
            break
        if data is None and response.content:
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.splitlines()
                content = "\n".join(lines[1:-1])
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise LLMOutputError("model returned neither the required tool call nor valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMOutputError("structured model output must be an object")
        return StructuredLLMResult(
            data=data,
            model_id=response.model_id,
            usage=response.usage or {},
            finish_reason=response.finish_reason,
            raw_content=response.content,
        )

    def _load_model_config(self, model_id: str) -> dict[str, Any]:
        path = self.model_config_path
        if not path.exists():
            raise FileNotFoundError(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for model in data.get("models", []):
            if model.get("id") == model_id:
                if not model.get("enabled", False):
                    raise ValueError(f"Andybot model is disabled: {model_id}")
                return model
        raise KeyError(f"Andybot model not found: {model_id}")

TARGET_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "target": {
            "type": "object",
            "properties": {
                "positive": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "negative": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "acceptance_criteria": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["description", "required"],
                    },
                },
            },
            "required": ["positive", "negative", "acceptance_criteria"],
        },
    },
    "required": ["status", "reason"],
}

OBSERVATION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "observation": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "source_type": {"type": "string"},
                            "source_ref": {"type": ["string", "null"]},
                        },
                        "required": ["description", "source_type"],
                    },
                },
                "unknowns": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["facts", "unknowns"],
        },
    },
    "required": ["status", "reason"],
}

ESTIMATE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "estimate": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["feasible", "not_feasible"]},
                "risks": {"type": "array", "items": {"type": "string"}},
                "cost": {"type": "object"},
                "information_gaps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "risks", "cost", "information_gaps"],
        },
    },
    "required": ["status", "reason"],
}

PLAN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "plan": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "version": {"type": "integer"},
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "action_id": {"type": "string"},
                            "objective": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "instruction": {"type": "string"},
                            "assertions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string"},
                                        "required": {"type": "boolean"},
                                    },
                                    "required": ["description", "required"],
                                },
                            },
                            "max_attempts": {"type": "integer", "minimum": 1},
                        },
                        "required": ["action_id", "objective", "depends_on", "instruction", "assertions", "max_attempts"],
                    },
                },
            },
            "required": ["actions"],
        },
    },
    "required": ["status", "reason"],
}


# Backward-compatible import for code written during the first POC iteration.
AndybotLLMAdapter = TOEDACLLMAdapter
