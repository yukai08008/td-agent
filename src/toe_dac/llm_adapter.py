from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llm as local_llm
from .environment import load_environment
from .runtime_content import RuntimeContentLoader, RuntimePromptSnapshot
from .skill_runtime import SkillToolRuntime


@dataclass
class StructuredLLMResult:
    data: dict[str, Any]
    model_id: str | None
    usage: dict[str, int]
    finish_reason: str | None
    raw_content: str | None
    repaired: bool = False
    repair_evidence: list[dict[str, Any]] = field(default_factory=list)
    skill_events: list[dict[str, Any]] = field(default_factory=list)


class LLMOutputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_output: str | None = None,
        model_id: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.raw_output = raw_output
        self.model_id = model_id
        self.attempts = attempts or []


class TOEDACLLMAdapter:
    """Adapter over the LLM client migrated from Andybot."""

    def __init__(
        self,
        model_config_path: str | Path,
        model_id: str,
        runtime_snapshot: RuntimePromptSnapshot | None = None,
        skill_runtime: SkillToolRuntime | None = None,
    ):
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
        if runtime_snapshot is None:
            try:
                runtime_snapshot = RuntimeContentLoader().load()
            except FileNotFoundError:
                runtime_snapshot = RuntimePromptSnapshot.empty()
        self.runtime_snapshot = runtime_snapshot
        self.skill_runtime = skill_runtime or SkillToolRuntime()
        self._last_skill_events: list[dict[str, Any]] = []
        provider = self.llm_module.LLMProvider.OPENAI
        self.client = self.llm_module.LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self.model_config["url"],
            model=self.model_config["id"],
        )

    def configure_evidence(self, screenshot_dir: Path, session_id: str) -> None:
        self.skill_runtime.configure_evidence(screenshot_dir, session_id)

    async def generate_structured(
        self,
        *,
        phase: str,
        system_prompt: str,
        payload: dict[str, Any],
        tool_name: str,
        schema: dict[str, Any],
        allow_repair: bool = True,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StructuredLLMResult:
        self._last_skill_events = []
        result = await self._call(
            system_prompt, payload, tool_name, schema,
            phase=phase, progress_callback=progress_callback,
        )
        try:
            parsed = self._parse(result, tool_name)
            parsed.skill_events = list(self._last_skill_events)
            return parsed
        except LLMOutputError as initial_error:
            if not allow_repair:
                raise
            if progress_callback:
                progress_callback({"type": "repair_started", "phase": phase, "reason": str(initial_error)})
            repair_payload = {
                "phase": phase,
                "instruction": "上一次输出不符合工具 Schema。只调用指定工具重新提交，不要解释。",
                "invalid_content": initial_error.raw_output or result.content,
                "required_schema": schema,
                "original_input": payload,
            }
            repaired = await self._call(
                system_prompt, repair_payload, tool_name, schema,
                phase=phase, progress_callback=progress_callback,
            )
            try:
                parsed = self._parse(repaired, tool_name)
            except LLMOutputError as repair_error:
                repair_error.attempts = [
                    self._error_evidence("initial", initial_error),
                    self._error_evidence("repair", repair_error),
                ]
                raise
            parsed.repaired = True
            parsed.skill_events = list(self._last_skill_events)
            parsed.repair_evidence = [
                self._error_evidence("initial", initial_error),
                {
                    "stage": "repair",
                    "status": "succeeded",
                    "model_id": parsed.model_id,
                    "raw_output": self._response_output(repaired),
                },
            ]
            return parsed

    async def _call(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        tool_name: str,
        schema: dict[str, Any],
        *,
        phase: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        phase_tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "提交当前 TOE-DAC 阶段的结构化结果",
                "parameters": schema,
            },
        }
        available_names = [entry.name for entry in self.runtime_snapshot.available_skills]
        load_tool = {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "按需加载技能正文。仅当技能索引表明该技能与当前任务相关时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string", "enum": available_names},
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["names"],
                    "additionalProperties": False,
                },
            },
        }
        transcript = [
            self.llm_module.Message(
                role=self.llm_module.MessageRole.USER,
                content=json.dumps(payload, ensure_ascii=False, indent=2),
            ),
        ]
        skill_call_counts: dict[str, int] = {}
        for _ in range(8):
            messages = [
                self.llm_module.Message(
                    role=self.llm_module.MessageRole.SYSTEM,
                    content=self.runtime_snapshot.render(system_prompt, phase=phase),
                ),
                *transcript,
            ]
            tools = [phase_tool]
            if available_names and phase == "observe":
                tools.append(load_tool)
            active_names = {skill.name for skill in self.runtime_snapshot.skills}
            tools.extend(self.skill_runtime.tool_definitions(active_names, phase))
            model_started = time.monotonic()
            if progress_callback:
                progress_callback({"type": "model_call_started", "phase": phase})
            try:
                response = await self.client.generate(messages, tools=tools)
            except Exception as exc:
                if progress_callback:
                    progress_callback({
                        "type": "model_call_failed",
                        "phase": phase,
                        "duration_ms": round((time.monotonic() - model_started) * 1000, 1),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                raise LLMOutputError(
                    f"model runtime failed: {type(exc).__name__}: {exc}",
                    model_id=str(self.model_config.get("id", "")) or None,
                    attempts=[{
                        "stage": "model_call",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }],
                ) from exc
            if progress_callback:
                progress_callback({
                    "type": "model_call_completed",
                    "phase": phase,
                    "duration_ms": round((time.monotonic() - model_started) * 1000, 1),
                })
            runtime_tool_names = {
                tool["function"]["name"]
                for tool in tools
                if tool["function"]["name"] not in {tool_name, "load_skill"}
            }
            runtime_calls = [
                call for call in getattr(response, "tool_calls", None) or []
                if call.function.get("name") == "load_skill"
                or call.function.get("name") in runtime_tool_names
            ]
            if not runtime_calls:
                return response
            transcript.append(self.llm_module.Message(
                role=self.llm_module.MessageRole.ASSISTANT,
                content=getattr(response, "content", None) or "",
                tool_calls=[self._tool_call_dict(call) for call in runtime_calls],
            ))
            for call in runtime_calls:
                name = call.function.get("name", "")
                arguments = self._tool_arguments(call, response)
                if name == "load_skill":
                    output = self._activate_skills(arguments, response)
                    if progress_callback:
                        progress_callback({
                            "type": "skill_loaded" if output.get("ok") else "skill_load_failed",
                            "phase": phase,
                            "skills": arguments.get("names", []),
                            "error": output.get("error"),
                        })
                else:
                    skill_call_counts[name] = skill_call_counts.get(name, 0) + 1
                    call_number = skill_call_counts[name]
                    if skill_call_counts[name] > 3:
                        output = {
                            "ok": False,
                            "error": f"{name} exceeded the per-phase call budget of 3",
                            "instruction": "Use collected evidence or ask the human; do not retry.",
                        }
                        self._last_skill_events.append({
                            "skill": "agent-browser" if name.startswith("agent_browser") else "alex-serp",
                            "tool": name,
                            "status": "failed",
                            "error_type": "SkillBudgetExceeded",
                            "error": output["error"],
                            "attempt_count": skill_call_counts[name],
                        })
                        if progress_callback:
                            progress_callback({
                                "type": "skill_tool_failed", "phase": phase, "tool": name,
                                "call_number": call_number, "budget": 3, "error": output["error"],
                            })
                    else:
                        if progress_callback:
                            progress_callback({
                                "type": "skill_tool_started", "phase": phase, "tool": name,
                                "call_number": call_number, "budget": 3,
                                "input": arguments,
                            })
                        result = await self.skill_runtime.execute(
                            name,
                            arguments,
                            progress_callback=progress_callback,
                        )
                        output = result.output
                        self._last_skill_events.append(result.event)
                        if progress_callback:
                            progress_callback({
                                "type": "skill_tool_completed" if output.get("ok") else "skill_tool_failed",
                                "phase": phase, "tool": name, "call_number": call_number, "budget": 3,
                                "duration_ms": result.event.get("duration_ms"),
                                "attempt_count": result.event.get("attempt_count"),
                                "result_count": output.get("count"),
                                "error": output.get("error"),
                            })
                transcript.append(self.llm_module.Message(
                    role=self.llm_module.MessageRole.TOOL,
                    content=json.dumps(output, ensure_ascii=False),
                    name=name,
                    tool_call_id=getattr(call, "id", "") or "runtime_tool",
                ))
        raise LLMOutputError(
            "progressive skill/tool loop exceeded 8 rounds",
            attempts=list(self._last_skill_events),
        )

    def _activate_skills(self, arguments: dict[str, Any], response: Any) -> dict[str, Any]:
        names = arguments.get("names", [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise LLMOutputError("load_skill names must be a list of strings")
        before = {skill.name for skill in self.runtime_snapshot.skills}
        try:
            self.runtime_snapshot = self.runtime_snapshot.activate(names)
        except Exception as exc:
            event = {
                "skill": ",".join(names),
                "tool": "load_skill",
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "attempt_count": 1,
            }
            self._last_skill_events.append(event)
            return {"ok": False, "error": str(exc), "instruction": "Do not blindly retry; choose another path or ask the human."}
        loaded = [skill.name for skill in self.runtime_snapshot.skills if skill.name not in before]
        self._last_skill_events.append({
            "skill": ",".join(names),
            "tool": "load_skill",
            "status": "succeeded",
            "loaded": loaded,
            "already_loaded": [name for name in names if name in before],
            "attempt_count": 1,
        })
        return {"ok": True, "loaded": loaded, "already_loaded": [name for name in names if name in before]}

    @staticmethod
    def _tool_call_dict(call: Any) -> dict[str, Any]:
        return {
            "id": getattr(call, "id", "") or "runtime_tool",
            "type": "function",
            "function": call.function,
        }

    @staticmethod
    def _tool_arguments(call: Any, response: Any) -> dict[str, Any]:
        arguments = call.function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMOutputError(
                    f"invalid JSON in {call.function.get('name')} tool arguments: {exc}",
                    raw_output=arguments,
                    model_id=response.model_id,
                ) from exc
        if not isinstance(arguments, dict):
            raise LLMOutputError(f"{call.function.get('name')} arguments must be an object")
        return arguments

    @staticmethod
    def _parse(response: Any, tool_name: str) -> StructuredLLMResult:
        data = None
        for call in response.tool_calls or []:
            function = call.function
            if function.get("name") != tool_name:
                continue
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    data = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise LLMOutputError(
                        f"invalid JSON in {tool_name} tool arguments: {exc}",
                        raw_output=arguments,
                        model_id=response.model_id,
                    ) from exc
            else:
                data = arguments
            break
        if data is None and response.content:
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.splitlines()
                content = "\n".join(lines[1:-1])
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise LLMOutputError(
                    "model returned neither the required tool call nor valid JSON",
                    raw_output=content,
                    model_id=response.model_id,
                ) from exc
        if not isinstance(data, dict):
            raise LLMOutputError("structured model output must be an object")
        return StructuredLLMResult(
            data=data,
            model_id=response.model_id,
            usage=response.usage or {},
            finish_reason=response.finish_reason,
            raw_content=response.content,
        )

    @staticmethod
    def _response_output(response: Any) -> str | None:
        for call in response.tool_calls or []:
            arguments = call.function.get("arguments")
            if isinstance(arguments, str):
                return arguments
        return response.content

    @staticmethod
    def _error_evidence(stage: str, error: LLMOutputError) -> dict[str, Any]:
        return {
            "stage": stage,
            "status": "failed",
            "error_type": type(error.__cause__ or error).__name__,
            "error": str(error),
            "model_id": error.model_id,
            "raw_output": error.raw_output,
        }

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

DETERMINISTIC_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "non_empty", "contains", "language_zh", "max_length",
                "observation_contains", "observation_field_non_empty",
                "artifact_exists", "evidence_exists",
                "references_evidence", "semantic",
            ],
        },
        "value": {},
        "evidence_type": {"type": "string"},
        "field": {"type": "string"},
    },
    "required": ["type"],
}


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
                            "check": DETERMINISTIC_CHECK_SCHEMA,
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
                "verdict": {"type": "string", "enum": ["feasible", "needs_observation", "not_feasible"]},
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
        "status": {"type": "string", "enum": ["accepted", "needs_observation", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "observation_request": {"type": "array", "items": {"type": "string"}},
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
                            "executor": {"type": "string", "enum": ["agent_response", "external"]},
                            "assertions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string"},
                                        "required": {"type": "boolean"},
                                        "check": DETERMINISTIC_CHECK_SCHEMA,
                                    },
                                    "required": ["description", "required"],
                                },
                            },
                            "max_attempts": {"type": "integer", "minimum": 1},
                        },
                        "required": ["action_id", "objective", "depends_on", "instruction", "executor", "assertions", "max_attempts"],
                    },
                },
            },
            "required": ["actions"],
        },
    },
    "required": ["status", "reason"],
}

ACTION_EXECUTION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "result": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "minLength": 1},
                "summary": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    "required": ["status", "reason"],
}

CHECK_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "checks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "assertion_id": {"type": "string"},
                    "description": {"type": "string"},
                    "required": {"type": "boolean"},
                    "passed": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["description", "required", "passed", "evidence"],
            },
        },
    },
    "required": ["status", "reason"],
}


# Backward-compatible import for code written during the first POC iteration.
AndybotLLMAdapter = TOEDACLLMAdapter
