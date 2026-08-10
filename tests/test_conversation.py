from __future__ import annotations

import json

import pytest

from toe_dac.conversation import ConversationController
from toe_dac.experience import ExperienceStore
from toe_dac.llm_adapter import LLMOutputError, StructuredLLMResult
from toe_dac.states import TDState
from toe_dac.validation import ValidationError


class FakeStructuredAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        data = dict(self.responses.pop(0))
        skill_events = data.pop("_skill_events", [])
        return StructuredLLMResult(
            data=data,
            model_id="fake-model",
            usage={"input": 1, "output": 1},
            finish_reason="tool_calls",
            raw_content=None,
            skill_events=skill_events,
        )


class BrokenToolArgumentsAdapter:
    async def generate_structured(self, **kwargs):
        parse_error = LLMOutputError(
            "invalid JSON in submit_target tool arguments: "
            "Expecting ',' delimiter: line 1 column 445 (char 444)",
            raw_output='{"status":"accepted" "reason":"missing comma"}',
            model_id="deepseek-v4-flash",
        )
        parse_error.attempts = [
            {"stage": "initial", "status": "failed", "raw_output": "bad-1"},
            {"stage": "repair", "status": "failed", "raw_output": "bad-2"},
        ]
        raise parse_error


class RepairedToolArgumentsAdapter:
    async def generate_structured(self, **kwargs):
        return StructuredLLMResult(
            data={"status": "needs_human", "reason": "ambiguous", "question": "具体目标是什么？"},
            model_id="deepseek-v4-flash",
            usage={"input": 1, "output": 1},
            finish_reason="tool_calls",
            raw_content=None,
            repaired=True,
            repair_evidence=[
                {"stage": "initial", "status": "failed", "raw_output": "bad"},
                {"stage": "repair", "status": "succeeded", "raw_output": "good"},
            ],
        )


class ObserveRuntimeFailureAdapter:
    def __init__(self):
        self.calls = 0

    async def generate_structured(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return StructuredLLMResult(
                data={"status": "accepted", "reason": "明确", "target": _target()},
                model_id="fake-model", usage={}, finish_reason="tool_calls", raw_content=None,
            )
        raise LLMOutputError(
            "progressive skill/tool loop exceeded 8 rounds",
            attempts=[{"skill": "alex-serp", "status": "failed", "error": "SERP unavailable"}],
        )


class FlakyObserveAdapter(FakeStructuredAdapter):
    def __init__(self, responses):
        super().__init__(responses)
        self.observe_failed = False

    async def generate_structured(self, **kwargs):
        if kwargs["phase"] == "observe" and not self.observe_failed:
            self.observe_failed = True
            self.calls.append(kwargs)
            raise LLMOutputError(
                "temporary malformed observe output",
                attempts=[{"stage": "initial", "status": "failed", "raw_output": "bad"}],
            )
        return await super().generate_structured(**kwargs)


class ExhaustedTransportAdapter:
    def __init__(self):
        self.calls = 0

    async def generate_structured(self, **kwargs):
        self.calls += 1
        raise LLMOutputError(
            "model runtime failed: ModelTransportError: transport exhausted",
            model_id="deepseek-v4-flash",
            category="model_transport",
            attempts=[{
                "stage": "model_transport",
                "status": "failed",
                "error_type": "ModelTransportError",
                "transport_attempts": [
                    {"attempt": 1, "error_type": "IncompleteRead"},
                    {"attempt": 2, "error_type": "RemoteDisconnected"},
                ],
            }],
        )


def _target():
    return {
        "positive": ["补充 README"],
        "negative": ["不修改代码"],
        "acceptance_criteria": [{"description": "包含安装和测试说明", "required": True}],
    }


def _observation():
    return {
        "facts": [{"description": "README 只有标题", "source_type": "human_input", "source_ref": None}],
        "unknowns": [],
    }


def _estimate():
    return {"verdict": "feasible", "risks": [], "cost": {"max_actions": 1}, "information_gaps": []}


def _plan():
    return {
        "plan_id": "plan_chat",
        "version": 1,
        "actions": [{
            "action_id": "a_readme",
            "objective": "补充 README",
            "depends_on": [],
            "instruction": "写入安装和测试说明",
            "assertions": [{"description": "章节存在", "required": True}],
            "max_attempts": 1,
        }],
    }


def test_conversation_is_multi_turn_and_persists_across_reattach(repository):
    adapter = FakeStructuredAdapter([
        {"status": "needs_human", "reason": "整理范围不明确", "question": "只改文档还是也改代码？"},
        {"status": "accepted", "reason": "范围明确", "target": _target()},
        {"status": "needs_human", "reason": "缺少当前 README 情况", "question": "README 现在包含哪些内容？"},
        {"status": "accepted", "reason": "事实充分", "observation": _observation()},
        {"status": "accepted", "reason": "可行", "estimate": _estimate()},
        {"status": "accepted", "reason": "计划明确", "plan": _plan()},
    ])
    first = ConversationController.open(repository, adapter, "ut_chat")
    first_session = first.service.context["session_id"]
    replies = __import__("asyncio").run(first.handle_user_message("帮我整理这个项目"))
    assert first.service.state == TDState.WAITING_HUMAN
    assert "只改文档" in replies[-1]
    first.detach_connection()

    second = ConversationController.open(repository, adapter, "ut_chat")
    assert second.service.context["session_id"] == first_session
    replies = __import__("asyncio").run(second.handle_user_message("只补充 README，不修改代码"))
    assert second.service.state == TDState.WAITING_HUMAN
    assert second.service.context["control"]["return_to"] == "observing"
    second.detach_connection()

    third = ConversationController.open(repository, adapter, "ut_chat")
    assert third.service.context["session_id"] == first_session
    replies = __import__("asyncio").run(third.handle_user_message("README 目前只有标题"))
    assert third.service.state == TDState.ACTING
    assert any("Target 已确定" in item for item in replies) is False
    assert any("Plan 包含 1 个 Action" in item for item in replies)
    assert any("需要外部受限 Executor" in item for item in replies)

    history = repository.message_history("ut_chat")
    assert [item["role"] for item in history].count("user") == 3
    assert {item["session_id"] for item in history} == {first_session}
    assert len({item["td_id"] for item in history}) == 1


def test_new_requirement_after_terminal_must_use_new_thread(repository):
    adapter = FakeStructuredAdapter([])
    controller = ConversationController.open(repository, adapter, "ut_long_lived")
    old_td_id = controller.service.context["td_id"]
    controller.service.cancel()
    replies = __import__("asyncio").run(controller.handle_user_message("开始另一个需求"))
    info = repository.thread_info("ut_long_lived")
    assert controller.service.context["td_id"] == old_td_id
    assert info["root_td_id"] == old_td_id
    assert info["td_ids"] == [old_td_id]
    assert controller.service.state == TDState.CANCELLED
    assert "新的 User Thread" in replies[-1]


def test_sessions_belong_to_thread_and_messages_keep_session_identity(repository):
    adapter = FakeStructuredAdapter([
        {"status": "needs_human", "reason": "r1", "question": "q1"},
        {"status": "needs_human", "reason": "r2", "question": "q2"},
    ])
    first = ConversationController.open(repository, adapter, "ut_sessions")
    first_session = first.service.context["session_id"]
    __import__("asyncio").run(first.handle_user_message("task"))
    first.detach_connection()

    resumed = ConversationController.open(repository, adapter, "ut_sessions")
    assert resumed.service.context["session_id"] == first_session
    resumed.detach_connection()

    second = ConversationController.open(repository, adapter, "ut_sessions", new_session=True)
    second_session = second.service.context["session_id"]
    __import__("asyncio").run(second.handle_user_message("补充信息"))

    sessions = repository.list_sessions("ut_sessions")
    assert [item["session_id"] for item in sessions] == [first_session, second_session]
    assert [item["status"] for item in sessions] == ["detached", "active"]
    assert {item["session_id"] for item in repository.message_history("ut_sessions")} == {
        first_session, second_session,
    }
    assert all(
        (
            repository.thread_dir("ut_sessions")
            / "trace" / "sessions" / item["session_id"] / "session.json"
        ).exists()
        for item in sessions
    )


def test_can_reattach_an_explicit_older_session(repository):
    adapter = FakeStructuredAdapter([])
    first = ConversationController.open(repository, adapter, "ut_select_session")
    first_session = first.service.context["session_id"]
    first.detach_connection()
    second = ConversationController.open(repository, adapter, "ut_select_session", new_session=True)
    second_session = second.service.context["session_id"]
    second.detach_connection()

    selected = ConversationController.open(
        repository,
        adapter,
        "ut_select_session",
        session_id=first_session,
    )

    assert selected.service.context["session_id"] == first_session
    assert repository.thread_info("ut_select_session")["latest_session_id"] == first_session
    statuses = {item["session_id"]: item["status"] for item in repository.list_sessions("ut_select_session")}
    assert statuses[first_session] == "active"
    assert statuses[second_session] == "detached"


def test_legacy_completed_session_is_reopened_when_td_is_non_terminal(repository):
    adapter = FakeStructuredAdapter([])
    first = ConversationController.open(repository, adapter, "ut_legacy_session")
    session_id = first.service.context["session_id"]
    repository.end_session(first.service.context, "completed")

    resumed = ConversationController.open(repository, adapter, "ut_legacy_session")

    assert resumed.service.context["session_id"] == session_id
    session = repository.find_session(session_id)
    assert session["status"] == "active"
    assert session["ended_at"] is None
    assert session["legacy_reopened"] is True


def test_detach_ends_session_only_when_td_is_terminal(repository):
    adapter = FakeStructuredAdapter([])
    controller = ConversationController.open(repository, adapter, "ut_terminal_session")
    session_id = controller.service.context["session_id"]
    controller.service.cancel()

    controller.detach_connection()

    session = repository.find_session(session_id)
    assert session["status"] == "cancelled"
    assert session["ended_at"] is not None


def test_controller_emits_ui_neutral_events_in_real_time(repository):
    adapter = FakeStructuredAdapter([
        {"status": "needs_human", "reason": "目标含糊", "question": "成功标准是什么？"},
    ])
    controller = ConversationController.open(repository, adapter, "ut_events")
    observed = []
    events = __import__("asyncio").run(
        controller.handle_user_events("帮我完成任务", observed.append)
    )
    assert [event.type for event in events] == ["phase_started", "human_question"]
    assert observed == events
    assert events[0].visible is False
    assert events[1].phase == "target"


def test_greeting_is_answered_locally_without_advancing_td(repository):
    adapter = FakeStructuredAdapter([])
    controller = ConversationController.open(repository, adapter, "ut_greeting")
    controller.service.start()
    revision = controller.service.context["revision"]

    events = __import__("asyncio").run(controller.handle_user_events("你好！"))

    assert controller.service.state == TDState.TARGETING
    assert controller.service.context["revision"] == revision
    assert adapter.calls == []
    assert [event.type for event in events] == ["assistant_message"]
    assert "明确需求" in events[0].message

    access_log = (repository.root / "logs" / "access.log").read_text(encoding="utf-8")
    assert "user_thread=ut_greeting" in access_log
    assert "type=user_ask" in access_log
    assert "type=agent_answer" in access_log


def test_greeting_does_not_consume_pending_human_question(repository):
    adapter = FakeStructuredAdapter([
        {"status": "needs_human", "reason": "目标不明确", "question": "你希望完成什么？"},
    ])
    controller = ConversationController.open(repository, adapter, "ut_waiting_greeting")
    __import__("asyncio").run(controller.handle_user_events("帮我处理一下"))
    revision = controller.service.context["revision"]

    events = __import__("asyncio").run(controller.handle_user_events("hello"))

    assert controller.service.state == TDState.WAITING_HUMAN
    assert controller.service.context["revision"] == revision
    assert controller.service.context["control"]["human_response"] is None
    assert len(adapter.calls) == 1
    assert events[0].type == "assistant_message"
    assert "需要你补充" in events[0].message


def test_clarification_does_not_consume_human_answer_or_call_model(repository):
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "目标明确", "target": _target()},
        {
            "status": "needs_human",
            "reason": "当前没有外部数据源或可用工具，无法取得实时天气数据。",
            "question": "是否存在可验证的天气事实？",
        },
    ])
    controller = ConversationController.open(repository, adapter, "ut_clarify")
    first_events = __import__("asyncio").run(controller.handle_user_events("查询上海天气"))
    revision = controller.service.context["revision"]

    assert controller.service.state == TDState.WAITING_HUMAN
    assert "请提供可验证的数据/来源" in first_events[-1].message
    assert "是否存在可验证" not in first_events[-1].message

    events = __import__("asyncio").run(controller.handle_user_events("啥意思"))

    assert controller.service.state == TDState.WAITING_HUMAN
    assert controller.service.context["revision"] == revision
    assert controller.service.context["control"]["human_response"] is None
    assert len(adapter.calls) == 2
    assert events[0].data["route"] == "clarify"
    assert "这不是让你判断事实是否存在" in events[0].message
    access_log = (repository.root / "logs" / "access.log").read_text(encoding="utf-8")
    assert access_log.count("type=user_ask") >= 2


def test_natural_language_status_is_local_and_non_mutating(repository):
    adapter = FakeStructuredAdapter([])
    controller = ConversationController.open(repository, adapter, "ut_status")
    revision = controller.service.context["revision"]

    events = __import__("asyncio").run(controller.handle_user_events("现在什么状态？"))

    assert controller.service.context["revision"] == revision
    assert adapter.calls == []
    assert events[0].data["route"] == "status"
    assert "当前阶段：idle" in events[0].message


def test_natural_language_observation_inspection_is_local_and_non_mutating(repository):
    adapter = FakeStructuredAdapter([])
    controller = ConversationController.open(repository, adapter, "ut_inspect_observe")
    controller.service.start()
    controller.service.submit_target(_target())
    controller.service.submit_observation(_observation())
    revision = controller.service.context["revision"]

    events = __import__("asyncio").run(controller.handle_user_events("你 observe 到了什么"))

    assert controller.service.context["revision"] == revision
    assert controller.service.state == TDState.ESTIMATING
    assert adapter.calls == []
    assert events[0].data["route"] == "inspect_observation"
    assert "README 只有标题" in events[0].message


def test_invalid_model_target_is_repaired_and_recorded_as_successful_experience(repository):
    invalid = {"positive": ["查天气"], "negative": ["不查其他城市"], "acceptance_criteria": []}
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误输出", "target": invalid},
        {"status": "accepted", "reason": "已修复", "target": _target()},
        {"status": "needs_human", "reason": "缺少事实", "question": "README 当前是什么状态？"},
    ])
    controller = ConversationController.open(repository, adapter, "ut_repair")
    replies = __import__("asyncio").run(controller.handle_user_message("补充 README"))

    assert controller.service.state == TDState.WAITING_HUMAN
    assert controller.service.context["recovery"]["retry_count"] == 1
    assert "repair_feedback" in adapter.calls[1]["payload"]["phase_context"]
    assert "README 当前" in replies[-1]
    rejection = [
        item for item in repository.operation_log(controller.service.context)
        if item.get("status") == "rejected"
    ][-1]
    assert rejection["operation"] == "submit_target"
    assert controller.service.context["recovery"]["last_failure"]["cause"] == "invalid_model_output"
    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    assert any(item["success_count"] == 1 for item in experiences)


def test_repeated_invalid_target_asks_human_instead_of_crashing(repository):
    invalid = {"positive": ["查天气"], "negative": ["不猜测"], "acceptance_criteria": []}
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误一", "target": invalid},
        {"status": "accepted", "reason": "错误二", "target": invalid},
    ])
    controller = ConversationController.open(repository, adapter, "ut_repair_human")
    replies = __import__("asyncio").run(controller.handle_user_message("上海天气"))

    assert controller.service.state == TDState.WAITING_HUMAN
    assert controller.service.context["control"]["return_to"] == "targeting"
    assert controller.service.context["recovery"]["retry_count"] == 2
    assert "成功标准" in replies[-1]
    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    assert any(item["failure_count"] == 1 for item in experiences)


def test_repeated_malformed_target_tool_json_stops_after_one_path_retry(repository):
    controller = ConversationController.open(repository, BrokenToolArgumentsAdapter(), "ut_bad_json")
    session_id = controller.service.context["session_id"]

    events = __import__("asyncio").run(controller.handle_user_events("查询上海天气"))

    assert controller.service.state == TDState.FAILED
    assert events[-1].type == "terminal"
    assert controller.service.context["recovery"]["last_failure"]["cause"] == "skill_or_model_runtime_failed"
    assert controller.service.context["recovery"]["runtime_retry_counts"]["target"] == 1
    assert any("failure-" in item for item in controller.service.context["artifacts"])

    operations = repository.operation_log(controller.service.context)
    failed = [item for item in operations if item.get("operation") == "generate_structured"][-1]
    assert failed["status"] == "failed"
    assert failed["phase"] == "target"
    assert failed["error_type"] == "LLMOutputError"
    assert failed["session_id"] == session_id
    evidence_path = repository.root / failed["evidence_ref"]
    assert evidence_path.exists()
    assert '"stage": "initial"' in evidence_path.read_text(encoding="utf-8")

    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    experience = next(
        item for item in experiences
        if item["exception"]["cause"] == "skill_or_model_runtime_failed"
    )
    assert experience["source_refs"]["session_id"] == session_id
    assert experience["failure_count"] == 1


def test_successful_json_repair_is_logged_as_successful_experience(repository):
    controller = ConversationController.open(repository, RepairedToolArgumentsAdapter(), "ut_recovered_json")

    __import__("asyncio").run(controller.handle_user_events("查询上海天气"))

    assert controller.service.state == TDState.WAITING_HUMAN
    operation = [
        item for item in repository.operation_log(controller.service.context)
        if item.get("operation") == "generate_structured"
    ][-1]
    assert operation["status"] == "recovered"
    assert (repository.root / operation["evidence_ref"]).exists()
    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    experience = next(item for item in experiences if item["exception"]["cause"] == "invalid_model_output")
    assert experience["use_count"] == 1
    assert experience["success_count"] == 1


def test_repeated_observe_skill_runtime_failure_stops_after_one_path_retry(repository):
    controller = ConversationController.open(repository, ObserveRuntimeFailureAdapter(), "ut_skill_failure")

    events = __import__("asyncio").run(controller.handle_user_events("查询上海天气"))

    assert controller.service.state == TDState.FAILED
    assert events[-1].type == "terminal"
    assert "明确失败" in events[-1].message
    assert controller.service.context["recovery"]["runtime_retry_counts"]["observe"] == 1
    operations = repository.operation_log(controller.service.context)
    failed = [item for item in operations if item.get("operation") == "generate_structured"][-1]
    assert failed["status"] == "failed"
    assert failed["phase"] == "observe"
    assert (repository.root / failed["evidence_ref"]).exists()
    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    experience = next(
        item for item in experiences
        if item["exception"]["cause"] == "skill_or_model_runtime_failed"
    )
    assert experience["use_count"] >= 1
    assert experience["success_count"] == 0
    assert experience["failure_count"] >= 1


def test_transient_observe_failure_recovers_and_continues_to_completion(repository):
    adapter = FlakyObserveAdapter([
        {"status": "accepted", "reason": "明确", "target": _target()},
        {"status": "accepted", "reason": "事实充分", "observation": _observation()},
        {"status": "accepted", "reason": "可行", "estimate": _estimate()},
        {"status": "accepted", "reason": "计划明确", "plan": {
            **_plan(),
            "actions": [{**_plan()["actions"][0], "executor": "agent_response"}],
        }},
        {
            "status": "accepted", "reason": "已完成",
            "result": {"content": "# README\n\n## 安装\nuv sync\n\n## 测试\nuv run pytest", "summary": "README"},
        },
        {
            "status": "accepted", "reason": "断言通过",
            "checks": [{"assertion_id": "a1", "description": "章节存在", "required": True,
                        "passed": True, "evidence_refs": ["agent-response"]}],
        },
        {
            "status": "accepted", "reason": "目标通过",
            "checks": [{"criterion_id": "c1", "description": "包含安装和测试说明",
                        "required": True, "passed": True, "evidence_refs": ["agent-response"]}],
        },
    ])
    controller = ConversationController.open(repository, adapter, "ut_flaky_observe")

    events = __import__("asyncio").run(controller.handle_user_events("补充 README"))

    assert controller.service.state == TDState.SUCCEEDED
    assert controller.service.context["recovery"]["runtime_retry_counts"]["observe"] == 1
    assert any(event.type == "automatic_retry" for event in events)
    experiences = controller.service.experience.rebuild_index()["experiences"].values()
    experience = next(
        item for item in experiences
        if item["exception"]["cause"] == "skill_or_model_runtime_failed"
    )
    assert experience["success_count"] == 1


def test_exhausted_transport_does_not_retry_the_whole_phase(repository):
    adapter = ExhaustedTransportAdapter()
    controller = ConversationController.open(repository, adapter, "ut_transport_exhausted")

    events = __import__("asyncio").run(controller.handle_user_events("收集主机资源信息"))

    assert adapter.calls == 1
    assert controller.service.state == TDState.FAILED
    assert events[-1].type == "terminal"
    assert events[-1].data["transport_exhausted"] is True
    assert "模型传输层自动恢复已用尽" in events[-1].message


def test_agent_response_action_runs_checks_persists_artifact_and_succeeds(repository):
    adapter = FakeStructuredAdapter([
        {
            "status": "accepted",
            "reason": "已生成回复",
            "result": {"content": "# 上海天气\n\n今天有雨，25～29℃。", "summary": "天气回复"},
        },
        {
            "status": "accepted",
            "reason": "行动断言通过",
            "checks": [{
                "assertion_id": "as_weather",
                "description": "回复包含天气和温度",
                "required": True,
                "passed": True,
                "evidence": "候选回复包含有雨和25～29℃",
            }],
        },
        {
            "status": "accepted",
            "reason": "目标验收通过",
            "checks": [{
                "assertion_id": "tc_weather",
                "description": "交付天气信息",
                "required": True,
                "passed": True,
                "evidence": "最终回复满足目标",
            }],
        },
    ])
    controller = ConversationController.open(repository, adapter, "ut_agent_response")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation(_observation())
    service.submit_estimate(_estimate())
    service.submit_plan({
        "plan_id": "plan_response",
        "version": 1,
        "actions": [{
            "action_id": "act_weather",
            "objective": "向用户输出天气回复",
            "depends_on": [],
            "instruction": "根据事实回复用户",
            "executor": "agent_response",
            "assertions": [{"description": "回复包含天气和温度", "required": True}],
            "max_attempts": 2,
        }],
    })

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert service.state == TDState.SUCCEEDED
    assert events[-1].type == "assistant_message"
    assert "25～29℃" in events[-1].message
    assert service.context["checks"]["action_checks"][-1]["passed"] is True
    assert service.context["checks"]["target_check"]["passed"] is True
    artifact_ref = service.context["artifacts"][0]
    assert (repository.root / artifact_ref).read_text(encoding="utf-8").startswith("# 上海天气")


def test_running_skill_script_action_returns_control_without_completing_action(repository):
    job_payload = {
        "ok": True,
        "job_id": "cmd-1234abcd",
        "status": "running",
        "supervisor_pid": 1234,
    }
    adapter = FakeStructuredAdapter([{
        "status": "accepted",
        "reason": "后台命令已启动",
        "result": {"content": "后台命令 cmd-1234abcd 正在运行"},
        "_skill_events": [{
            "skill": "run-cmd",
            "tool": "run_skill_script",
            "status": "succeeded",
            "evidence": {"stdout": json.dumps(job_payload)},
        }],
    }])
    controller = ConversationController.open(repository, adapter, "ut_async_skill_action")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation(_observation())
    service.submit_estimate(_estimate())
    service.submit_plan({
        "plan_id": "plan_async",
        "version": 1,
        "actions": [{
            "action_id": "run_async",
            "objective": "异步执行检查命令",
            "depends_on": [],
            "instruction": "使用 run-cmd 启动后台命令",
            "executor": "skill_script",
            "assertions": [{"description": "命令退出码为 0", "required": True}],
            "max_attempts": 2,
        }],
    })

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert service.state == TDState.ACTING
    assert events[-1].type == "background_job_running"
    assert events[-1].data["background_job"]["job_id"] == "cmd-1234abcd"
    assert service.context["execution"]["attempts"] == []
    assert service.context["artifacts"] == []


def test_state_changing_action_requires_and_persists_before_after_evidence(repository):
    skill_events = [
        {
            "skill": "run-cmd", "tool": "run_skill_script", "status": "succeeded",
            "evidence_role": role, "raw_output": {"ok": True, "role": role},
            "evidence": {"stdout": json.dumps({"status": "completed", "role": role})},
        }
        for role in ("before", "action", "after")
    ]
    adapter = FakeStructuredAdapter([{
        "status": "accepted", "reason": "变更和验证已完成",
        "result": {"content": "配置变更已完成，变更后状态正常"},
        "_skill_events": skill_events,
    }])
    controller = ConversationController.open(repository, adapter, "ut_act_before_after")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation(_observation())
    service.submit_estimate(_estimate())
    service.submit_plan({
        "plan_id": "plan_change", "version": 1,
        "actions": [{
            "action_id": "change_config", "objective": "修改配置", "depends_on": [],
            "instruction": "修改前查询、执行修改、修改后验证",
            "executor": "skill_script", "changes_state": True,
            "assertions": [{"description": "变更后配置正常", "required": True}],
            "max_attempts": 2,
        }],
    })

    event = __import__("asyncio").run(controller._run_act())

    assert event.type == "phase_completed"
    raw_evidence = [
        item for item in service.context["evidence_registry"]
        if item["type"] == "raw_json" and item["phase"] == "act"
    ]
    names = {__import__("pathlib").Path(item["path"]).name for item in raw_evidence}
    assert any(name.startswith("act-before-") for name in names)
    assert any(name.startswith("act-action-") for name in names)
    assert any(name.startswith("act-after-") for name in names)


def test_state_changing_action_rejects_missing_after_evidence(repository):
    adapter = FakeStructuredAdapter([{
        "status": "accepted", "reason": "未验证", "result": {"content": "已执行"},
        "_skill_events": [{
            "skill": "run-cmd", "tool": "run_skill_script", "status": "succeeded",
            "evidence_role": "action", "raw_output": {"ok": True},
            "evidence": {"stdout": "{}"},
        }],
    }])
    controller = ConversationController.open(repository, adapter, "ut_act_missing_after")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation(_observation())
    service.submit_estimate(_estimate())
    service.submit_plan({
        "actions": [{
            "action_id": "change", "objective": "修改配置", "depends_on": [],
            "instruction": "修改配置", "executor": "skill_script", "changes_state": True,
            "assertions": [{"description": "配置正常", "required": True}], "max_attempts": 1,
        }],
    })

    with pytest.raises(ValidationError, match="before/action/after evidence"):
        __import__("asyncio").run(controller._run_act())


def test_estimate_mechanical_fields_are_filled_without_another_model_call(repository):
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "明确", "target": _target()},
        {"status": "accepted", "reason": "充分", "observation": _observation()},
        {"status": "accepted", "reason": "缺字段", "estimate": {
            "verdict": "feasible", "risks": [],
        }},
        {"status": "accepted", "reason": "计划", "plan": _plan()},
    ])
    controller = ConversationController.open(repository, adapter, "ut_estimate_repair")

    __import__("asyncio").run(controller.handle_user_events("补充 README"))

    assert controller.service.state == TDState.ACTING
    estimate_calls = [call for call in adapter.calls if call["phase"] == "estimate"]
    assert len(estimate_calls) == 1
    assert controller.service.context["estimate"]["cost"]["mechanical_operations"] == "derived by controller"
    normalization = [
        item for item in repository.operation_log(controller.service.context)
        if item.get("operation") == "deterministic_normalization" and item.get("phase") == "estimate"
    ]
    assert normalization[-1]["status"] == "succeeded"


def test_decide_rejects_information_collection_action_and_repairs(repository):
    acquisition_plan = {
        "plan_id": "bad_observe_in_act", "version": 1,
        "actions": [{
            "action_id": "fetch", "objective": "获取官方资料", "depends_on": [],
            "instruction": "访问网站并抓取正文", "executor": "external",
            "assertions": [{"description": "已获取", "required": True}], "max_attempts": 1,
        }],
    }
    repaired_plan = {
        "plan_id": "external_change", "version": 2,
        "actions": [{
            "action_id": "edit", "objective": "修改 README", "depends_on": [],
            "instruction": "修改文件", "executor": "external",
            "assertions": [{"description": "文件已修改", "required": True}], "max_attempts": 1,
        }],
    }
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误计划", "plan": acquisition_plan},
        {"status": "accepted", "reason": "修复计划", "plan": repaired_plan},
    ])
    controller = ConversationController.open(repository, adapter, "ut_decide_repair")
    controller.service.start()
    controller.service.submit_target(_target())
    controller.service.submit_observation(_observation())
    controller.service.submit_estimate(_estimate())

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert controller.service.state == TDState.ACTING
    assert events[-1].type == "executor_boundary"
    decide_calls = [call for call in adapter.calls if call["phase"] == "decide"]
    assert len(decide_calls) == 2
    assert "repair_feedback" in decide_calls[1]["payload"]["phase_context"]


def test_agent_response_report_can_mention_access_time_without_false_acquisition_rejection(repository):
    controller = ConversationController.open(repository, FakeStructuredAdapter([]), "ut_report_plan")
    controller._validate_plan_runtime({
        "actions": [{
            "action_id": "write_report",
            "objective": "生成包含访问时间、页面标题和正文的报告",
            "instruction": "仅基于 Observe 已记录事实输出报告",
            "executor": "agent_response",
        }],
    })


def test_target_noncanonical_evidence_directory_is_normalized_without_model_retry(repository):
    invalid_target = {
        **_target(),
        "positive": [*_target()["positive"], "保留网页截图作为证据，默认复制到 ./evidence/"],
    }
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误增加目录", "target": invalid_target},
        {"status": "needs_human", "reason": "缺少事实", "question": "README 当前是什么状态？"},
    ])
    controller = ConversationController.open(repository, adapter, "ut_target_evidence_contract")

    __import__("asyncio").run(controller.handle_user_events("补充 README，并保留操作证据"))

    assert controller.service.state == TDState.WAITING_HUMAN
    target_calls = [call for call in adapter.calls if call["phase"] == "target"]
    assert len(target_calls) == 1
    assert "./evidence" not in str(controller.service.context["target"])
    normalization = [
        item for item in repository.operation_log(controller.service.context)
        if item.get("operation") == "deterministic_normalization" and item.get("phase") == "target"
    ]
    assert normalization[-1]["status"] == "succeeded"


def test_target_cannot_replace_explicit_ssh_login_with_external_recon(repository):
    downgraded = {
        "positive": ["通过浏览器和搜索收集目标 IP 的公开信息"],
        "negative": ["不使用 root 登录"],
        "acceptance_criteria": [{
            "description": "保留浏览器截图", "required": True,
        }],
    }
    corrected = {
        "positive": [
            "以 root 身份通过 SSH 登录 45.126.120.34",
            "收集系统、CPU、内存、磁盘和容器信息",
        ],
        "negative": ["不执行破坏性变更"],
        "acceptance_criteria": [{
            "description": "形成服务器资源勘查报告", "required": True,
        }],
    }
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "降级", "target": downgraded},
        {"status": "accepted", "reason": "保留用户方法", "target": corrected},
        {"status": "needs_human", "reason": "stop after target", "question": "continue?"},
    ])
    controller = ConversationController.open(repository, adapter, "ut_target_fidelity")

    __import__("asyncio").run(controller.handle_user_events(
        "使用 root 通过 SSH 登录 45.126.120.34，查看系统和服务器资源",
    ))

    target_calls = [call for call in adapter.calls if call["phase"] == "target"]
    assert len(target_calls) == 2
    assert "repair_feedback" in target_calls[1]["payload"]["phase_context"]
    errors = target_calls[1]["payload"]["phase_context"]["repair_feedback"]["errors"]
    assert any("dropped the user-specified SSH/root login method" in item for item in errors)
    assert controller.service.context["target"]["positive"] == corrected["positive"]


def test_plan_cannot_relocate_existing_screenshot_evidence(repository):
    controller = ConversationController.open(repository, FakeStructuredAdapter([]), "ut_evidence_relocation")
    controller.service.context["observation"] = {
        "facts": [{
            "description": "网页截图已保存",
            "source_type": "tool",
            "source_ref": "trace/sessions/sess-demo/screenshots/observe-demo.png",
        }],
        "unknowns": [],
    }

    with pytest.raises(ValidationError, match="redundantly relocates screenshot evidence"):
        controller._validate_plan_runtime({
            "actions": [{
                "action_id": "archive_screenshot",
                "objective": "将网页截图归档至 ./evidence/",
                "instruction": "复制已有 PNG 到 ./evidence/",
                "executor": "agent_response",
                "assertions": [{"description": "./evidence/ 下存在截图", "required": True}],
            }],
        })


def test_report_plan_that_forbids_screenshot_relocation_is_valid(repository):
    controller = ConversationController.open(
        repository, FakeStructuredAdapter([]), "ut_evidence_relocation_negation",
    )
    controller.service.context["observation"] = {
        "facts": [{
            "description": "网页截图已保存",
            "source_type": "tool",
            "source_ref": "trace/sessions/sess-demo/screenshots/observe-demo.png",
        }],
        "unknowns": [],
    }

    controller._validate_plan_runtime({
        "actions": [{
            "action_id": "report-zh-example-com",
            "objective": "生成中文报告并引用 canonical 截图证据",
            "instruction": (
                "生成简短中文报告，引用截图路径 trace/sessions/sess-demo/"
                "screenshots/observe-demo.png。不得复制、移动或重新归档截图，"
                "不得执行任何 Observe 或外部变更。"
            ),
            "executor": "agent_response",
            "assertions": [{
                "description": "报告引用现有截图但不复制截图",
                "required": True,
                "check": {"type": "references_evidence"},
            }],
        }],
    })


def test_model_can_adopt_cross_thread_system_experience_and_update_effectiveness(repository):
    signature = {
        "phase": "decide",
        "cause": "semantic_validation_failed",
        "error_code": "plan.screenshot_relocation_conflict",
    }
    seed_store = ExperienceStore(repository.root)
    prior_id = seed_store.observe_exception(
        scope_id="ut_prior", user_thread_id="ut_prior", td_id="td_prior", session_id="ss_prior",
        exception={**signature, "message": "否定的截图复制语句被误判为迁移"},
        visibility="system", signature=signature,
    )
    seed_store.record_resolution(prior_id, "ut_prior", {
        "type": "control_rule", "instruction": "识别否定词，不要删除报告 Action",
    })
    seed_treatment = seed_store.treatment_started(prior_id, "ut_prior", "fix_negation_rule")
    seed_store.treatment_finished(
        prior_id, "ut_prior", True, {"version": "0.6.1"}, treatment_id=seed_treatment,
    )
    invalid_plan = {
        "plan_id": "plan-invalid", "version": 1,
        "actions": [{
            "action_id": "report-copy", "objective": "生成报告并复制截图", "depends_on": [],
            "instruction": "生成中文报告，然后复制截图到其他目录", "executor": "agent_response",
            "assertions": [{"description": "报告和截图副本存在", "required": True}],
            "max_attempts": 2,
        }],
    }
    valid_plan = {
        "plan_id": "plan-valid", "version": 2,
        "actions": [{
            "action_id": "write-report", "objective": "写入中文报告", "depends_on": [],
            "instruction": "引用现有截图生成报告，不复制截图", "executor": "external",
            "assertions": [{"description": "报告存在", "required": True}],
            "max_attempts": 2,
        }],
    }
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误计划", "plan": invalid_plan},
        {
            "status": "accepted", "reason": "采纳历史修复方案", "plan": valid_plan,
            "experience_decisions": [{
                "experience_id": prior_id, "decision": "adopt",
                "reason": "同一控制规则冲突，历史方案已有成功记录", "confidence": 0.95,
            }],
        },
    ])
    controller = ConversationController.open(repository, adapter, "ut_current")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation({
        "facts": [{
            "description": "截图已保存在 Session trace/screenshots/proof.png",
            "source_type": "tool", "source_ref": "proof.png",
        }],
        "unknowns": [],
    })
    service.submit_estimate(_estimate())

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert service.state == TDState.ACTING
    assert events[-1].type == "executor_boundary"
    prior = seed_store.get(prior_id)
    assert prior["match_count"] == 1
    assert prior["adopt_count"] == 1
    assert prior["success_count"] == 2
    current = next(
        item for key, item in seed_store.rebuild_index()["experiences"].items()
        if key != prior_id and item.get("signature", {}).get("error_code") == signature["error_code"]
    )
    assert current["visibility"] == "system"
    assert current["success_count"] == 1
    assert current["failure_count"] == 0
    assert len(current["source_refs"]["operation_ids"]) == 1


def test_repeated_plan_validation_failure_records_complete_terminal_experience(repository):
    invalid_plan = {
        "plan_id": "plan-invalid", "version": 1,
        "actions": [{
            "action_id": "report-copy", "objective": "生成报告并复制截图", "depends_on": [],
            "instruction": "生成中文报告，然后复制截图到其他目录", "executor": "agent_response",
            "assertions": [{"description": "报告和截图副本存在", "required": True}],
            "max_attempts": 2,
        }],
    }
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "错误计划一", "plan": invalid_plan},
        {"status": "accepted", "reason": "错误计划二", "plan": invalid_plan},
    ])
    controller = ConversationController.open(repository, adapter, "ut_terminal_experience")
    service = controller.service
    service.start()
    service.submit_target(_target())
    service.submit_observation({
        "facts": [{
            "description": "截图已保存在 Session trace/screenshots/proof.png",
            "source_type": "tool", "source_ref": "proof.png",
        }],
        "unknowns": [],
    })
    service.submit_estimate(_estimate())

    __import__("asyncio").run(controller.handle_user_events("继续"))

    assert service.state == TDState.FAILED
    experience = next(
        item for item in service.experience.rebuild_index()["experiences"].values()
        if item.get("signature", {}).get("error_code") == "plan.screenshot_relocation_conflict"
    )
    assert experience["visibility"] == "system"
    assert experience["use_count"] == 1
    assert experience["success_count"] == 0
    assert experience["failure_count"] == 1
    assert experience["treatments"][0]["status"] == "failed"
    assert len(experience["source_refs"]["operation_ids"]) == 2
    assert len(experience["source_refs"]["artifact_refs"]) == 1
    assert experience["last_outcome"]["outcome"] == "failed"
    artifact = repository.root / experience["source_refs"]["artifact_refs"][0]
    failure_report = __import__("json").loads(artifact.read_text(encoding="utf-8"))
    assert failure_report["last_failure"]["cause"] == "semantic_validation_failed"
    assert failure_report["last_failure"]["terminal_cause"] == "semantic_validation_exhausted"


def _prepare_bad_screenshot_archive_plan(controller):
    controller.service.start()
    controller.service.submit_target(_target())
    controller.service.submit_observation({
        "facts": [{
            "description": "截图已保存在 Session trace/screenshots/observe-demo.png",
            "source_type": "tool", "source_ref": "observe-demo.png",
        }],
        "unknowns": [],
    })
    controller.service.submit_estimate(_estimate())
    controller.service.submit_plan({
        "plan_id": "legacy_bad_plan", "version": 1,
        "actions": [{
            "action_id": "archive_screenshot",
            "objective": "将截图归档至 ./evidence/",
            "depends_on": [], "instruction": "复制截图到 ./evidence/",
            "executor": "agent_response",
            "assertions": [{"description": "./evidence/ 存在截图", "required": True}],
            "max_attempts": 2,
        }],
    })


def test_internal_action_confirmation_is_suppressed_and_replanned(repository):
    adapter = FakeStructuredAdapter([
        {
            "status": "needs_human",
            "reason": "archive_screenshot 要求复制至 ./evidence/，但截图已在 trace/screenshots",
            "question": "选择 A 接受 trace，还是 B 授权复制？",
        },
        {"status": "accepted", "reason": "删除多余归档", "plan": _plan()},
    ])
    controller = ConversationController.open(repository, adapter, "ut_suppress_internal_confirm")
    _prepare_bad_screenshot_archive_plan(controller)

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert controller.service.state == TDState.ACTING
    assert not any(event.type == "human_question" for event in events)
    assert any(event.type == "automatic_recovery" for event in events)
    assert controller.service.context["plan"]["plan_id"] == "plan_chat"


def test_answer_to_legacy_internal_confirmation_is_consumed_as_replan(repository):
    adapter = FakeStructuredAdapter([
        {"status": "accepted", "reason": "删除多余归档", "plan": _plan()},
    ])
    controller = ConversationController.open(repository, adapter, "ut_legacy_confirm_reply")
    _prepare_bad_screenshot_archive_plan(controller)
    controller.service.request_human(
        "请选择 A 接受 trace/screenshots，或 B 复制至 ./evidence/。",
        "archive_screenshot 的 ./evidence/ 断言与 trace/screenshots 证据位置冲突",
    )

    events = __import__("asyncio").run(controller.handle_user_events("A"))

    assert controller.service.state == TDState.ACTING
    assert not any(event.type == "human_question" for event in events)
    assert controller.service.context["plan"]["plan_id"] == "plan_chat"
    assert controller.service.context["control"]["human_response"] == {"text": "A"}


def test_reopening_legacy_internal_confirmation_auto_resumes_at_decide(repository):
    first = ConversationController.open(
        repository, FakeStructuredAdapter([]), "ut_legacy_confirm_reopen",
    )
    _prepare_bad_screenshot_archive_plan(first)
    first.service.request_human(
        "是否授权修订 archive_screenshot，将 ./evidence/ 改为 trace/screenshots？",
        "当前 Plan 的截图证据位置与既有 trace/screenshots 事实冲突",
    )
    first.detach_connection()

    reopened = ConversationController.open(
        repository, FakeStructuredAdapter([]), "ut_legacy_confirm_reopen",
    )

    assert reopened.service.state == TDState.DECIDING
    assert reopened.service.context["plan"]["status"] == "revision_requested"
    assert reopened.service.context["control"]["human_question"] is None


def test_inspect_accepts_cli_observe_alias(repository):
    controller = ConversationController.open(repository, FakeStructuredAdapter([]), "ut_inspect_alias")

    result = controller.inspect("observe")

    assert "observation" in result
    assert "unsupported" not in result


def test_completed_td_opens_as_read_only_without_reattaching(repository):
    first = ConversationController.open(repository, FakeStructuredAdapter([]), "ut_read_only")
    session_id = first.service.context["session_id"]
    first.service.cancel()

    reopened = ConversationController.open(
        repository, FakeStructuredAdapter([]), "ut_read_only", session_id=session_id,
    )

    assert reopened.read_only is True
    assert reopened.service.state == TDState.CANCELLED
    assert reopened.service.context["session_id"] == session_id
    reopened.detach_connection()
