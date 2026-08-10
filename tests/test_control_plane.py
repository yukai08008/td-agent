from __future__ import annotations

from pathlib import Path

from toe_dac.control_plane import DeterministicControlPlane
from toe_dac.conversation import ConversationController
from toe_dac.llm_adapter import StructuredLLMResult
from toe_dac.service import TDService
from toe_dac.skill_runtime import SkillToolResult
from toe_dac.states import TDState


class FakeBrowserRuntime:
    def configure_evidence(self, screenshot_dir, session_id):
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    async def execute(self, tool_name, arguments, progress_callback=None):
        screenshot = self.screenshot_dir / "observe-example.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nmechanical-control-plane")
        output = {
            "ok": True,
            "url": arguments["url"],
            "page_title": "Example Domain",
            "body_text": "This domain is for use in documentation examples.",
            "snapshot": "heading Example Domain",
            "screenshot_ref": str(screenshot),
            "screenshot_size_bytes": screenshot.stat().st_size,
            "screenshot_format": "png",
            "observed_at": "2026-08-10T00:00:00+08:00",
        }
        return SkillToolResult(output, {
            "skill": "agent-browser",
            "tool": tool_name,
            "status": "succeeded",
            "attempt_count": 1,
            "duration_ms": 10,
            "evidence": output,
        })


class SequencedAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.skill_runtime = FakeBrowserRuntime()

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        skill_events = response.pop("_skill_events", [])
        return StructuredLLMResult(
            data=response, model_id="fake", usage={},
            finish_reason="tool_calls", raw_content=None, skill_events=skill_events,
        )


def _web_target():
    return {
        "positive": ["访问 https://example.com 并生成报告"],
        "negative": ["不得猜测网页内容"],
        "acceptance_criteria": [{
            "description": "报告包含页面标题和主要内容",
            "required": True,
            "check": {"type": "non_empty"},
        }],
    }


def test_runtime_evidence_is_mechanically_hashed_after_model_selected_tool(repository):
    service = TDService.create(repository, "ut_mechanical_observe")
    service.start()
    service.submit_target(_web_target())
    runtime = FakeBrowserRuntime()
    runtime.configure_evidence(
        repository.session_evidence_dir(service.context) / "screenshots",
        service.context["session_id"],
    )
    tool_result = __import__("asyncio").run(runtime.execute(
        "agent_browser_observe", {"url": "https://example.com"},
    ))
    plane = DeterministicControlPlane(repository, service.context, object())
    records = plane.evidence_records_from_tool_events([tool_result.event], phase="observe")
    service.register_evidence(records)

    screenshot_record = next(item for item in records if item["type"] == "screenshot")
    assert screenshot_record["sha256"]
    assert screenshot_record["path"].startswith(str(repository.session_evidence_dir(service.context)))
    assert screenshot_record["metadata"]["page_title"] == "Example Domain"
    assert Path(screenshot_record["path"]).name.startswith("observe-")
    assert service.context["evidence_registry"][0]["type"] == "screenshot"


def test_plan_normalization_removes_mechanical_evidence_and_acceptance_actions(repository):
    service = TDService.create(repository, "ut_normalize_plan")
    service.context["observation"] = {
        "facts": [{"description": "截图已保存为 observe.png", "source_type": "tool"}],
        "unknowns": [],
    }
    plane = DeterministicControlPlane(repository, service.context, object())
    plan, changes = plane.normalize_plan({
        "actions": [
            {"action_id": "report", "objective": "生成中文报告", "instruction": "输出报告",
             "executor": "agent_response", "assertions": [{"description": "报告为中文", "required": True}]},
            {"action_id": "archive", "objective": "归档截图", "instruction": "复制截图到 ./evidence/",
             "executor": "agent_response", "assertions": [{"description": "截图存在", "required": True}]},
            {"action_id": "verify", "objective": "核验全部验收标准", "instruction": "执行最终验收",
             "executor": "agent_response", "assertions": [{"description": "全部通过", "required": True}]},
        ],
    }, "生成报告并保留截图")

    assert [action["action_id"] for action in plan["actions"]] == ["report"]
    assert plan["actions"][0]["assertions"][0]["check"] == {"type": "language_zh"}
    assert len(changes) >= 3


def test_target_normalization_removes_runtime_evidence_requirements(repository):
    service = TDService.create(repository, "ut_target_effect_only")
    plane = DeterministicControlPlane(repository, service.context, object())

    target, changes = plane.normalize_target({
        "positive": ["生成中文网页报告", "保留网页截图作为证据"],
        "negative": ["不猜测网页内容"],
        "acceptance_criteria": [
            {"description": "报告包含页面主要内容", "required": True},
            {"description": "截图证据已留存", "required": True,
             "check": {"type": "evidence_exists", "evidence_type": "screenshot"}},
        ],
    }, "生成中文网页报告并留证")

    assert target["positive"] == ["生成中文网页报告"]
    assert [item["description"] for item in target["acceptance_criteria"]] == [
        "报告包含页面主要内容",
    ]
    assert len(changes) == 2


def test_report_action_that_mentions_archived_screenshot_is_not_misclassified_as_file_copy(repository):
    service = TDService.create(repository, "ut_report_with_evidence")
    service.context["evidence_registry"] = [{"type": "screenshot", "path": "/tmp/proof.png"}]
    plane = DeterministicControlPlane(repository, service.context, object())
    plan, _ = plane.normalize_plan({
        "actions": [{
            "action_id": "deliver-report", "objective": "生成中文报告并引用截图证据",
            "instruction": "输出报告，说明截图已经归档在 canonical evidence directory",
            "executor": "agent_response", "depends_on": [], "max_attempts": 2,
            "assertions": [{"description": "报告引用截图", "required": True}],
        }],
    }, "生成简短中文报告，并保留截图作为证据")

    assert [action["action_id"] for action in plan["actions"]] == ["deliver-report"]


def test_model_invented_length_limit_is_not_promoted_to_hard_user_constraint(repository):
    service = TDService.create(repository, "ut_length_constraint")
    plane = DeterministicControlPlane(repository, service.context, object())
    target, target_changes = plane.normalize_target({
        "positive": [
            "确认页面标题为 Example Domain",
            "确认页面主要内容包含 This domain is for illustrative examples",
            "生成简短中文报告",
        ], "negative": [],
        "acceptance_criteria": [
            {"description": "观察记录有标题", "required": True,
             "check": {"type": "observation_field_non_empty", "field": "title"}},
            {"description": "实际标题为 Example Domain", "required": True,
             "check": {"type": "observation_contains", "value": "Example Domain"}},
            {"description": "实际主要内容包含 illustrative examples", "required": True,
             "check": {"type": "observation_contains", "value": "illustrative examples"}},
            {"description": "报告不超过 500 字", "required": True,
             "check": {"type": "max_length", "value": 500}},
        ],
    }, "生成一份简短中文报告")
    plan, plan_changes = plane.normalize_plan({
        "actions": [{
            "action_id": "report", "objective": "生成简短报告", "depends_on": [],
            "instruction": "输出报告", "executor": "agent_response", "max_attempts": 2,
            "assertions": [{
                "description": "报告不超过 500 字", "required": True,
                "check": {"type": "max_length", "value": 500},
            }],
        }],
    }, "生成一份简短中文报告")

    assert target["acceptance_criteria"][0]["check"]["field"] == "page_title"
    assert target["acceptance_criteria"][3]["check"]["value"] == 1200
    assert target["positive"][:2] == ["确认并记录页面实际标题", "确认并记录页面实际主要内容"]
    assert plan["actions"][0]["assertions"][0]["check"]["value"] == 1200
    assert target_changes and plan_changes


def test_controller_keeps_model_judgment_in_observe_and_mechanically_registers_evidence(repository):
    screenshot_holder = {}
    service_adapter = SequencedAdapter([])
    controller = ConversationController.open(repository, service_adapter, "ut_controller_observe")
    evidence_dir = repository.session_evidence_dir(controller.service.context) / "screenshots"
    runtime = FakeBrowserRuntime()
    runtime.configure_evidence(evidence_dir, controller.service.context["session_id"])
    tool_result = __import__("asyncio").run(runtime.execute(
        "agent_browser_observe", {"url": "https://example.com"},
    ))
    screenshot_holder["path"] = tool_result.output["screenshot_ref"]
    service_adapter.responses.extend([
        {"status": "accepted", "reason": "目标明确", "target": _web_target()},
        {"status": "accepted", "reason": "模型判断事实充分", "observation": {
            "facts": [
                {"description": "页面标题是 Example Domain", "source_type": "tool_result",
                 "source_ref": screenshot_holder["path"],
                 "value": {"page_title": "Example Domain"}},
            ],
            "unknowns": [],
        }, "_skill_events": [tool_result.event]},
        {"status": "accepted", "reason": "可行", "estimate": {
            "verdict": "feasible", "risks": [], "cost": {}, "information_gaps": [],
        }},
        {"status": "accepted", "reason": "计划", "plan": {
            "actions": [{
                "action_id": "report", "objective": "生成报告", "depends_on": [],
                "instruction": "输出中文报告", "executor": "external",
                "assertions": [{"description": "结果非空", "required": True,
                                "check": {"type": "non_empty"}}],
                "max_attempts": 1,
            }],
        }},
    ])

    events = __import__("asyncio").run(controller.handle_user_events(
        "访问 https://example.com，确认页面并保留截图，然后生成报告",
    ))

    assert controller.service.state == TDState.ACTING
    assert [call["phase"] for call in service_adapter.calls] == ["target", "observe", "estimate", "decide"]
    registry = controller.service.context["evidence_registry"]
    assert any(item["type"] == "screenshot" for item in registry)
    raw_paths = [Path(item["path"]) for item in registry if item["type"] == "raw_json"]
    assert raw_paths
    assert {path.name.split("-", 1)[0] for path in raw_paths} >= {
        "target", "observe", "estimate", "decide",
    }
    assert any(event.phase == "observe" and event.type == "phase_completed" for event in events)


def test_deterministic_checks_provide_hard_facts_for_model_audit(repository):
    service = TDService.create(repository, "ut_deterministic_checks")
    screenshot = repository.session_evidence_dir(service.context) / "screenshots" / "proof.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nproof")
    service.context["evidence_registry"] = [{"evidence_id": "evi_1", "type": "screenshot", "path": str(screenshot)}]
    plane = DeterministicControlPlane(repository, service.context, object())
    action = {
        "assertions": [
            {"description": "包含标题", "required": True, "check": {"type": "contains", "value": "Example Domain"}},
            {"description": "中文报告", "required": True, "check": {"type": "language_zh"}},
            {"description": "截图存在", "required": True, "check": {"type": "evidence_exists", "evidence_type": "screenshot"}},
        ],
    }
    attempt = {"result": {"content": "中文报告：页面标题为 Example Domain，截图已经保存。"}}

    result = plane.check_action(action, attempt)

    assert result.complete is True
    assert all(check["passed"] for check in result.checks)
    assert all(check["decision_source"] == "deterministic_control_plane" for check in result.checks)


def test_action_and_target_checks_keep_model_audit_when_hard_checks_are_complete(repository):
    adapter = SequencedAdapter([
        {"status": "accepted", "reason": "语义审查通过", "checks": [{
            "assertion_id": "semantic-action-audit",
            "description": "候选报告在语义上实现了 Action 目标",
            "required": True,
            "passed": True,
            "evidence": "报告内容与 Action 目标一致",
        }]},
        {"status": "accepted", "reason": "目标审查通过", "checks": [{
            "assertion_id": "semantic-target-audit",
            "description": "交付结果整体满足 Target 且未违反负向约束",
            "required": True,
            "passed": True,
            "evidence": "综合 Target、Observation、Artifact 与证据判断",
        }]},
    ])
    controller = ConversationController.open(repository, adapter, "ut_hybrid_checks")
    service = controller.service
    service.start()
    service.submit_target(_web_target())
    service.submit_observation({
        "facts": [{
            "description": "页面标题为 Example Domain",
            "source_type": "tool_result",
            "source_ref": "https://example.com",
        }],
        "unknowns": [],
    })
    service.submit_estimate({
        "verdict": "feasible", "risks": [], "cost": {}, "information_gaps": [],
    })
    service.submit_plan({
        "plan_id": "plan-hybrid", "version": 1,
        "actions": [{
            "action_id": "report", "objective": "生成中文报告", "depends_on": [],
            "instruction": "根据 Observation 生成报告", "executor": "agent_response",
            "assertions": [{
                "assertion_id": "report-non-empty", "description": "报告非空",
                "required": True, "check": {"type": "non_empty"},
            }],
            "max_attempts": 1,
        }],
    })
    screenshot = repository.session_evidence_dir(service.context) / "screenshots" / "proof.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nproof")
    service.register_evidence([{
        "evidence_id": "evi-proof", "type": "screenshot", "path": str(screenshot),
    }])
    service.submit_action_result({
        "result": {"executor": "agent_response", "content": "中文报告：Example Domain 页面有效。"},
        "evidence_refs": [],
    })

    events = __import__("asyncio").run(controller.handle_user_events("继续"))

    assert service.state == TDState.SUCCEEDED
    assert [call["phase"] for call in adapter.calls] == ["action_check", "target_check"]
    assert events[-1].data["decision_source"] == "hybrid"
    assert any(
        check.get("decision_source") == "deterministic_control_plane"
        for check in service.context["checks"]["target_check"]["checks"]
    )
    assert any(
        check.get("assertion_id") == "semantic-target-audit"
        for check in service.context["checks"]["target_check"]["checks"]
    )
