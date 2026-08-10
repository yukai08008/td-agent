from __future__ import annotations

from collections.abc import Callable
import json
import re
import time
from typing import Any

from .access_log import AccessLogger
from .control_plane import DeterministicControlPlane
from .events import ConversationEvent
from .input_router import ConversationIntent, route_input
from .llm_adapter import (
    ACTION_EXECUTION_TOOL_SCHEMA,
    CHECK_TOOL_SCHEMA,
    ESTIMATE_TOOL_SCHEMA,
    OBSERVATION_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    TARGET_TOOL_SCHEMA,
    LLMOutputError,
)
from .service import TDService
from .states import TDState, TERMINAL_STATES
from .storage import TDRepository, short_id
from .validation import ValidationError


class ConversationController:
    """Natural-language controller for one active TD in a User Thread."""

    def __init__(
        self,
        repository: TDRepository,
        adapter: Any,
        service: TDService,
        *,
        read_only: bool = False,
    ):
        self.repository = repository
        self.adapter = adapter
        self.service = service
        self.read_only = read_only
        self.access_log = AccessLogger(repository.access_log_dir)
        self._progress_sink: Callable[[ConversationEvent], None] | None = None

    @classmethod
    def open(
        cls,
        repository: TDRepository,
        adapter: Any,
        user_thread_id: str,
        retry_budget: int = 3,
        *,
        session_id: str | None = None,
        new_session: bool = False,
    ) -> "ConversationController":
        active_td_id = repository.active_td_id(user_thread_id)
        read_only = False
        if active_td_id:
            service = TDService.load(repository, user_thread_id, active_td_id)
            if new_session:
                repository.start_new_session(service.context)
            else:
                thread_info = repository.thread_info(user_thread_id) or {}
                selected_session = session_id or thread_info.get("latest_session_id") or service.context["session_id"]
                if service.state in TERMINAL_STATES:
                    repository.attach_session(service.context, selected_session, read_only=True)
                    read_only = True
                else:
                    repository.attach_session(service.context, selected_session)
        else:
            if session_id:
                raise ValueError(f"Cannot attach Session {session_id}: User Thread has no TD")
            service = TDService.create(repository, user_thread_id, retry_budget)
        controller = cls(repository, adapter, service, read_only=read_only)
        if not read_only and service.state == TDState.WAITING_HUMAN:
            control = service.context.get("control", {})
            if controller._is_internal_replan_issue(
                str(control.get("return_to") or ""),
                str(control.get("human_question") or ""),
                str(control.get("waiting_reason") or ""),
            ):
                service.user_replan(
                    "启动时检测到遗留的内部 Plan/断言确认循环，自动解除等待并重新规划。"
                )
        return controller

    def detach_connection(self) -> None:
        if not self.read_only:
            self.repository.detach_session(self.service.context)

    def close_session(self, status: str | None = None) -> None:
        """Compatibility wrapper: default close now detaches instead of ending the Session."""
        if status:
            self.repository.end_session(self.service.context, status)
        else:
            self.detach_connection()

    async def handle_user_message(self, content: str) -> list[str]:
        """Backward-compatible text-only view of conversation events."""
        events = await self.handle_user_events(content)
        return [event.message for event in events if event.visible and event.message]

    async def handle_user_events(
        self,
        content: str,
        on_event: Callable[[ConversationEvent], None] | None = None,
    ) -> list[ConversationEvent]:
        if not content.strip():
            return []
        started = time.monotonic()
        request_id = short_id("req")
        context = self.service.context
        context["current_request_id"] = request_id
        intent = route_input(content)
        user_type = (
            "user_answer"
            if self.service.state == TDState.WAITING_HUMAN and intent == "task_input"
            else "user_ask"
        )
        self.repository.record_message(
            context,
            "user",
            content,
            {"request_id": request_id, "access_type": user_type},
        )
        self.access_log.record(
            request_id=request_id,
            user_thread_id=context["user_thread_id"],
            session_id=context["session_id"],
            access_type=user_type,
            duration_ms=0,
        )

        if intent != "task_input":
            reply = self._conversation_control_reply(intent)
            self._record_assistant(reply, {
                "route": "conversation_control",
                "intent": intent,
                "request_id": request_id,
            })
            event = ConversationEvent("assistant_message", reply, data={"route": intent})
            self._record_agent_access(request_id, "agent_answer", started)
            if on_event:
                on_event(event)
            return [event]

        if self.service.state in TERMINAL_STATES:
            reply = (
                f"这个需求已经结束（{self.service.state.value}）。"
                "新需求必须创建新的 User Thread，请退出后运行 `toe-dac new`。"
            )
            self._record_assistant(reply, {"boundary": "thread_terminal"})
            event = ConversationEvent("terminal", reply, data={"state": self.service.state.value})
            self._record_agent_access(request_id, "agent_answer", started)
            if on_event:
                on_event(event)
            return [event]

        if self.service.state == TDState.WAITING_HUMAN:
            pending_question = str(context.get("control", {}).get("human_question") or "")
            pending_reason = str(context.get("control", {}).get("waiting_reason") or "")
            pending_phase = str(context.get("control", {}).get("return_to") or "")
            internal_replan = self._is_internal_replan_issue(
                pending_phase, pending_question, pending_reason,
            )
            self.service.human_reply({"text": content})
            if internal_replan and self.service.state in {
                TDState.DECIDING, TDState.ACTING,
                TDState.CHECKING_ACTION, TDState.CHECKING_TARGET,
            }:
                self.service.user_replan(
                    "用户答复已消费；原问题属于内部 Plan/断言冲突，自动重新规划，"
                    "不得再次请求相同确认。"
                )
        if self.service.state == TDState.IDLE:
            self.service.start()

        events: list[ConversationEvent] = []

        def emit(event: ConversationEvent) -> None:
            events.append(event)
            if event.visible:
                access_type = "agent_ask" if event.type == "human_question" else "agent_answer"
                self._record_agent_access(request_id, access_type, started)
            if on_event:
                on_event(event)

        async def run_phase(
            phase: str,
            runner: Callable[[], Any],
        ) -> ConversationEvent:
            budget = int(self.service.context["recovery"].get("retry_budget", 3))
            while True:
                emit(self._phase_started(phase))
                phase_started = time.monotonic()
                try:
                    result = await runner()
                except LLMOutputError as exc:
                    duration_ms = round((time.monotonic() - phase_started) * 1000, 1)
                    self.repository.record_operation(
                        self.service.context, "phase_run", "failed", phase=phase,
                        error_type=type(exc).__name__, error=str(exc),
                        data={"duration_ms": duration_ms, "state_after": self.service.state.value},
                    )
                    operation_id = self._record_llm_output_failure(phase, exc)
                    used = self.service.runtime_retry_count(phase)
                    if used >= budget:
                        self.service.fail_runtime_terminal(
                            phase, "runtime_retry_budget_exhausted", str(exc),
                        )
                        message = (
                            f"{phase} 自动恢复已用尽 {used}/{budget} 次，TD 已明确失败。"
                            "失败原因和尝试轨迹已写入 Artifact。"
                        )
                        self._record_assistant(message, {
                            "phase": phase, "operation_id": operation_id,
                            "retry_count": used, "retry_budget": budget,
                        })
                        return ConversationEvent(
                            "terminal", message, phase=phase,
                            data={"state": "failed", "operation_id": operation_id},
                        )
                    used = self.service.register_runtime_retry(phase)
                    message = (
                        f"{phase} 本次调用失败（{duration_ms / 1000:.1f}s），"
                        f"正在自动换路径重试 {used}/{budget}。"
                    )
                    self._record_assistant(message, {
                        "phase": phase, "operation_id": operation_id,
                        "retry_count": used, "retry_budget": budget,
                    })
                    emit(ConversationEvent(
                        "automatic_retry", message, phase=phase,
                        data={"operation_id": operation_id, "retry_count": used,
                              "retry_budget": budget, "duration_ms": duration_ms},
                    ))
                    continue
                except Exception as exc:
                    duration_ms = round((time.monotonic() - phase_started) * 1000, 1)
                    self.repository.record_operation(
                        self.service.context, "phase_run", "failed", phase=phase,
                        error_type=type(exc).__name__, error=str(exc),
                        data={"duration_ms": duration_ms, "state_after": self.service.state.value},
                    )
                    raise
                failure = self.service.context.get("recovery", {}).get("last_failure") or {}
                if (
                    self.service.context.get("recovery", {}).get("active_experience_id")
                    and failure.get("phase") == phase
                    and failure.get("cause") == "skill_or_model_runtime_failed"
                ):
                    self.service.finish_runtime_treatment(True, {"automatic_retry_succeeded": True})
                break
            data = dict(result.data)
            duration_ms = round((time.monotonic() - phase_started) * 1000, 1)
            data["duration_ms"] = duration_ms
            self.repository.record_operation(
                self.service.context, "phase_run", "succeeded", phase=phase,
                data={
                    "duration_ms": duration_ms,
                    "state_after": self.service.state.value,
                    "event_type": result.type,
                },
            )
            return ConversationEvent(
                result.type, result.message, phase=result.phase,
                data=data, visible=result.visible,
            )

        previous_progress_sink = self._progress_sink
        self._progress_sink = on_event
        try:
            for _ in range(32):
                state = self.service.state
                if state == TDState.TARGETING:
                    event = await run_phase("target", self._run_target)
                elif state == TDState.OBSERVING:
                    event = await run_phase("observe", self._run_observe)
                elif state == TDState.ESTIMATING:
                    event = await run_phase("estimate", self._run_estimate)
                elif state == TDState.DECIDING:
                    event = await run_phase("decide", self._run_decide)
                elif state == TDState.ACTING:
                    action = self.service.current_action()
                    if self._action_executor(action) != "agent_response":
                        reply = (
                            "当前 Action 需要外部受限 Executor，尚未获得可自动执行的授权或能力："
                            f"{action['objective']}"
                        )
                        self._record_assistant(reply, {"boundary": "executor", "action": action})
                        emit(ConversationEvent("executor_boundary", reply, phase="act"))
                        break
                    event = await run_phase("act", self._run_act)
                elif state == TDState.CHECKING_ACTION:
                    event = await run_phase("action_check", self._run_action_check)
                elif state == TDState.CHECKING_TARGET:
                    event = await run_phase("target_check", self._run_target_check)
                elif state == TDState.WAITING_HUMAN:
                    break
                elif state == TDState.RECOVERING:
                    event = self._auto_recover()
                elif state == TDState.PAUSED:
                    reply = "TD 已暂停，使用 /resume 后继续。"
                    self._record_assistant(reply, {"boundary": "paused"})
                    emit(ConversationEvent("paused", reply))
                    break
                else:
                    reply = f"TD 已进入终态：{state.value}。下一条新需求会在同一 User Thread 下创建新 TD。"
                    self._record_assistant(reply, {"boundary": "terminal"})
                    emit(ConversationEvent("terminal", reply, data={"state": state.value}))
                    break
                emit(event)
                if self.service.state in TERMINAL_STATES | {TDState.WAITING_HUMAN}:
                    break
        except Exception:
            self._record_agent_access(request_id, "agent_answer", started, status="error")
            raise
        finally:
            self._progress_sink = previous_progress_sink
        return events

    def _record_llm_output_failure(self, phase: str, error: LLMOutputError) -> str:
        operation_id = self.repository.record_operation(
            self.service.context,
            "generate_structured",
            "failed",
            phase=phase,
            error_type=type(error).__name__,
            error=str(error),
            data={"model_id": error.model_id},
            evidence={"attempts": error.attempts, "raw_output": error.raw_output},
        )
        self.service.begin_runtime_treatment(
            phase,
            "skill_or_model_runtime_failed",
            str(error),
            strategy="automatic_path_retry",
            details={"operation_id": operation_id},
        )
        return operation_id

    def _auto_recover(self) -> ConversationEvent:
        failure = self.service.context.get("recovery", {}).get("last_failure") or {}
        phase = str(failure.get("phase", "recover"))
        cause = str(failure.get("cause", "unknown_failure"))
        if phase == "target":
            decision = "retry_targeting"
        elif phase == "action":
            decision = "retry_action"
        elif phase == "check":
            decision = "replan"
        else:
            decision = "reobserve"
        try:
            self.service.recover(decision, reason=f"automatic recovery after {cause}")
            message = f"Recovering 自动选择 {decision}，继续运行。"
            return ConversationEvent(
                "automatic_recovery", message, phase="recover",
                data={"decision": decision, "cause": cause},
            )
        except ValidationError:
            self.service.recover("give_up", reason=f"automatic recovery exhausted: {cause}")
            message = "自动恢复预算已用尽，TD 已明确失败。"
            return ConversationEvent(
                "terminal", message, phase="recover", data={"state": "failed"},
            )

    def _emit_progress(self, progress: dict[str, Any]) -> None:
        if not self._progress_sink:
            return
        kind = str(progress.get("type", "progress"))
        if kind == "model_call_started":
            message = "模型调用开始"
        elif kind == "model_call_completed":
            message = f"模型调用完成，用时 {float(progress.get('duration_ms', 0)) / 1000:.1f}s"
        elif kind == "model_call_failed":
            message = (
                f"模型调用失败，用时 {float(progress.get('duration_ms', 0)) / 1000:.1f}s："
                f"{progress.get('error_type', 'runtime error')}"
            )
        elif kind == "skill_loaded":
            message = f"技能已加载：{', '.join(progress.get('skills', []))}"
        elif kind == "skill_load_failed":
            message = f"技能加载失败：{progress.get('error', 'unknown error')}"
        elif kind == "skill_tool_started":
            value = progress.get("input", {})
            query = str(value.get("query", ""))[:80] if isinstance(value, dict) else ""
            message = (
                f"{progress.get('tool')} 调用 {progress.get('call_number')}/{progress.get('budget')}"
                + (f"：{query}" if query else "")
            )
        elif kind == "skill_tool_retry":
            message = (
                f"{progress.get('tool')} 第 {progress.get('attempt')} 次失败"
                f"（HTTP {progress.get('status')}），{progress.get('delay_seconds')}s 后重试"
            )
        elif kind in {"skill_tool_completed", "skill_tool_failed"}:
            status = "完成" if kind.endswith("completed") else "失败"
            duration = progress.get("duration_ms")
            duration_text = f"，用时 {float(duration) / 1000:.1f}s" if duration is not None else ""
            count = progress.get("result_count")
            count_text = f"，返回 {count} 条" if count is not None else ""
            message = f"{progress.get('tool')} {status}{duration_text}{count_text}"
        elif kind == "repair_started":
            message = f"结构化输出不合法，开始自动修复：{progress.get('reason')}"
        else:
            message = kind
        self._progress_sink(ConversationEvent(
            "progress", message, phase=str(progress.get("phase", "")) or None,
            data=progress, visible=False,
        ))

    def _conversation_control_reply(self, intent: ConversationIntent) -> str:
        if intent == "status":
            return self._status_reply()
        if intent == "clarify":
            return self.why()
        if intent.startswith("inspect_"):
            return self.inspect(intent.removeprefix("inspect_"))
        if self.service.state == TDState.WAITING_HUMAN:
            return f"你好。{self._clarification_reply()}"
        summary = self._target_summary()
        if summary:
            return f"你好。当前正在继续这个用户线头：{summary}"
        return "你好。请告诉我这个用户线头需要完成的明确需求。"

    def inspect(self, section: str) -> str:
        """Read the persisted TD control plane without advancing the state machine."""
        aliases = {
            "observe": "observation",
            "observations": "observation",
            "decision": "plan",
            "decide": "plan",
            "error": "errors",
            "times": "timing",
        }
        section = aliases.get(section.strip().lower(), section.strip().lower())
        context = self.service.context
        summary = {
            "state": self.service.state.value,
            "target": context.get("target") or {},
            "observation": context.get("observation") or {},
            "estimate": context.get("estimate") or {},
            "plan": context.get("plan") or {},
            "artifacts": context.get("artifacts") or [],
        }
        values: dict[str, Any] = {
            "summary": summary,
            "target": context.get("target") or {},
            "observation": context.get("observation") or {},
            "estimate": context.get("estimate") or {},
            "plan": context.get("plan") or {},
            "action": self._safe_current_action(),
            "artifacts": context.get("artifacts") or [],
            "errors": self._error_snapshot(),
            "timing": self._timing_snapshot(),
        }
        if section not in values:
            raise ValueError(f"unsupported inspection section: {section}")
        value = values[section]
        if not value:
            return f"当前还没有 {section} 数据。"
        return f"当前 {section}：\n\n```json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```"

    def why(self) -> str:
        context = self.service.context
        if self.service.state in TERMINAL_STATES:
            return (
                f"这个需求已经以 `{self.service.state.value}` 结束，当前处于只读浏览模式。\n"
                "可使用 `/show target|observe|estimate|plan|artifacts`、`/history`、"
                "`/evidence` 或 `/artifacts` 查看结果。"
            )
        if self.service.state == TDState.WAITING_HUMAN:
            return self._clarification_reply()
        failure = context.get("recovery", {}).get("last_failure")
        if self.service.state == TDState.RECOVERING and failure:
            return f"当前因异常进入 Recovering：{failure.get('message', failure)}"
        action = self._safe_current_action()
        if self.service.state == TDState.ACTING and action:
            executor = self._action_executor(action)
            if executor == "external":
                return (
                    "当前停在外部 Executor 边界。\n"
                    f"Action：{action.get('objective')}\n"
                    f"原因：executor={executor}，当前 POC 不自动执行外部操作。\n"
                    "可以使用 `/show action` 查看详情，或 `/replan <调整要求>` 重新规划。"
                )
        operations = self.repository.operation_log(context)
        latest = operations[-1] if operations else {}
        return (
            f"当前阶段：{self.service.state.value}。"
            f"最近事件：{latest.get('event') or latest.get('operation') or '无'}。"
            "使用 `/show target|observe|estimate|plan|action|errors` 查看具体上下文。"
        )

    def _safe_current_action(self) -> dict[str, Any]:
        if self.service.state not in {
            TDState.ACTING, TDState.CHECKING_ACTION, TDState.CHECKING_TARGET, TDState.RECOVERING,
        }:
            return {}
        try:
            return self.service.current_action()
        except RuntimeError:
            return {}

    def _error_snapshot(self) -> dict[str, Any]:
        operations = self.repository.operation_log(self.service.context)
        failures = [
            item for item in operations
            if item.get("status") in {"failed", "rejected"}
        ]
        return {
            "last_failure": self.service.context.get("recovery", {}).get("last_failure"),
            "recent_operations": failures[-5:],
        }

    def _timing_snapshot(self) -> dict[str, Any]:
        operations = self.repository.operation_log(self.service.context)
        timed = [
            item for item in operations
            if item.get("data", {}).get("duration_ms") is not None
        ]
        return {"recent_operations": timed[-20:]}

    def resume_hint(self) -> str:
        """Human-facing context shown when a Session resumes an existing Thread."""
        if self.service.state == TDState.WAITING_HUMAN:
            return self._clarification_reply()
        summary = self._target_summary()
        if self.read_only and summary:
            return f"已结束需求：{summary}"
        return f"正在继续已有需求：{summary}" if summary else ""

    def _status_reply(self) -> str:
        summary = self._target_summary() or "目标尚未确定"
        reply = f"当前需求：{summary}\n当前阶段：{self.service.state.value}"
        if self.service.state == TDState.WAITING_HUMAN:
            reply += f"\n\n{self._clarification_reply()}"
        return reply

    def _clarification_reply(self) -> str:
        if self.service.state != TDState.WAITING_HUMAN:
            summary = self._target_summary()
            if summary:
                return f"当前正在处理“{summary}”，阶段是 {self.service.state.value}，暂时不需要你确认。"
            return "当前还没有形成明确目标。你可以直接描述要完成的事情和成功标准。"

        control = self.service.context["control"]
        phase = str(control.get("return_to") or "unknown")
        reason = str(control.get("waiting_reason") or "")
        question = str(control.get("human_question") or "请补充继续执行所需的信息。")
        if self._is_external_data_gap(phase, reason):
            return (
                "当前目标已经明确，但 Observe 阶段缺少可验证的外部数据或数据源。"
                "这不是让你判断事实是否存在。你可以：\n"
                "- 提供可验证的数据或来源；\n"
                "- 使用 /pause 暂停，等接入相应工具后继续；\n"
                "- 使用 /cancel 结束这个需求。"
            )
        return f"当前停在 {phase} 阶段，需要你补充：{question}"

    def _target_summary(self) -> str:
        positive = self.service.context.get("target", {}).get("positive", [])
        return str(positive[0]) if positive else ""

    @staticmethod
    def _is_external_data_gap(phase: str, reason: str) -> bool:
        if phase != "observing":
            return False
        markers = ("外部数据", "数据源", "实时", "联网", "实际天气", "天气数据", "工具")
        return any(marker in reason for marker in markers)

    def _record_agent_access(
        self,
        request_id: str,
        access_type: str,
        started: float,
        *,
        status: str = "ok",
    ) -> None:
        context = self.service.context
        self.access_log.record(
            request_id=request_id,
            user_thread_id=context["user_thread_id"],
            session_id=context["session_id"],
            access_type=access_type,
            duration_ms=(time.monotonic() - started) * 1000,
            status=status,
        )

    async def _run_target(self) -> ConversationEvent:
        phase_context: dict[str, Any] = {
            "target_revisions": self.service.context["target_revisions"],
        }
        for attempt in range(2):
            result = await self._generate(
                "target",
                "submit_target",
                TARGET_TOOL_SCHEMA,
                "只定义清晰、可验证的目标。存在实质歧义时必须请求人类，不得进入后续阶段。"
                "证据由运行时存入 Session 的 canonical evidence directory；"
                "用户未指定其他位置时，不得创造 ./evidence/ 等额外归档目录或复制要求。"
                "能机械验收的 acceptance criterion 必须填写 check；复合条件拆成多个 criterion。"
                "只有确实需要语义判断时才使用 check.type=semantic。",
                phase_context,
            )
            if self._needs_human(result):
                return self._ask_human(result, "target")
            target = self._required_object(result, "target")
            target, changes = self._control_plane().normalize_target(
                target, self._user_request_text(),
            )
            self._record_control_plane_changes("target", changes)
            try:
                self._validate_target_runtime(target)
                self.service.submit_target(target)
            except ValidationError as exc:
                recovery_event = self._recover_invalid_target(exc, target, final_attempt=attempt == 1)
                if recovery_event:
                    return recovery_event
                phase_context["repair_feedback"] = {
                    "instruction": "上一次 Target 未通过本地语义校验。修复所有错误后重新提交。",
                    "errors": exc.errors,
                    "invalid_target": target,
                }
                continue
            text = f"Target 已确定（revision {self.service.context['target']['revision']}），开始 Observe。"
            self._record_assistant(text, {"phase": "target", "structured": target})
            return ConversationEvent("phase_completed", text, phase="target", data={"structured": target})
        raise RuntimeError("unreachable target repair loop")

    def _recover_target_output_error(self, error: LLMOutputError) -> ConversationEvent:
        operation_id = self.repository.record_operation(
            self.service.context,
            "generate_structured",
            "failed",
            phase="target",
            error_type=type(error.__cause__ or error).__name__,
            error=str(error),
            data={"model_id": error.model_id, "repair_attempted": bool(error.attempts)},
            evidence={"attempts": error.attempts or [{
                "stage": "initial",
                "status": "failed",
                "raw_output": error.raw_output,
                "model_id": error.model_id,
            }]},
        )
        self.service.fail_targeting("invalid_model_output", str(error))
        self.service.record_active_treatment(
            "repair_structured_output",
            False,
            {"operation_id": operation_id, "attempt_count": len(error.attempts) or 1},
        )
        message = "Target 结构化输出自动修复失败，已进入 Recovering。"
        self._record_assistant(message, {
            "phase": "target",
            "cause": "invalid_model_output",
            "operation_id": operation_id,
        })
        return ConversationEvent(
            "recovery_required",
            message,
            phase="target",
            data={"operation_id": operation_id, "reason": str(error)},
        )

    def _recover_invalid_target(
        self,
        error: ValidationError,
        invalid_target: dict[str, Any],
        *,
        final_attempt: bool,
    ) -> ConversationEvent | None:
        message = "; ".join(error.errors)
        self.service.fail_targeting("invalid_model_output", message)
        try:
            self.service.recover(
                "retry_targeting",
                reason="repair Target rejected by deterministic validation",
            )
        except ValidationError:
            question = "Target 自动修复失败且重试预算已经用完。请确认是否修改需求或终止任务。"
            self.service.recover(
                "escalate",
                reason=message,
                human_question=question,
            )
            self._record_assistant(question, {
                "phase": "target",
                "reason": message,
                "invalid_target": invalid_target,
            })
            return ConversationEvent("human_question", question, phase="target", data={"reason": message})
        if not final_attempt:
            return None
        question = "模型连续生成了不合法的 Target。请补充明确的成功标准，我会重新生成。"
        self.service.request_human(question, message)
        self._record_assistant(question, {
            "phase": "target",
            "reason": message,
            "invalid_target": invalid_target,
        })
        return ConversationEvent("human_question", question, phase="target", data={"reason": message})

    async def _run_observe(self) -> ConversationEvent:
        phase_context: dict[str, Any] = {
            "target": self.service.context["target"],
            "previous_estimate": self.service.context.get("estimate") or None,
            "user_adjustment": self.service.context.get("control", {}).get("waiting_reason"),
            "evidence_registry": self.service.context.get("evidence_registry", []),
        }
        for attempt in range(2):
            result = await self._generate(
                "observe", "submit_observation", OBSERVATION_TOOL_SCHEMA,
                "只收集对话或工具结果中有来源的事实。不得把推断写成事实；"
                "若技能索引中存在可获取缺失事实的技能，必须先加载并调用，只有技能失败或确实不适用时才能请求人类。"
                "依赖网页可视内容的关键事实应加载 agent-browser，对最终采用的关键页面至少保存一次截图；"
                "纯模型或 HTTP/API 调用没有真实画面，不得伪造截图。",
                phase_context,
            )
            if self._needs_human(result):
                return self._ask_human(result, "observe")
            try:
                observation = self._required_object(result, "observation")
                self.service.submit_observation(observation)
            except ValidationError as exc:
                invalid_observation = result.get("observation")
                operation_id = self._record_semantic_rejection(
                    "observe", invalid_observation if isinstance(invalid_observation, dict) else result, exc,
                )
                self.service.record_semantic_validation_attempt(
                    "observe", str(exc), operation_id=operation_id,
                    error_code=self._semantic_error_code("observe", exc),
                    final_attempt=attempt == 1,
                )
                if attempt == 0:
                    phase_context["repair_feedback"] = {
                        "errors": exc.errors, "invalid_observation": invalid_observation,
                        "instruction": "修复所有字段后重新提交 Observation。",
                    }
                    continue
                return self._semantic_failure_to_human("observe", exc)
            text = f"Observe 完成：记录 {len(observation['facts'])} 条事实，开始 Estimate。"
            self._record_assistant(text, {"phase": "observe", "structured": observation})
            return ConversationEvent("phase_completed", text, phase="observe", data={"structured": observation})
        raise RuntimeError("unreachable Observe repair loop")

    async def _run_estimate(self) -> ConversationEvent:
        phase_context: dict[str, Any] = {
            "target": self.service.context["target"],
            "observation": self.service.context["observation"],
        }
        for attempt in range(2):
            result = await self._generate(
                "estimate", "submit_estimate", ESTIMATE_TOOL_SCHEMA,
                "根据 Target 和 Observation 评估可行性、风险、成本与信息缺口。不得执行任务。"
                "若验收必需事实仍可通过现有 Observe 技能获得，返回 verdict=needs_observation；"
                "非必需缺口不得扩大为新的执行目标。已经位于 canonical evidence directory 的截图"
                "已经完成证据留存，不得把复制到其他目录列为信息缺口或风险。",
                phase_context,
            )
            if self._needs_human(result):
                return self._ask_human(result, "estimate")
            try:
                estimate = self._required_object(result, "estimate")
                estimate, changes = self._control_plane().normalize_estimate(estimate)
                self._record_control_plane_changes("estimate", changes)
            except ValidationError as exc:
                operation_id = self._record_semantic_rejection("estimate", result, exc)
                self.service.record_semantic_validation_attempt(
                    "estimate", str(exc), operation_id=operation_id,
                    error_code=self._semantic_error_code("estimate", exc),
                    final_attempt=attempt == 1,
                )
                if attempt == 0:
                    phase_context["repair_feedback"] = {
                        "errors": exc.errors, "invalid_estimate": result.get("estimate"),
                        "instruction": "Estimate 必须包含 verdict、risks、cost、information_gaps。修复后重新提交。",
                    }
                    continue
                return self._semantic_failure_to_human("estimate", exc)
            if estimate.get("verdict") == "not_feasible":
                reason = result.get("reason") or "Estimate verdict is not_feasible"
                self.service.fail_runtime_terminal("estimate", "not_feasible", reason)
                message = "Estimate 已明确判定目标不可行，TD 以 failed 结束；评估依据已写入 Artifact。"
                self._record_assistant(message, {"phase": "estimate", "reason": reason})
                return ConversationEvent(
                    "terminal", message, phase="estimate",
                    data={"state": "failed", "reason": reason},
                )
            try:
                state = self.service.submit_estimate(estimate)
            except ValidationError as exc:
                operation_id = self._record_semantic_rejection("estimate", estimate, exc)
                self.service.record_semantic_validation_attempt(
                    "estimate", str(exc), operation_id=operation_id,
                    error_code=self._semantic_error_code("estimate", exc),
                    final_attempt=attempt == 1,
                )
                if attempt == 0:
                    no_progress = any("no new facts" in item for item in exc.errors)
                    phase_context["repair_feedback"] = {
                        "errors": exc.errors, "invalid_estimate": estimate,
                        "instruction": (
                            "重复 Observe 已经没有新增事实，不得再次返回 needs_observation。"
                            "请基于现有证据判定 feasible，或明确判定 not_feasible 并请求人类。"
                            if no_progress else
                            "Estimate 必须包含 verdict、risks、cost、information_gaps。修复后重新提交。"
                        ),
                    }
                    continue
                return self._semantic_failure_to_human("estimate", exc)
            if state == TDState.OBSERVING:
                text = "Estimate 发现验收必需的信息缺口，返回 Observe 补充证据。"
                self._record_assistant(text, {"phase": "estimate", "structured": estimate})
                return ConversationEvent("phase_completed", text, phase="estimate", data={"structured": estimate})
            text = "Estimate 判定可行，开始 Decide。"
            self._record_assistant(text, {"phase": "estimate", "structured": estimate})
            return ConversationEvent("phase_completed", text, phase="estimate", data={"structured": estimate})
        raise RuntimeError("unreachable Estimate repair loop")

    async def _run_decide(self) -> ConversationEvent:
        phase_context: dict[str, Any] = {
            "target": self.service.context["target"],
            "observation": self.service.context["observation"],
            "estimate": self.service.context["estimate"],
            "user_adjustment": self.service.context.get("control", {}).get("waiting_reason"),
            "available_executors": ["agent_response", "external_boundary"],
        }
        for attempt in range(2):
            result = await self._generate(
                "decide", "submit_plan", PLAN_TOOL_SCHEMA,
                "生成有依赖、断言和尝试预算的原子 Action 列表。不得执行 Action。"
                "executor=agent_response 可自动执行；executor=external 只用于 Target 明确要求的外部变更，"
                "并会停在授权边界。搜索、访问、读取、抓取等事实收集属于 Observe，不得作为 Action；"
                "需要补充事实时返回 status=needs_observation 和 observation_request。"
                "不得引入 Target 验收标准未要求的新范围。截图位于 canonical evidence directory 时"
                "已经完成留证，不得规划复制、移动或再次归档截图的 Action。"
                "每个 assertion 只表达一个条件；能机械检查时必须填写 check，"
                "只有不可计算的语义质量判断使用 check.type=semantic。",
                phase_context,
            )
            if self._needs_human(result):
                return self._ask_human(result, "decide")
            if result.get("status") == "needs_observation":
                reason = str(result.get("reason", "Decide requires more observation"))
                self.service.user_reobserve(reason)
                text = "Decide 发现计划依赖新的事实，返回 Observe。"
                self._record_assistant(text, {"phase": "decide", "reason": reason})
                return ConversationEvent("phase_completed", text, phase="decide", data={"reason": reason})
            try:
                plan = self._required_object(result, "plan")
                plan, changes = self._control_plane().normalize_plan(
                    plan, self._user_request_text(),
                )
                self._record_control_plane_changes("decide", changes)
                self._validate_plan_runtime(plan)
                self.service.submit_plan(plan)
            except ValidationError as exc:
                invalid_plan = result.get("plan")
                operation_id = self._record_semantic_rejection(
                    "decide", invalid_plan if isinstance(invalid_plan, dict) else result, exc,
                )
                self.service.record_semantic_validation_attempt(
                    "decide", str(exc), operation_id=operation_id,
                    error_code=self._semantic_error_code("decide", exc),
                    final_attempt=attempt == 1,
                )
                if attempt == 0:
                    phase_context["repair_feedback"] = {
                        "errors": exc.errors, "invalid_plan": invalid_plan,
                        "instruction": (
                            "删除不可执行、属于 Observe 或重复搬运既有证据的 Action；"
                            "canonical evidence directory 中的截图可直接用于验收。必要时返回 needs_observation。"
                        ),
                    }
                    continue
                return self._semantic_failure_to_human("decide", exc)
            text = f"Decide 完成：Plan 包含 {len(plan['actions'])} 个 Action。"
            self._record_assistant(text, {"phase": "decide", "structured": plan})
            return ConversationEvent("phase_completed", text, phase="decide", data={"structured": plan})
        raise RuntimeError("unreachable Decide repair loop")

    async def _run_act(self) -> ConversationEvent:
        action = self.service.current_action()
        result = await self._generate(
            "act",
            "submit_action_execution",
            ACTION_EXECUTION_TOOL_SCHEMA,
            "只执行当前 agent_response Action：基于已有 Target、Observation、Estimate 和 Plan 生成面向用户的最终内容。"
            "不得再次规划、搜索或声称执行了外部操作。存在 previous_action_checks 时，必须针对"
            "其中未通过的断言修正上次结果，不得原样重复。",
            {
                "target": self.service.context["target"],
                "observation": self.service.context["observation"],
                "estimate": self.service.context["estimate"],
                "action": action,
                "previous_attempts": [
                    item for item in self.service.context["execution"]["attempts"]
                    if item.get("action_id") == action["action_id"]
                ],
                "previous_action_checks": [
                    item for item in self.service.context["checks"]["action_checks"]
                    if item.get("action_id") == action["action_id"]
                ],
            },
        )
        if self._needs_human(result):
            return self._ask_human(result, "act")
        execution = self._required_object(result, "result")
        content = str(execution.get("content", "")).strip()
        if not content:
            raise ValidationError(["agent_response result.content must be non-empty"])
        artifact_ref = self.repository.write_artifact(
            self.service.context,
            f"{action['action_id']}.md",
            content + "\n",
        )
        self.service.submit_action_result({
            "result": {
                "executor": "agent_response",
                "content": content,
                "summary": execution.get("summary", ""),
            },
            "evidence_refs": [artifact_ref],
        })
        text = f"Act 完成：{action['action_id']} 已产生候选回复，开始 Action Check。"
        return ConversationEvent(
            "phase_completed", text, phase="act",
            data={"action_id": action["action_id"], "artifact_ref": artifact_ref},
        )

    async def _run_action_check(self) -> ConversationEvent:
        action = self.service.current_action()
        attempt = self._latest_action_attempt(action["action_id"])
        deterministic = self._control_plane().check_action(action, attempt)
        result = await self._generate(
            "action_check",
            "submit_action_check",
            CHECK_TOOL_SCHEMA,
            "审查当前 Action 是否真正实现其目标。确定性控制器提供的检查结果是硬事实，"
            "不得改写；你仍须对行动结果的语义正确性、完整性及未覆盖断言作关键判断。"
            "只能根据行动结果和证据判断，没有证据时必须判定失败。",
            {
                "action": action,
                "action_result": attempt.get("result", {}),
                "evidence_refs": attempt.get("evidence_refs", []),
                "deterministic_checks": deterministic.checks,
                "unresolved_assertions": deterministic.unresolved,
            },
        )
        if self._needs_human(result):
            if self._is_internal_check_recovery(result):
                checks = self._merge_checks(deterministic.checks, [{
                    "assertion_id": "model-semantic-review",
                    "description": "模型语义审查发现 Action 仍需修正",
                    "required": True,
                    "passed": False,
                    "evidence": str(result.get("reason") or result.get("question") or "需自动重试"),
                    "decision_source": "model",
                }])
                return self._apply_action_checks(action, checks, decision_source="hybrid")
            return self._ask_human(result, "action_check")
        model_checks = result.get("checks")
        if not isinstance(model_checks, list):
            raise ValidationError(["action_check checks must be a list"])
        checks = self._merge_checks(deterministic.checks, model_checks)
        return self._apply_action_checks(action, checks, decision_source="hybrid")

    def _apply_action_checks(
        self,
        action: dict[str, Any],
        checks: list[dict[str, Any]],
        *,
        decision_source: str,
    ) -> ConversationEvent:
        state = self.service.check_action(checks)
        if state == TDState.RECOVERING:
            try:
                self.service.recover("retry_action", reason="action assertions failed; regenerate response")
                return ConversationEvent(
                    "phase_completed",
                    f"Action Check 未通过，正在按预算重试 {action['action_id']}。",
                    phase="action_check",
                    data={"checks": checks, "retry": True},
                )
            except ValidationError as exc:
                self.service.recover(
                    "escalate",
                    reason=str(exc),
                    human_question="Action 自动重试预算已耗尽。是否修改要求后继续？",
                )
                return ConversationEvent(
                    "human_question",
                    "Action 自动重试预算已耗尽。是否修改要求后继续？",
                    phase="action_check",
                    data={"reason": str(exc), "checks": checks},
                )
        text = f"Action Check 通过：{action['action_id']}。"
        return ConversationEvent(
            "phase_completed", text, phase="action_check",
            data={"checks": checks, "decision_source": decision_source},
        )

    async def _run_target_check(self) -> ConversationEvent:
        deterministic = self._control_plane().check_target()
        result = await self._generate(
            "target_check",
            "submit_target_check",
            CHECK_TOOL_SCHEMA,
            "判断最终结果是否真正满足 Target，并检查 negative 约束。确定性控制器提供的"
            "检查结果是硬事实，不得改写；你仍须审查目标语义、整体覆盖度与未覆盖条件。"
            "只能依据 Observation、行动结果和证据判断。",
            {
                "target": self.service.context["target"],
                "observation": self.service.context["observation"],
                "action_attempts": self.service.context["execution"]["attempts"],
                "artifacts": self.service.context["artifacts"],
                "evidence_registry": self.service.context.get("evidence_registry", []),
                "deterministic_checks": deterministic.checks,
                "unresolved_criteria": deterministic.unresolved,
            },
        )
        if self._needs_human(result):
            if self._is_internal_check_recovery(result):
                checks = self._merge_checks(deterministic.checks, [{
                    "assertion_id": "model-semantic-target-review",
                    "description": "模型语义审查发现 Target 尚未满足",
                    "required": True,
                    "passed": False,
                    "evidence": str(result.get("reason") or result.get("question") or "需自动恢复"),
                    "decision_source": "model",
                }])
                return self._apply_target_checks(checks, decision_source="hybrid")
            return self._ask_human(result, "target_check")
        model_checks = result.get("checks")
        if not isinstance(model_checks, list):
            raise ValidationError(["target_check checks must be a list"])
        checks = self._merge_checks(deterministic.checks, model_checks)
        return self._apply_target_checks(checks, decision_source="hybrid")

    def _apply_target_checks(
        self,
        checks: list[dict[str, Any]],
        *,
        decision_source: str,
    ) -> ConversationEvent:
        state = self.service.check_target(checks)
        if state == TDState.RECOVERING:
            try:
                self.service.recover("replan", reason="target acceptance check failed")
                return ConversationEvent(
                    "phase_completed",
                    "Target Check 未通过，正在按预算重新规划。",
                    phase="target_check",
                    data={"checks": checks, "replan": True},
                )
            except ValidationError as exc:
                self.service.recover(
                    "escalate",
                    reason=str(exc),
                    human_question="Target 自动修复预算已耗尽。是否修改目标后继续？",
                )
                return ConversationEvent(
                    "human_question",
                    "Target 自动修复预算已耗尽。是否修改目标后继续？",
                    phase="target_check",
                    data={"reason": str(exc), "checks": checks},
                )
        content = self._latest_agent_response()
        self._record_assistant(content, {
            "phase": "target_check",
            "target_succeeded": True,
            "checks": checks,
            "artifacts": self.service.context["artifacts"],
        })
        return ConversationEvent(
            "assistant_message", content, phase="target_check",
            data={
                "checks": checks,
                "target_succeeded": True,
                "decision_source": decision_source,
            },
        )

    @staticmethod
    def _action_executor(action: dict[str, Any]) -> str:
        explicit = action.get("executor")
        if explicit in {"agent_response", "external"}:
            return str(explicit)
        text = f"{action.get('objective', '')} {action.get('instruction', '')}"
        response_markers = ("向用户", "回复", "输出", "汇报", "报告", "摘要", "deliver")
        return "agent_response" if any(marker in text for marker in response_markers) else "external"

    def _latest_action_attempt(self, action_id: str) -> dict[str, Any]:
        for attempt in reversed(self.service.context["execution"]["attempts"]):
            if attempt.get("action_id") == action_id:
                return attempt
        raise RuntimeError(f"action attempt not found: {action_id}")

    def _latest_agent_response(self) -> str:
        for attempt in reversed(self.service.context["execution"]["attempts"]):
            result = attempt.get("result", {})
            if result.get("executor") == "agent_response" and str(result.get("content", "")).strip():
                return str(result["content"]).strip()
        raise RuntimeError("completed target has no agent_response artifact")

    @staticmethod
    def _merge_checks(
        deterministic: list[dict[str, Any]],
        model_checks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged = [*deterministic]
        known = {
            str(item.get("assertion_id") or item.get("description", "")).casefold()
            for item in deterministic
        }
        for item in model_checks:
            key = str(item.get("assertion_id") or item.get("description", "")).casefold()
            if key not in known:
                merged.append(item)
                known.add(key)
        return merged

    def _control_plane(self) -> DeterministicControlPlane:
        return DeterministicControlPlane(
            self.repository, self.service.context, self.adapter,
        )

    def _user_request_text(self) -> str:
        history = self.repository.message_history(
            self.service.context["user_thread_id"],
            td_id=self.service.context["td_id"],
            limit=30,
        )
        return "\n".join(
            str(item.get("content", ""))
            for item in history if item.get("role") == "user"
        )

    def _record_control_plane_changes(self, phase: str, changes: list[str]) -> None:
        if not changes:
            return
        self.repository.record_operation(
            self.service.context,
            "deterministic_normalization",
            "succeeded",
            phase=phase,
            data={"changes": changes, "decision_source": "deterministic_control_plane"},
        )

    def _validate_plan_runtime(self, plan: dict[str, Any]) -> None:
        errors: list[str] = []
        acquisition_prefixes = (
            "获取", "查询", "搜索", "访问", "读取", "抓取", "检索",
            "observe ", "search ", "fetch ", "read ", "visit ",
        )
        observation_text = json.dumps(
            self.service.context.get("observation", {}), ensure_ascii=False,
        ).casefold()
        has_screenshot_evidence = any(
            marker in observation_text for marker in ("screenshot", "截图", ".png")
        )
        history = self.repository.message_history(
            self.service.context["user_thread_id"],
            td_id=self.service.context["td_id"],
            limit=10,
        )
        user_text = " ".join(
            str(item.get("content", "")).casefold()
            for item in history if item.get("role") == "user"
        )
        user_requested_relocation = any(
            marker in user_text for marker in ("复制截图", "移动截图", "归档至", "./evidence", " evidence/")
        )
        for action in plan.get("actions", []):
            action_id = action.get("action_id", "unknown")
            executor = action.get("executor")
            objective = str(action.get("objective", "")).casefold()
            action_text = " ".join([
                str(action.get("objective", "")),
                str(action.get("instruction", "")),
                json.dumps(action.get("assertions", []), ensure_ascii=False),
            ]).casefold()
            # agent_response is capability-confined to persisted context and cannot
            # browse or read external state. Only an external action whose objective
            # starts with an acquisition verb is misplaced Observe work.
            if executor == "external" and objective.startswith(acquisition_prefixes):
                errors.append(f"action {action_id} collects facts and belongs to Observe")
            relocation = self._has_positive_screenshot_relocation_intent(action_text)
            noncanonical_path = self._has_positive_noncanonical_path_intent(action_text)
            if (
                has_screenshot_evidence
                and (relocation or noncanonical_path)
                and not user_requested_relocation
            ):
                errors.append(
                    f"action {action_id} redundantly relocates screenshot evidence; "
                    "the Session screenshots directory is already the canonical evidence location"
                )
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def _has_positive_screenshot_relocation_intent(action_text: str) -> bool:
        relocation_markers = ("复制", "移动", "迁移", "归档", "copy ", "move ", "archive ")
        subject_markers = ("截图", "screenshot", ".png")
        for clause in re.split(r"[。；;\n]", action_text.casefold()):
            positions = [clause.find(marker) for marker in relocation_markers if marker in clause]
            if not positions or not any(marker in clause for marker in subject_markers):
                continue
            prefix = clause[:min(positions)]
            if ConversationController._contains_negation(prefix):
                continue
            return True
        return False

    @staticmethod
    def _has_positive_noncanonical_path_intent(action_text: str) -> bool:
        for clause in re.split(r"[。；;\n]", action_text.casefold()):
            positions = [
                clause.find(marker) for marker in ("./evidence", " evidence/")
                if marker in clause
            ]
            if not positions:
                continue
            if ConversationController._contains_negation(clause[:min(positions)]):
                continue
            return True
        return False

    @staticmethod
    def _contains_negation(text: str) -> bool:
        stripped = text.rstrip()
        return stripped.endswith(("不", "not")) or any(marker in text for marker in (
            "不得", "不要", "禁止", "无需", "无须", "不需要", "不能",
            "do not", "don't", "must not", "no need to", "without ",
        ))

    def _validate_target_runtime(self, target: dict[str, Any]) -> None:
        target_text = json.dumps(target, ensure_ascii=False).casefold()
        history = self.repository.message_history(
            self.service.context["user_thread_id"],
            td_id=self.service.context["td_id"],
            limit=10,
        )
        user_text = " ".join(
            str(item.get("content", "")).casefold()
            for item in history if item.get("role") == "user"
        )
        target_has_noncanonical = "./evidence" in target_text or " evidence/" in target_text
        user_requested_it = "./evidence" in user_text or " evidence/" in user_text
        if target_has_noncanonical and not user_requested_it:
            raise ValidationError([
                "Target invents a non-canonical ./evidence directory; screenshots are retained "
                "in the Session canonical evidence directory unless the user explicitly requests another location",
            ])

    def _record_semantic_rejection(
        self,
        phase: str,
        invalid_value: dict[str, Any],
        error: ValidationError,
    ) -> str:
        return self.repository.record_operation(
            self.service.context,
            "semantic_validation",
            "rejected",
            phase=phase,
            error_type="ValidationError",
            error=str(error),
            data={"errors": error.errors},
            evidence={"invalid_value": invalid_value},
        )

    def _semantic_failure_to_human(
        self,
        phase: str,
        error: ValidationError,
    ) -> ConversationEvent:
        self.service.fail_runtime_terminal(
            phase, "semantic_validation_exhausted", str(error),
        )
        message = f"{phase} 自动修复后仍未通过本地校验，TD 已明确失败；轨迹已写入 Artifact。"
        self._record_assistant(message, {"phase": phase, "reason": str(error)})
        return ConversationEvent(
            "terminal", message, phase=phase,
            data={"state": "failed", "reason": str(error), "errors": error.errors},
        )

    async def _generate(
        self,
        phase: str,
        tool_name: str,
        schema: dict[str, Any],
        phase_rule: str,
        phase_context: dict[str, Any],
    ) -> dict[str, Any]:
        control_plane = self._control_plane()
        evidence_directory = control_plane.evidence_directory
        configure_evidence = getattr(self.adapter, "configure_evidence", None)
        if callable(configure_evidence):
            configure_evidence(
                evidence_directory,
                self.service.context["session_id"],
            )
        history = self.repository.message_history(
            self.service.context["user_thread_id"],
            td_id=self.service.context["td_id"],
            limit=30,
        )
        generate_started = time.monotonic()
        result = await self.adapter.generate_structured(
            phase=phase,
            system_prompt=(
                f"你是 TOE-DAC 的 {phase.upper()} 决策器。{phase_rule}"
                "运行时已经规定：网页截图及同类原始证据保存在 Session canonical evidence directory；"
                "该位置本身就是正式证据位置，无需复制到 ./evidence/ 或 Artifact 目录。"
                "你可以先调用 load_skill 和加载后开放的技能工具获取完成本阶段所需的信息；"
                "若 payload 提供 experience_candidates，必须基于当前事实独立判断，"
                "通过 experience_decisions 明确 adopt 或 reject；不得因历史经验而跳过当前证据。"
                "取得结果后必须调用指定的阶段提交工具。模型不能直接改变状态。"
            ),
            payload={
                "current_state": self.service.state.value,
                "conversation": [{"role": item["role"], "content": item["content"]} for item in history],
                "phase_context": phase_context,
                "storage_contract": control_plane.storage_contract(),
                "experience_candidates": self.service.context["recovery"].get(
                    "experience_candidates", [],
                ),
                "retry_budget": self.service.context["recovery"]["retry_budget"],
            },
            tool_name=tool_name,
            schema=schema,
            progress_callback=self._emit_progress,
        )
        generate_duration_ms = round((time.monotonic() - generate_started) * 1000, 1)
        failed_skill_events: list[dict[str, Any]] = []
        for skill_event in result.skill_events:
            failed = skill_event.get("status") == "failed"
            self.repository.record_operation(
                self.service.context,
                "skill_tool",
                "failed" if failed else "succeeded",
                phase=phase,
                error_type=str(skill_event.get("error_type", "")) or None,
                error=str(skill_event.get("error", "")) or None,
                data={
                    key: value for key, value in skill_event.items()
                    if key not in {"attempts", "error", "error_type", "evidence"}
                },
                evidence={
                    "attempts": skill_event.get("attempts", []),
                    "tool_evidence": skill_event.get("evidence"),
                },
            )
            if failed:
                failed_skill_events.append(skill_event)
        evidence_records = control_plane.evidence_records_from_tool_events(result.skill_events)
        if evidence_records:
            self.service.register_evidence(evidence_records)
        experience_decisions = result.data.get("experience_decisions", [])
        if isinstance(experience_decisions, list) and experience_decisions:
            self.service.apply_experience_decisions(experience_decisions)
        if failed_skill_events:
            summary = "; ".join(str(item.get("error", "skill failed")) for item in failed_skill_events)
            self.service.begin_runtime_treatment(
                phase,
                "skill_execution_failed",
                summary,
                strategy="agent_fallback_or_human_interrupt",
                details={"failures": failed_skill_events},
            )
            if not self._needs_human(result.data):
                self.service.finish_runtime_treatment(True, {"fallback_completed": True})
        else:
            failure = self.service.context.get("recovery", {}).get("last_failure") or {}
            if (
                self.service.context.get("recovery", {}).get("active_experience_id")
                and str(failure.get("cause", "")).startswith("skill_")
            ):
                self.service.finish_runtime_treatment(True, {"subsequent_attempt_succeeded": True})
        operation_id = self.repository.record_operation(
            self.service.context,
            "generate_structured",
            "recovered" if result.repaired else "succeeded",
            phase=phase,
            data={
                "model_id": result.model_id,
                "repair_attempted": result.repaired,
                "duration_ms": generate_duration_ms,
                "usage": result.usage,
            },
            evidence={"attempts": result.repair_evidence} if result.repaired else None,
        )
        if result.repaired:
            self.service.record_resolved_exception(
                phase,
                "invalid_model_output",
                "模型结构化输出解析失败，自动修复后成功",
                strategy="repair_structured_output",
                details={"operation_id": operation_id, "model_id": result.model_id},
            )
        return result.data

    @staticmethod
    def _semantic_error_code(phase: str, error: ValidationError) -> str:
        message = str(error).casefold()
        if "redundantly relocates screenshot evidence" in message:
            return "plan.screenshot_relocation_conflict"
        if "actions must be a non-empty list" in message:
            return "plan.empty_after_normalization"
        if "no new facts" in message or "re-observation produced no new facts" in message:
            return "estimate.no_observation_progress"
        if "must be a non-empty list" in message:
            return f"{phase}.required_list_empty"
        return f"{phase}.semantic_validation"

    def _ask_human(self, result: dict[str, Any], phase: str) -> ConversationEvent:
        question = str(result.get("question", "")).strip()
        reason = str(result.get("reason", "")).strip()
        if not question or not reason:
            raise ValidationError(["needs_human requires question and reason"])
        original_question = question
        state_phase = {
            "target": "targeting",
            "observe": "observing",
            "estimate": "estimating",
            "decide": "deciding",
            "act": "acting",
            "action_check": "checking_action",
            "target_check": "checking_target",
        }.get(phase, phase)
        if self._is_internal_replan_issue(state_phase, question, reason):
            self.service.user_replan(
                "模型试图把内部 Plan/断言冲突转交给用户。删除无效 Action 或断言后自动重新规划。"
            )
            message = "检测到内部 Plan/断言冲突，已自动返回 Decide；无需用户确认。"
            self._record_assistant(message, {
                "phase": phase,
                "reason": reason,
                "suppressed_question": original_question,
                "automatic_replan": True,
            })
            return ConversationEvent(
                "automatic_recovery", message, phase=phase,
                data={"reason": reason, "decision": "replan"},
            )
        if self._is_external_data_gap(state_phase, reason):
            question = (
                "当前缺少完成 Observe 所需的外部数据或数据源。"
                "请提供可验证的数据/来源；如果目前无法提供，可使用 /pause 或 /cancel。"
            )
        self.service.request_human(question, reason)
        self._record_assistant(question, {
            "phase": self.service.context["control"]["return_to"],
            "reason": reason,
            "original_question": original_question,
        })
        return ConversationEvent("human_question", question, phase=phase, data={"reason": reason})

    @staticmethod
    def _is_internal_replan_issue(phase: str, question: str, reason: str) -> bool:
        if phase not in {"deciding", "acting", "checking_action", "checking_target",
                         "decide", "act", "action_check", "target_check"}:
            return False
        text = f"{question} {reason}".casefold()
        screenshot_location_conflict = (
            any(marker in text for marker in ("./evidence", "archive_screenshot", "证据位置"))
            and any(marker in text for marker in ("trace", "screenshots", "截图"))
        )
        plan_revision_request = (
            any(marker in text for marker in ("修订", "修改", "更新"))
            and any(marker in text for marker in ("action", "plan", "断言"))
        )
        return screenshot_location_conflict or plan_revision_request

    @staticmethod
    def _is_internal_check_recovery(result: dict[str, Any]) -> bool:
        text = f"{result.get('question', '')} {result.get('reason', '')}".casefold()
        external_authority = (
            "凭证", "密码", "api key", "付费", "购买", "写入远端", "删除", "外部系统授权",
        )
        if any(marker in text for marker in external_authority):
            return False
        internal_markers = (
            "重试", "重新执行", "重新规划", "剩余预算", "验收标准", "assertion",
            "criterion", "结构化字段", "action 未通过", "target 检查",
        )
        return any(marker in text for marker in internal_markers)

    @staticmethod
    def _phase_started(phase: str) -> ConversationEvent:
        return ConversationEvent(
            "phase_started",
            f"正在进行 {phase.title()}…",
            phase=phase,
            visible=False,
        )

    @staticmethod
    def _needs_human(result: dict[str, Any]) -> bool:
        return result.get("status") == "needs_human"

    @staticmethod
    def _required_object(result: dict[str, Any], key: str) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise ValidationError([f"accepted output requires {key} object"])
        return value

    def _record_assistant(self, content: str, metadata: dict[str, Any]) -> None:
        self.repository.record_message(self.service.context, "assistant", content, metadata)
