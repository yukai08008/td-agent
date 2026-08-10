from __future__ import annotations

import json
import time
import asyncio
import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llm as local_llm
from .environment import load_environment
from .runtime_content import RuntimeContentLoader, RuntimePromptSnapshot
from .skill_runtime import SkillToolResult, SkillToolRuntime


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
        category: str = "structured_output",
    ):
        super().__init__(message)
        self.raw_output = raw_output
        self.model_id = model_id
        self.attempts = attempts or []
        self.category = category


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
        self._tool_result_cache: dict[str, Any] = {}
        self._configured_session_id: str | None = None
        self._session_control_dir: Path | None = None
        provider = self.llm_module.LLMProvider.OPENAI
        self.client = self.llm_module.LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self.model_config["url"],
            model=self.model_config["id"],
            retry_config=self.llm_module.RetryConfig(
                max_retries=int(self.model_config.get("transportRetries", 2)),
                base_delay=float(self.model_config.get("transportRetryBaseDelay", 0.5)),
                max_delay=float(self.model_config.get("transportRetryMaxDelay", 2.0)),
                backoff_factor=2.0,
            ),
        )

    def configure_evidence(self, screenshot_dir: Path, session_id: str) -> None:
        if self._configured_session_id not in {None, session_id}:
            self._tool_result_cache.clear()
            self._session_control_dir = None
        self._configured_session_id = session_id
        self.skill_runtime.configure_evidence(screenshot_dir, session_id)
        session_dir = screenshot_dir.resolve().parent
        if session_dir.name == "view":
            session_dir = session_dir.parent
        self._session_control_dir = session_dir / "control"
        self._session_control_dir.mkdir(parents=True, exist_ok=True)
        self._session_control_dir.chmod(0o700)
        self._load_persisted_runtime_state()

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
                allow_runtime_tools=False,
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
        allow_runtime_tools: bool = True,
    ):
        phase_tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "提交当前 TOE-DAC 阶段的结构化结果",
                "parameters": schema,
            },
        }
        active_names = {skill.name for skill in self.runtime_snapshot.skills}
        available_names = [
            entry.name for entry in self.runtime_snapshot.available_skills
            if entry.name not in active_names
            and (not entry.phases or not phase or phase in entry.phases)
        ] if allow_runtime_tools else []
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
        max_rounds = 8 if allow_runtime_tools else 1
        for _ in range(max_rounds):
            messages = [
                self.llm_module.Message(
                    role=self.llm_module.MessageRole.SYSTEM,
                    content=self.runtime_snapshot.render(system_prompt, phase=phase),
                ),
                *transcript,
            ]
            tools = [phase_tool]
            if available_names:
                tools.append(load_tool)
            active_names = {skill.name for skill in self.runtime_snapshot.skills}
            configure_skills = getattr(self.skill_runtime, "configure_skills", None)
            if configure_skills:
                configure_skills(self.runtime_snapshot.skills)
            if allow_runtime_tools:
                tools.extend(self.skill_runtime.tool_definitions(active_names, phase))
            model_started = time.monotonic()
            if progress_callback:
                progress_callback({"type": "model_call_started", "phase": phase})
            previous_retry_callback = getattr(self.client, "retry_callback", None)
            if progress_callback:
                self.client.retry_callback = lambda info: progress_callback({
                    "type": "model_transport_retry",
                    "phase": phase,
                    **info,
                })
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
                transport_attempts = getattr(exc, "attempts", None)
                attempts = [{
                    "stage": "model_transport" if transport_attempts else "model_call",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **({"transport_attempts": transport_attempts} if transport_attempts else {}),
                }]
                raise LLMOutputError(
                    f"model runtime failed: {type(exc).__name__}: {exc}",
                    model_id=str(self.model_config.get("id", "")) or None,
                    attempts=attempts,
                    category="model_transport" if transport_attempts else "model_runtime",
                ) from exc
            finally:
                self.client.retry_callback = previous_retry_callback
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
                    loaded_now = set(output.get("loaded", [])) | set(output.get("already_loaded", []))
                    available_names[:] = [item for item in available_names if item not in loaded_now]
                    if progress_callback:
                        progress_callback({
                            "type": "skill_loaded" if output.get("ok") else "skill_load_failed",
                            "phase": phase,
                            "skills": arguments.get("names", []),
                            "error": output.get("error"),
                        })
                else:
                    budget_key = name
                    if name == "run_skill_script":
                        script_arguments = arguments.get("arguments", [])
                        operation = (
                            str(script_arguments[0])
                            if isinstance(script_arguments, list) and script_arguments
                            else ""
                        )
                        job_id = ""
                        if isinstance(script_arguments, list) and "--job-id" in script_arguments:
                            job_index = script_arguments.index("--job-id") + 1
                            if job_index < len(script_arguments):
                                job_id = str(script_arguments[job_index])
                        budget_key = ":".join((
                            name,
                            str(arguments.get("skill_name", "")),
                            str(arguments.get("script", "")),
                            operation,
                            job_id,
                        ))
                    skill_call_counts[budget_key] = skill_call_counts.get(budget_key, 0) + 1
                    call_number = skill_call_counts[budget_key]
                    if call_number > 3:
                        output = {
                            "ok": False,
                            "error": f"{budget_key} exceeded the per-phase call budget of 3",
                            "instruction": "Use collected evidence or ask the human; do not retry.",
                        }
                        self._last_skill_events.append({
                            "skill": self._skill_name_for_tool(name, arguments),
                            "tool": name,
                            "status": "failed",
                            "error_type": "SkillBudgetExceeded",
                            "error": output["error"],
                            "attempt_count": call_number,
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
                                "input": self._safe_progress_input(name, arguments),
                            })
                        cache_key = self._tool_cache_key(name, arguments)
                        cacheable_phase = phase in {"observe", "act"}
                        cached = self._tool_result_cache.get(cache_key) if cacheable_phase else None
                        if cached is not None:
                            result = self._reused_tool_result(cached, cache_key)
                        else:
                            result = await self.skill_runtime.execute(
                                name,
                                arguments,
                                progress_callback=progress_callback,
                            )
                            result = await self._settle_observe_job(
                                result,
                                arguments,
                                phase=phase,
                                progress_callback=progress_callback,
                            )
                        output = result.output
                        event = copy.deepcopy(result.event)
                        event["raw_input"] = copy.deepcopy(arguments)
                        event["raw_output"] = copy.deepcopy(output)
                        if name == "run_skill_script":
                            event["evidence_role"] = str(
                                arguments.get("evidence_role")
                                or ("observation" if phase == "observe" else "result")
                            )
                        result = SkillToolResult(output, event)
                        if cacheable_phase and cached is None and result.output.get("ok"):
                            self._tool_result_cache[cache_key] = result
                            self._persist_tool_checkpoint(cache_key, result)
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
            f"progressive skill/tool loop exceeded {max_rounds} rounds",
            attempts=list(self._last_skill_events),
        )

    @staticmethod
    def _tool_cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _load_persisted_runtime_state(self) -> None:
        if getattr(self, "_session_control_dir", None) is None:
            return
        skills_path = self._session_control_dir / "loaded-skills.json"
        try:
            skills_value = json.loads(skills_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skills_value = []
        if isinstance(skills_value, list):
            known = {entry.name for entry in self.runtime_snapshot.available_skills}
            names = [str(item) for item in skills_value if str(item) in known]
            if names:
                try:
                    self.runtime_snapshot = self.runtime_snapshot.activate(names)
                except (OSError, ValueError):
                    pass
        checkpoint_path = self._session_control_dir / "tool-checkpoints.jsonl"
        try:
            lines = checkpoint_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
                key = str(record["cache_key"])
                output = record["output"]
                event = record["event"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if isinstance(output, dict) and isinstance(event, dict):
                self._tool_result_cache[key] = SkillToolResult(output, event)

    def _persist_loaded_skills(self) -> None:
        if getattr(self, "_session_control_dir", None) is None:
            return
        path = self._session_control_dir / "loaded-skills.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps([skill.name for skill in self.runtime_snapshot.skills], ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _persist_tool_checkpoint(self, cache_key: str, result: SkillToolResult) -> None:
        if getattr(self, "_session_control_dir", None) is None:
            return
        path = self._session_control_dir / "tool-checkpoints.jsonl"
        record = {"cache_key": cache_key, "output": result.output, "event": result.event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _reused_tool_result(result: SkillToolResult, cache_key: str) -> SkillToolResult:
        output = copy.deepcopy(result.output)
        output["checkpoint_reused"] = True
        event = copy.deepcopy(result.event)
        event.update({
            "status": "succeeded",
            "checkpoint_reused": True,
            "cache_key": cache_key,
            "duration_ms": 0,
        })
        return SkillToolResult(output, event)

    @staticmethod
    def _script_payload(result: SkillToolResult) -> dict[str, Any] | None:
        stdout = result.output.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            return None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _with_script_payload(cls, result: SkillToolResult) -> SkillToolResult:
        payload = cls._script_payload(result)
        if payload is None:
            return result
        output = copy.deepcopy(result.output)
        output["result"] = payload
        return SkillToolResult(output, copy.deepcopy(result.event))

    async def _settle_observe_job(
        self,
        result: SkillToolResult,
        arguments: dict[str, Any],
        *,
        phase: str,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> SkillToolResult:
        result = self._with_script_payload(result)
        script_arguments = arguments.get("arguments", [])
        if (
            phase != "observe"
            or arguments.get("skill_name") != "run-cmd"
            or arguments.get("script") != "scripts/run_cmd.py"
            or not isinstance(script_arguments, list)
            or not script_arguments
            or script_arguments[0] != "start"
        ):
            return result
        payload = result.output.get("result")
        if not isinstance(payload, dict) or payload.get("status") != "running" or not payload.get("job_id"):
            return result
        job_id = str(payload["job_id"])
        timeout = arguments.get("timeout", 120)
        wait_seconds = min(float(timeout) if isinstance(timeout, int) else 120.0, 30.0)
        deadline = time.monotonic() + wait_seconds
        polls = 0
        if progress_callback:
            progress_callback({
                "type": "background_job_waiting",
                "phase": phase,
                "job_id": job_id,
                "timeout_seconds": wait_seconds,
            })
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            polls += 1
            status_arguments = {
                "skill_name": "run-cmd",
                "script": "scripts/run_cmd.py",
                "arguments": ["status", "--job-id", job_id],
                "timeout": min(int(wait_seconds) or 1, 30),
            }
            status_result = await self.skill_runtime.execute(
                "run_skill_script",
                status_arguments,
                progress_callback=progress_callback,
            )
            status_result = self._with_script_payload(status_result)
            status_payload = status_result.output.get("result")
            if not isinstance(status_payload, dict):
                return status_result
            if status_payload.get("status") != "running":
                event = copy.deepcopy(status_result.event)
                event.update({
                    "controller_polled": True,
                    "job_id": job_id,
                    "poll_count": polls,
                })
                if progress_callback:
                    progress_callback({
                        "type": "background_job_completed",
                        "phase": phase,
                        "job_id": job_id,
                        "status": status_payload.get("status"),
                        "poll_count": polls,
                    })
                return SkillToolResult(status_result.output, event)
        output = copy.deepcopy(result.output)
        output["controller_wait_timeout"] = True
        output["instruction"] = (
            "The controller stopped waiting for this background job. Do not poll it repeatedly in the model loop; "
            "submit the currently known state or choose a bounded alternative."
        )
        event = copy.deepcopy(result.event)
        event.update({
            "controller_polled": True,
            "job_id": job_id,
            "poll_count": polls,
            "wait_timeout": True,
        })
        return SkillToolResult(output, event)

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
        self._persist_loaded_skills()
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
    def _skill_name_for_tool(tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "run_skill_script":
            return str(arguments.get("skill_name", ""))
        if tool_name.startswith("agent_browser"):
            return "agent-browser"
        return "alex-serp"

    @staticmethod
    def _safe_progress_input(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "run_skill_script":
            return arguments
        script_arguments = arguments.get("arguments", [])
        return {
            "skill_name": arguments.get("skill_name"),
            "script": arguments.get("script"),
            "argument_count": len(script_arguments) if isinstance(script_arguments, list) else None,
            "timeout": arguments.get("timeout", 120),
            "evidence_role": arguments.get("evidence_role", "result"),
        }

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
        tool_calls = getattr(response, "tool_calls", None) or []
        for call in tool_calls:
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
        content_value = getattr(response, "content", None)
        if data is None and content_value:
            content = content_value.strip()
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
        if data is None:
            returned_tools = [str(call.function.get("name", "")) for call in tool_calls]
            if returned_tools:
                message = (
                    f"model omitted required tool {tool_name}; returned tools: "
                    + ", ".join(returned_tools)
                )
            else:
                message = f"model returned an empty response without required tool {tool_name}"
            raise LLMOutputError(
                message,
                raw_output=TOEDACLLMAdapter._response_output(response),
                model_id=getattr(response, "model_id", None),
            )
        if not isinstance(data, dict):
            raise LLMOutputError(
                f"{tool_name} arguments must be an object, got {type(data).__name__}",
                raw_output=TOEDACLLMAdapter._response_output(response),
                model_id=getattr(response, "model_id", None),
            )
        return StructuredLLMResult(
            data=data,
            model_id=response.model_id,
            usage=response.usage or {},
            finish_reason=response.finish_reason,
            raw_content=response.content,
        )

    @staticmethod
    def _response_output(response: Any) -> str | None:
        calls = []
        for call in getattr(response, "tool_calls", None) or []:
            calls.append({
                "name": call.function.get("name"),
                "arguments": call.function.get("arguments"),
            })
        evidence = {
            "content": getattr(response, "content", None),
            "tool_calls": calls,
            "finish_reason": getattr(response, "finish_reason", None),
        }
        if not calls and evidence["content"] is None and evidence["finish_reason"] is None:
            return None
        return json.dumps(evidence, ensure_ascii=False)

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

EXPERIENCE_DECISIONS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "experience_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["adopt", "reject"]},
            "reason": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["experience_id", "decision", "reason", "confidence"],
    },
}


TARGET_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accepted", "needs_human"]},
        "question": {"type": "string"},
        "reason": {"type": "string"},
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
                            "executor": {
                                "type": "string",
                                "enum": ["agent_response", "skill_script", "external"],
                            },
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
                            "changes_state": {
                                "type": "boolean",
                                "description": "Action 是否会改变外部或工作区状态",
                            },
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
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
        "experience_decisions": EXPERIENCE_DECISIONS_SCHEMA,
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
