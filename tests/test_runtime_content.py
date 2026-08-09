from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from toe_dac.llm_adapter import TOEDACLLMAdapter
from toe_dac.runtime_content import (
    LoadedSkill,
    RuntimeContentLoader,
    RuntimePromptSnapshot,
    SkillIndexEntry,
    initialize_runtime_content,
)
from toe_dac.skill_runtime import SkillToolResult


def test_runtime_initialization_creates_claude_skills_and_blue_green_persona(tmp_path):
    created = initialize_runtime_content(tmp_path)

    assert created
    assert (tmp_path / "skills" / "index.md").read_text().startswith("---\n")
    assert (tmp_path / "skills" / "toe-dac-control" / "SKILL.md").read_text().startswith("---\n")
    assert (tmp_path / "skills" / "agent-browser" / "SKILL.md").read_text().startswith("---\n")
    assert (tmp_path / "skills" / "alex-serp" / "SKILL.md").read_text().startswith("---\n")
    assert (tmp_path / "persona" / "blue" / "system.md").is_file()
    assert (tmp_path / "persona" / "green" / "system.md").is_file()
    assert json.loads((tmp_path / "persona" / "active.json").read_text())["active"] == "blue"


def test_runtime_initialization_never_overwrites_user_content(tmp_path):
    initialize_runtime_content(tmp_path)
    prompt = tmp_path / "persona" / "blue" / "system.md"
    prompt.write_text("custom blue", encoding="utf-8")

    initialize_runtime_content(tmp_path)

    assert prompt.read_text(encoding="utf-8") == "custom blue"


def test_loader_loads_only_index_then_activates_selected_skill(tmp_path):
    initialize_runtime_content(tmp_path)
    blue = RuntimeContentLoader(tmp_path).load()
    assert blue.persona_slot == "blue"
    assert blue.skills == ()
    assert [skill.name for skill in blue.available_skills] == [
        "toe-dac-control", "agent-browser", "alex-serp",
    ]
    assert "agent-browser/SKILL.md" in blue.skill_index
    assert "# agent-browser — 浏览器自动化" not in blue.render("PHASE")

    activated = blue.activate(["agent-browser"])
    assert [skill.name for skill in activated.skills] == ["agent-browser"]
    assert activated.skills[0].requires == ("cli:agent-browser", "cli:node")
    assert "# agent-browser — 浏览器自动化" in activated.render("PHASE")

    control_path = tmp_path / "persona" / "active.json"
    control = json.loads(control_path.read_text(encoding="utf-8"))
    control.update({"active": "green", "standby": "blue", "revision": 2})
    control_path.write_text(json.dumps(control), encoding="utf-8")
    green = RuntimeContentLoader(tmp_path).load()

    assert green.persona_slot == "green"
    assert green.persona_revision == 2
    assert "candidate TD Agent" in green.persona


def test_loader_rejects_skill_path_escape(tmp_path):
    initialize_runtime_content(tmp_path)
    index_path = tmp_path / "skills" / "index.md"
    index = index_path.read_text(encoding="utf-8")
    index_path.write_text(
        index.replace("toe-dac-control/SKILL.md", "../persona/blue/system.md"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes its root"):
        RuntimeContentLoader(tmp_path).load()


def test_adapter_injects_runtime_snapshot_into_every_system_prompt(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "test-model",
        "enabled": True,
        "apiKeyEnv": "TEST_KEY",
        "url": "https://example.invalid/v1/chat/completions",
    }]}), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TEST_KEY=secret\n", encoding="utf-8")
    snapshot = RuntimePromptSnapshot(
        persona_slot="blue",
        persona_revision=3,
        persona="GLOBAL PERSONA",
        skill_index="# Skills Index\n\n## demo\n\n- Description: demo",
    )
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot)
    adapter.client.generate = AsyncMock(return_value=SimpleNamespace())

    __import__("asyncio").run(adapter._call(
        "PHASE PROMPT", {"value": 1}, "submit_target", {"type": "object"},
    ))

    messages = adapter.client.generate.await_args.args[0]
    system = messages[0].content
    assert "GLOBAL PERSONA" in system
    assert "## demo" in system
    assert "PHASE PROMPT" in system


def test_adapter_loads_skill_on_demand_then_retries_phase(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "test-model",
        "enabled": True,
        "apiKeyEnv": "TEST_KEY",
        "url": "https://example.invalid/v1/chat/completions",
    }]}), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TEST_KEY=secret\n", encoding="utf-8")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n\nDEMO SKILL BODY\n",
        encoding="utf-8",
    )
    snapshot = RuntimePromptSnapshot(
        persona_slot="blue",
        persona_revision=1,
        persona="GLOBAL PERSONA",
        skill_index="## demo\n\n- Description: demo",
        available_skills=(SkillIndexEntry("demo", "demo", "demo/SKILL.md", ()),),
        skills_root=str(skills_root),
    )
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot)
    load_response = SimpleNamespace(
        tool_calls=[SimpleNamespace(function={
            "name": "load_skill", "arguments": json.dumps({"names": ["demo"]}),
        })],
        model_id="test-model",
    )
    phase_response = SimpleNamespace(tool_calls=[], content="{}", model_id="test-model")
    adapter.client.generate = AsyncMock(side_effect=[load_response, phase_response])

    result = __import__("asyncio").run(adapter._call(
        "PHASE PROMPT", {"value": 1}, "submit_target", {"type": "object"},
    ))

    assert result is phase_response
    assert adapter.client.generate.await_count == 2
    first_system = adapter.client.generate.await_args_list[0].args[0][0].content
    second_system = adapter.client.generate.await_args_list[1].args[0][0].content
    assert "DEMO SKILL BODY" not in first_system
    assert "DEMO SKILL BODY" in second_system


def test_adapter_enforces_per_phase_skill_call_budget(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "test-model", "enabled": True, "apiKeyEnv": "TEST_KEY",
        "url": "https://example.invalid/v1/chat/completions",
    }]}), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TEST_KEY=secret\n", encoding="utf-8")

    class FakeSkillRuntime:
        def __init__(self):
            self.execute = AsyncMock(return_value=SkillToolResult(
                {"ok": True, "results": [], "count": 0},
                {"skill": "alex-serp", "tool": "alex_serp_search", "status": "succeeded", "attempt_count": 1},
            ))

        def tool_definitions(self, active_skills, phase):
            return [{"type": "function", "function": {
                "name": "alex_serp_search", "description": "search", "parameters": {"type": "object"},
            }}] if phase == "observe" else []

    runtime = FakeSkillRuntime()
    snapshot = RuntimePromptSnapshot(
        persona_slot="blue", persona_revision=1, persona="GLOBAL",
        skills=(LoadedSkill("alex-serp", "search", "SEARCH BODY", (), ("observe",)),),
    )
    adapter = TOEDACLLMAdapter(
        config, "test-model", runtime_snapshot=snapshot, skill_runtime=runtime,
    )
    search_responses = [SimpleNamespace(
        tool_calls=[SimpleNamespace(
            id=f"call-{index}",
            function={"name": "alex_serp_search", "arguments": '{"query":"上海天气"}'},
        )],
        content=None,
        model_id="test-model",
    ) for index in range(4)]
    phase_response = SimpleNamespace(tool_calls=[], content="{}", model_id="test-model")
    adapter.client.generate = AsyncMock(side_effect=[*search_responses, phase_response])

    __import__("asyncio").run(adapter._call(
        "OBSERVE", {}, "submit_observation", {"type": "object"}, phase="observe",
    ))

    assert runtime.execute.await_count == 3
    assert adapter._last_skill_events[-1]["error_type"] == "SkillBudgetExceeded"
