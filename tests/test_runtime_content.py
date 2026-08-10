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
    assert (tmp_path / "skills" / "run-cmd" / "SKILL.md").read_text().startswith("---\n")
    assert (tmp_path / "skills" / "run-cmd" / "scripts" / "run_cmd.py").is_file()
    assert (tmp_path / "persona" / "blue" / "system.md").is_file()
    assert (tmp_path / "persona" / "green" / "system.md").is_file()
    assert json.loads((tmp_path / "persona" / "active.json").read_text())["active"] == "blue"


def test_runtime_initialization_never_overwrites_user_content(tmp_path):
    initialize_runtime_content(tmp_path)
    prompt = tmp_path / "persona" / "blue" / "system.md"
    prompt.write_text("custom blue", encoding="utf-8")

    initialize_runtime_content(tmp_path)

    assert prompt.read_text(encoding="utf-8") == "custom blue"


def test_runtime_upgrade_merges_new_skill_entry_without_overwriting_existing_index(tmp_path):
    initialize_runtime_content(tmp_path)
    index_path = tmp_path / "skills" / "index.md"
    old_index = index_path.read_text(encoding="utf-8").split("\n## run-cmd", 1)[0]
    old_index = old_index.replace(
        "Only skills with a concrete, current purpose belong in this index.",
        "USER CUSTOM INDEX NOTE",
    )
    index_path.write_text(old_index.rstrip() + "\n", encoding="utf-8")

    changed = initialize_runtime_content(tmp_path)
    upgraded = index_path.read_text(encoding="utf-8")

    assert index_path in changed
    assert "USER CUSTOM INDEX NOTE" in upgraded
    assert upgraded.count("## run-cmd") == 1
    assert "- Path: run-cmd/SKILL.md" in upgraded


def test_loader_loads_only_index_then_activates_selected_skill(tmp_path):
    initialize_runtime_content(tmp_path)
    blue = RuntimeContentLoader(tmp_path).load()
    assert blue.persona_slot == "blue"
    assert blue.skills == ()
    assert [skill.name for skill in blue.available_skills] == [
        "toe-dac-control", "agent-browser", "alex-serp", "run-cmd",
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


def test_loaded_skill_lists_resources_without_reading_them_into_context(tmp_path):
    initialize_runtime_content(tmp_path)
    skill_root = tmp_path / "skills" / "agent-browser"
    script = skill_root / "scripts" / "inspect.py"
    reference = skill_root / "references" / "commands.md"
    script.parent.mkdir()
    reference.parent.mkdir()
    script.write_text("SECRET_SCRIPT_BODY", encoding="utf-8")
    reference.write_text("SECRET_REFERENCE_BODY", encoding="utf-8")

    activated = RuntimeContentLoader(tmp_path).load().activate(["agent-browser"])
    rendered = activated.render("OBSERVE", phase="observe")

    assert activated.skills[0].root == str(skill_root.resolve())
    assert activated.skills[0].resources == (
        "references/commands.md",
        "scripts/inspect.py",
    )
    assert "`references/commands.md`" in rendered
    assert "`scripts/inspect.py`" in rendered
    assert "SECRET_SCRIPT_BODY" not in rendered
    assert "SECRET_REFERENCE_BODY" not in rendered


def test_loader_accepts_claude_skill_yaml_metadata_and_folded_description(tmp_path):
    initialize_runtime_content(tmp_path)
    skill_path = tmp_path / "skills" / "agent-browser" / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: agent-browser\n"
        "description: >\n"
        "  Inspect interactive web pages and capture evidence.\n"
        "  Use for browser-driven observation.\n"
        "license: Apache-2.0\n"
        "metadata:\n"
        "  author: toe-dac\n"
        "  version: '1.0'\n"
        "allowed-tools: Bash(agent-browser:*) Read\n"
        "---\n\n"
        "# Browser skill\n",
        encoding="utf-8",
    )

    snapshot = RuntimeContentLoader(tmp_path).load().activate(["agent-browser"])

    assert snapshot.skills[0].description == (
        "Inspect interactive web pages and capture evidence. "
        "Use for browser-driven observation."
    )
    assert "# Browser skill" in snapshot.render("OBSERVE", phase="observe")


def test_runtime_initialization_copies_optional_skill_resources(tmp_path, monkeypatch):
    source = tmp_path / "source-runtime"
    source.joinpath("skills/demo/scripts").mkdir(parents=True)
    source.joinpath("skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nRun scripts/check.py\n",
        encoding="utf-8",
    )
    source.joinpath("skills/demo/scripts/check.py").write_text("print('ok')\n", encoding="utf-8")
    destination = tmp_path / "installed-runtime"
    monkeypatch.setattr("toe_dac.runtime_content._runtime_resource_root", lambda: source)

    initialize_runtime_content(destination)

    assert destination.joinpath("skills/demo/SKILL.md").is_file()
    assert destination.joinpath("skills/demo/scripts/check.py").read_text() == "print('ok')\n"


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
    skill_dir.joinpath("scripts").mkdir()
    skill_dir.joinpath("scripts/check.py").write_text("print('ok')\n", encoding="utf-8")
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
    second_tools = adapter.client.generate.await_args_list[1].kwargs["tools"]
    assert [tool["function"]["name"] for tool in second_tools] == [
        "submit_target", "run_skill_script",
    ]


def test_adapter_loads_skill_runs_bundled_script_then_submits_phase(tmp_path):
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
    skill_dir.joinpath("scripts").mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: Run a demo inspection.\n---\n\n"
        "Run `scripts/inspect.py`.\n",
        encoding="utf-8",
    )
    skill_dir.joinpath("scripts/inspect.py").write_text(
        "import sys\nprint('observed:' + sys.argv[1])\n",
        encoding="utf-8",
    )
    snapshot = RuntimePromptSnapshot(
        persona_slot="blue",
        persona_revision=1,
        persona="GLOBAL",
        skill_index="## demo\n\n- Description: Run a demo inspection.",
        available_skills=(SkillIndexEntry("demo", "Run demo", "demo/SKILL.md", (), ("observe",)),),
        skills_root=str(skills_root),
    )
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot)
    load_response = SimpleNamespace(
        tool_calls=[SimpleNamespace(
            id="load-1",
            function={"name": "load_skill", "arguments": '{"names":["demo"]}'},
        )],
        content=None,
        model_id="test-model",
    )
    script_response = SimpleNamespace(
        tool_calls=[SimpleNamespace(
            id="script-1",
            function={
                "name": "run_skill_script",
                "arguments": json.dumps({
                    "skill_name": "demo",
                    "script": "scripts/inspect.py",
                    "arguments": ["server"],
                }),
            },
        )],
        content=None,
        model_id="test-model",
    )
    phase_response = SimpleNamespace(
        tool_calls=[SimpleNamespace(
            id="submit-1",
            function={"name": "submit_observation", "arguments": "{}"},
        )],
        content=None,
        model_id="test-model",
    )
    adapter.client.generate = AsyncMock(side_effect=[load_response, script_response, phase_response])

    result = __import__("asyncio").run(adapter._call(
        "OBSERVE", {}, "submit_observation", {"type": "object"}, phase="observe",
    ))

    assert result is phase_response
    assert adapter.client.generate.await_count == 3
    final_messages = adapter.client.generate.await_args_list[2].args[0]
    script_tool_result = final_messages[-1]
    assert script_tool_result.name == "run_skill_script"
    assert "observed:server" in script_tool_result.content
    assert adapter._last_skill_events[-1]["status"] == "succeeded"
    assert adapter._last_skill_events[-1]["script_sha256"]


@pytest.mark.parametrize("phase", ["target", "observe", "estimate", "decide", "act", "action_check", "target_check"])
def test_load_skill_is_exposed_in_every_toe_dac_phase(tmp_path, phase):
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
        persona_revision=1,
        persona="GLOBAL",
        available_skills=(SkillIndexEntry("demo", "demo", "demo/SKILL.md", ()),),
    )
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot)
    adapter.client.generate = AsyncMock(return_value=SimpleNamespace(tool_calls=[], content="{}"))

    __import__("asyncio").run(adapter._call(
        "PHASE", {}, "submit_phase", {"type": "object"}, phase=phase,
    ))

    tools = adapter.client.generate.await_args.kwargs["tools"]
    assert [tool["function"]["name"] for tool in tools] == ["submit_phase", "load_skill"]


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
            function={"name": "alex_serp_search", "arguments": json.dumps({"query": f"上海天气 {index}"})},
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


def test_observe_reuses_successful_tool_checkpoint_across_phase_retry(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "test-model", "enabled": True, "apiKeyEnv": "TEST_KEY",
        "url": "https://example.invalid/v1/chat/completions",
    }]}), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TEST_KEY=secret\n", encoding="utf-8")

    class FakeSkillRuntime:
        def __init__(self):
            self.execute = AsyncMock(return_value=SkillToolResult(
                {"ok": True, "results": [{"title": "cached"}], "count": 1},
                {"skill": "alex-serp", "tool": "alex_serp_search", "status": "succeeded"},
            ))

        def tool_definitions(self, active_skills, phase):
            return [{"type": "function", "function": {
                "name": "alex_serp_search", "description": "search", "parameters": {"type": "object"},
            }}]

    runtime = FakeSkillRuntime()
    snapshot = RuntimePromptSnapshot(
        persona_slot="blue", persona_revision=1, persona="GLOBAL",
        skills=(LoadedSkill("alex-serp", "search", "SEARCH", (), ("observe",)),),
    )
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot, skill_runtime=runtime)

    def search_response(call_id):
        return SimpleNamespace(
            tool_calls=[SimpleNamespace(
                id=call_id,
                function={"name": "alex_serp_search", "arguments": '{"query":"same query"}'},
            )], content=None, model_id="test-model",
        )

    phase_response = SimpleNamespace(tool_calls=[], content="{}", model_id="test-model")
    adapter.client.generate = AsyncMock(side_effect=[
        search_response("first"), phase_response,
        search_response("second"), phase_response,
    ])

    __import__("asyncio").run(adapter._call(
        "OBSERVE", {}, "submit_observation", {"type": "object"}, phase="observe",
    ))
    __import__("asyncio").run(adapter._call(
        "OBSERVE", {}, "submit_observation", {"type": "object"}, phase="observe",
    ))

    assert runtime.execute.await_count == 1
    assert adapter._last_skill_events[-1]["checkpoint_reused"] is True


def test_observe_controller_polls_run_cmd_without_model_rounds(tmp_path):
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"models": [{
        "id": "test-model", "enabled": True, "apiKeyEnv": "TEST_KEY",
        "url": "https://example.invalid/v1/chat/completions",
    }]}), encoding="utf-8")
    (tmp_path / ".env.local").write_text("TEST_KEY=secret\n", encoding="utf-8")
    initialize_runtime_content(tmp_path)
    snapshot = RuntimeContentLoader(tmp_path).load().activate(["run-cmd"])
    adapter = TOEDACLLMAdapter(config, "test-model", runtime_snapshot=snapshot)
    adapter.configure_evidence(tmp_path / "session" / "screenshots", "sess_test")
    adapter.skill_runtime.configure_skills(snapshot.skills)
    arguments = {
        "skill_name": "run-cmd",
        "script": "scripts/run_cmd.py",
        "arguments": ["start", "--command", "sleep 0.1; printf controller-ok"],
        "timeout": 5,
    }

    async def scenario():
        started = await adapter.skill_runtime.execute("run_skill_script", arguments)
        return await adapter._settle_observe_job(
            started, arguments, phase="observe", progress_callback=None,
        )

    settled = __import__("asyncio").run(scenario())

    assert settled.output["result"]["status"] == "completed"
    assert settled.output["result"]["stdout"] == "controller-ok"
    assert settled.event["controller_polled"] is True
    assert settled.event["poll_count"] >= 1
