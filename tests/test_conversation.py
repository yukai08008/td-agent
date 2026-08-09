from __future__ import annotations

from toe_dac.conversation import ConversationController
from toe_dac.llm_adapter import StructuredLLMResult
from toe_dac.states import TDState


class FakeStructuredAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        data = self.responses.pop(0)
        return StructuredLLMResult(
            data=data,
            model_id="fake-model",
            usage={"input": 1, "output": 1},
            finish_reason="tool_calls",
            raw_content=None,
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


def test_conversation_is_multi_turn_and_cross_session(repository):
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

    second = ConversationController.open(repository, adapter, "ut_chat")
    assert second.service.context["session_id"] != first_session
    replies = __import__("asyncio").run(second.handle_user_message("只补充 README，不修改代码"))
    assert second.service.state == TDState.WAITING_HUMAN
    assert second.service.context["control"]["return_to"] == "observing"

    third = ConversationController.open(repository, adapter, "ut_chat")
    replies = __import__("asyncio").run(third.handle_user_message("README 目前只有标题"))
    assert third.service.state == TDState.ACTING
    assert any("Target 已确定" in item for item in replies) is False
    assert any("Plan 包含 1 个 Action" in item for item in replies)
    assert any("需要受限 Executor" in item for item in replies)

    history = repository.message_history("ut_chat")
    assert [item["role"] for item in history].count("user") == 3
    assert len({item["session_id"] for item in history}) == 3
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
    first.close_session()

    second = ConversationController.open(repository, adapter, "ut_sessions")
    second_session = second.service.context["session_id"]
    __import__("asyncio").run(second.handle_user_message("补充信息"))

    sessions = repository.list_sessions("ut_sessions")
    assert [item["session_id"] for item in sessions] == [first_session, second_session]
    assert [item["status"] for item in sessions] == ["completed", "active"]
    assert {item["session_id"] for item in repository.message_history("ut_sessions")} == {
        first_session, second_session,
    }
    assert all(
        (repository.thread_dir("ut_sessions") / "sessions" / f"{item['session_id']}.json").exists()
        for item in sessions
    )


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
