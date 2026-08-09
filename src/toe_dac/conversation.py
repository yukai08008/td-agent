from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .events import ConversationEvent
from .llm_adapter import (
    ESTIMATE_TOOL_SCHEMA,
    OBSERVATION_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    TARGET_TOOL_SCHEMA,
)
from .service import TDService
from .states import TDState, TERMINAL_STATES
from .storage import TDRepository
from .validation import ValidationError


class ConversationController:
    """Natural-language controller for one active TD in a User Thread."""

    def __init__(self, repository: TDRepository, adapter: Any, service: TDService):
        self.repository = repository
        self.adapter = adapter
        self.service = service

    @classmethod
    def open(
        cls,
        repository: TDRepository,
        adapter: Any,
        user_thread_id: str,
        retry_budget: int = 3,
    ) -> "ConversationController":
        active_td_id = repository.active_td_id(user_thread_id)
        if active_td_id:
            service = TDService.load(repository, user_thread_id, active_td_id)
            repository.start_new_session(service.context)
        else:
            service = TDService.create(repository, user_thread_id, retry_budget)
        return cls(repository, adapter, service)

    def close_session(self, status: str = "completed") -> None:
        self.repository.end_session(self.service.context, status)

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
        if self.service.state in TERMINAL_STATES:
            self.repository.record_message(self.service.context, "user", content)
            reply = (
                f"这个需求已经结束（{self.service.state.value}）。"
                "新需求必须创建新的 User Thread，请退出后运行 `toe-dac new`。"
            )
            self._record_assistant(reply, {"boundary": "thread_terminal"})
            event = ConversationEvent("terminal", reply, data={"state": self.service.state.value})
            if on_event:
                on_event(event)
            return [event]
        self.repository.record_message(self.service.context, "user", content)

        if self.service.state == TDState.WAITING_HUMAN:
            self.service.human_reply({"text": content})
        if self.service.state == TDState.IDLE:
            self.service.start()

        events: list[ConversationEvent] = []

        def emit(event: ConversationEvent) -> None:
            events.append(event)
            if on_event:
                on_event(event)

        for _ in range(6):
            state = self.service.state
            if state == TDState.TARGETING:
                emit(self._phase_started("target"))
                event = await self._run_target()
            elif state == TDState.OBSERVING:
                emit(self._phase_started("observe"))
                event = await self._run_observe()
            elif state == TDState.ESTIMATING:
                emit(self._phase_started("estimate"))
                event = await self._run_estimate()
            elif state == TDState.DECIDING:
                emit(self._phase_started("decide"))
                event = await self._run_decide()
            elif state == TDState.ACTING:
                reply = "计划已经建立，TD 进入 Act。当前需要受限 Executor 执行第一个 Action。"
                self._record_assistant(reply, {"boundary": "executor"})
                emit(ConversationEvent("executor_boundary", reply, phase="act"))
                break
            elif state == TDState.WAITING_HUMAN:
                break
            elif state == TDState.RECOVERING:
                reply = "TD 正在 Recovering，需要选择重试、重新观察、重新规划或请求人工授权。"
                self._record_assistant(reply, {"boundary": "recovery"})
                emit(ConversationEvent("recovery_required", reply, phase="recover"))
                break
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
            if self.service.state == TDState.WAITING_HUMAN:
                break
        return events

    async def _run_target(self) -> ConversationEvent:
        phase_context: dict[str, Any] = {
            "target_revisions": self.service.context["target_revisions"],
        }
        for attempt in range(2):
            result = await self._generate(
                "target",
                "submit_target",
                TARGET_TOOL_SCHEMA,
                "只定义清晰、可验证的目标。存在实质歧义时必须请求人类，不得进入后续阶段。",
                phase_context,
            )
            if self._needs_human(result):
                return self._ask_human(result, "target")
            target = self._required_object(result, "target")
            try:
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
        result = await self._generate(
            "observe",
            "submit_observation",
            OBSERVATION_TOOL_SCHEMA,
            "只收集对话中有来源的事实。不得把推断写成事实；信息不足时请求人类。",
            {"target": self.service.context["target"]},
        )
        if self._needs_human(result):
            return self._ask_human(result, "observe")
        observation = self._required_object(result, "observation")
        self.service.submit_observation(observation)
        text = f"Observe 完成：记录 {len(observation['facts'])} 条事实，开始 Estimate。"
        self._record_assistant(text, {"phase": "observe", "structured": observation})
        return ConversationEvent("phase_completed", text, phase="observe", data={"structured": observation})

    async def _run_estimate(self) -> ConversationEvent:
        result = await self._generate(
            "estimate",
            "submit_estimate",
            ESTIMATE_TOOL_SCHEMA,
            "根据 Target 和 Observation 评估可行性、风险、成本与信息缺口。不得执行任务。",
            {"target": self.service.context["target"], "observation": self.service.context["observation"]},
        )
        if self._needs_human(result):
            return self._ask_human(result, "estimate")
        estimate = self._required_object(result, "estimate")
        if estimate.get("verdict") != "feasible":
            question = result.get("question") or "当前条件下目标不可行。是否修改目标或补充条件？"
            reason = result.get("reason") or "Estimate verdict is not_feasible"
            self.service.request_human(question, reason)
            self._record_assistant(question, {"phase": "estimate", "reason": reason})
            return ConversationEvent("human_question", question, phase="estimate", data={"reason": reason})
        self.service.submit_estimate(estimate)
        text = "Estimate 判定可行，开始 Decide。"
        self._record_assistant(text, {"phase": "estimate", "structured": estimate})
        return ConversationEvent("phase_completed", text, phase="estimate", data={"structured": estimate})

    async def _run_decide(self) -> ConversationEvent:
        result = await self._generate(
            "decide",
            "submit_plan",
            PLAN_TOOL_SCHEMA,
            "生成有依赖、断言和尝试预算的原子 Action 列表。不得执行 Action。",
            {
                "target": self.service.context["target"],
                "observation": self.service.context["observation"],
                "estimate": self.service.context["estimate"],
            },
        )
        if self._needs_human(result):
            return self._ask_human(result, "decide")
        plan = self._required_object(result, "plan")
        self.service.submit_plan(plan)
        text = f"Decide 完成：Plan 包含 {len(plan['actions'])} 个 Action。"
        self._record_assistant(text, {"phase": "decide", "structured": plan})
        return ConversationEvent("phase_completed", text, phase="decide", data={"structured": plan})

    async def _generate(
        self,
        phase: str,
        tool_name: str,
        schema: dict[str, Any],
        phase_rule: str,
        phase_context: dict[str, Any],
    ) -> dict[str, Any]:
        history = self.repository.message_history(
            self.service.context["user_thread_id"],
            td_id=self.service.context["td_id"],
            limit=30,
        )
        result = await self.adapter.generate_structured(
            phase=phase,
            system_prompt=(
                f"你是 TOE-DAC 的 {phase.upper()} 决策器。{phase_rule}"
                "只调用指定提交工具；模型不能直接改变状态。"
            ),
            payload={
                "current_state": self.service.state.value,
                "conversation": [{"role": item["role"], "content": item["content"]} for item in history],
                "phase_context": phase_context,
                "retry_budget": self.service.context["recovery"]["retry_budget"],
            },
            tool_name=tool_name,
            schema=schema,
        )
        return result.data

    def _ask_human(self, result: dict[str, Any], phase: str) -> ConversationEvent:
        question = str(result.get("question", "")).strip()
        reason = str(result.get("reason", "")).strip()
        if not question or not reason:
            raise ValidationError(["needs_human requires question and reason"])
        self.service.request_human(question, reason)
        self._record_assistant(question, {"phase": self.service.context["control"]["return_to"], "reason": reason})
        return ConversationEvent("human_question", question, phase=phase, data={"reason": reason})

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
