from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from toe_dac.llm.http_transport import HTTPResponseError
from toe_dac.runtime_content import LoadedSkill, RuntimeContentLoader, initialize_runtime_content
from toe_dac.skill_runtime import SkillToolRuntime


def test_alex_serp_success(monkeypatch):
    post = AsyncMock(return_value={
        "results": [{"title": "上海天气", "description": "晴", "link": "https://example.com"}],
        "count": 1,
    })
    monkeypatch.setattr("toe_dac.skill_runtime.post_json", post)

    result = __import__("asyncio").run(SkillToolRuntime().execute(
        "alex_serp_search", {"query": "上海天气"},
    ))

    assert result.output["ok"] is True
    assert result.event["status"] == "succeeded"
    assert result.event["attempt_count"] == 1


def test_alex_serp_tool_is_only_exposed_during_observe():
    runtime = SkillToolRuntime()
    assert runtime.tool_definitions({"alex-serp"}, "observe")
    assert runtime.tool_definitions({"alex-serp"}, "estimate") == []


def test_agent_browser_tool_is_only_exposed_during_observe():
    runtime = SkillToolRuntime()
    definitions = runtime.tool_definitions({"agent-browser"}, "observe")
    assert definitions[0]["function"]["name"] == "agent_browser_observe"
    assert runtime.tool_definitions({"agent-browser"}, "decide") == []


def test_skill_script_tool_is_exposed_only_after_loading_skill_with_scripts(tmp_path):
    skill_root = tmp_path / "demo"
    skill_root.joinpath("scripts").mkdir(parents=True)
    runtime = SkillToolRuntime()

    assert runtime.tool_definitions({"demo"}, "observe") == []

    runtime.configure_skills((LoadedSkill(
        "demo", "demo", "body", (), (), str(skill_root), ("scripts/check.py",),
    ),))
    definitions = runtime.tool_definitions({"demo"}, "observe")

    assert definitions[0]["function"]["name"] == "run_skill_script"
    assert definitions[0]["function"]["parameters"]["properties"]["skill_name"]["enum"] == ["demo"]


def test_skill_script_tool_honors_index_phase_scope(tmp_path):
    skill_root = tmp_path / "demo"
    skill_root.joinpath("scripts").mkdir(parents=True)
    runtime = SkillToolRuntime()
    runtime.configure_skills((LoadedSkill(
        "demo", "demo", "body", (), ("observe",), str(skill_root), ("scripts/check.py",),
    ),))

    assert runtime.tool_definitions({"demo"}, "observe")[0]["function"]["name"] == "run_skill_script"
    assert runtime.tool_definitions({"demo"}, "act") == []


def test_run_skill_script_executes_python_with_argument_array_and_records_evidence(tmp_path):
    skill_root = tmp_path / "demo"
    script = skill_root / "scripts" / "inspect.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import os, sys\nprint(os.getcwd())\nprint('|'.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    runtime = SkillToolRuntime()
    runtime.configure_skills((LoadedSkill(
        "demo", "demo", "body", (), (), str(skill_root), ("scripts/inspect.py",),
    ),))

    result = __import__("asyncio").run(runtime.execute("run_skill_script", {
        "skill_name": "demo",
        "script": "scripts/inspect.py",
        "arguments": ["hello world", "$(not-a-shell)"],
        "timeout": 10,
    }))

    assert result.output["ok"] is True
    assert result.output["exit_code"] == 0
    assert str(skill_root) in result.output["stdout"]
    assert "hello world|$(not-a-shell)" in result.output["stdout"]
    assert result.event["script_sha256"]
    assert result.event["evidence"]["stdout"] == result.output["stdout"]


def test_run_skill_script_rejects_unloaded_and_escaping_paths(tmp_path):
    skill_root = tmp_path / "demo"
    skill_root.joinpath("scripts").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')", encoding="utf-8")
    runtime = SkillToolRuntime()

    unloaded = __import__("asyncio").run(runtime.execute("run_skill_script", {
        "skill_name": "demo", "script": "scripts/check.py",
    }))
    assert unloaded.output["ok"] is False
    assert unloaded.output["error"] == "skill is not loaded"

    runtime.configure_skills((LoadedSkill(
        "demo", "demo", "body", (), (), str(skill_root), (),
    ),))
    escaped = __import__("asyncio").run(runtime.execute("run_skill_script", {
        "skill_name": "demo", "script": "scripts/../../outside.py",
    }))
    assert escaped.output["ok"] is False
    assert "escapes" in escaped.output["error"]


def test_run_skill_script_reports_nonzero_exit(tmp_path):
    skill_root = tmp_path / "demo"
    script = skill_root / "scripts" / "fail.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nprint('bad', file=sys.stderr)\nraise SystemExit(7)\n", encoding="utf-8")
    runtime = SkillToolRuntime()
    runtime.configure_skills((LoadedSkill(
        "demo", "demo", "body", (), (), str(skill_root), ("scripts/fail.py",),
    ),))

    result = __import__("asyncio").run(runtime.execute("run_skill_script", {
        "skill_name": "demo", "script": "scripts/fail.py",
    }))

    assert result.output["ok"] is False
    assert result.output["exit_code"] == 7
    assert "bad" in result.output["stderr"]
    assert result.event["status"] == "failed"


def test_run_cmd_skill_starts_persistent_background_job_and_reads_incremental_output(tmp_path):
    initialize_runtime_content(tmp_path)
    snapshot = RuntimeContentLoader(tmp_path).load().activate(["run-cmd"])
    runtime = SkillToolRuntime()
    session_dir = tmp_path / "session"
    runtime.configure_evidence(session_dir / "screenshots", "sess_test")
    runtime.configure_skills(snapshot.skills)

    async def scenario():
        started = await runtime.execute("run_skill_script", {
            "skill_name": "run-cmd",
            "script": "scripts/run_cmd.py",
            "arguments": ["start", "--command", "sleep 0.15; printf async-ok"],
            "timeout": 5,
        })
        start_payload = json.loads(started.output["stdout"])
        assert started.output["ok"] is True
        assert start_payload["status"] == "running"
        job_id = start_payload["job_id"]

        await __import__("asyncio").sleep(0.3)
        completed = await runtime.execute("run_skill_script", {
            "skill_name": "run-cmd",
            "script": "scripts/run_cmd.py",
            "arguments": ["status", "--job-id", job_id],
            "timeout": 5,
        })
        status_payload = json.loads(completed.output["stdout"])
        assert completed.output["ok"] is True
        assert status_payload["status"] == "completed"
        assert status_payload["exit_code"] == 0
        assert status_payload["stdout"] == "async-ok"
        assert status_payload["stdout_offset"] == len("async-ok")
        assert (session_dir / "skill-jobs" / "run-cmd" / job_id / "result.json").is_file()

    __import__("asyncio").run(scenario())


def test_run_cmd_skill_can_terminate_background_process_group(tmp_path):
    initialize_runtime_content(tmp_path)
    snapshot = RuntimeContentLoader(tmp_path).load().activate(["run-cmd"])
    runtime = SkillToolRuntime()
    runtime.configure_evidence(tmp_path / "session" / "screenshots", "sess_test")
    runtime.configure_skills(snapshot.skills)

    async def scenario():
        started = await runtime.execute("run_skill_script", {
            "skill_name": "run-cmd",
            "script": "scripts/run_cmd.py",
            "arguments": ["start", "--command", "sleep 30"],
        })
        job_id = json.loads(started.output["stdout"])["job_id"]
        killed = await runtime.execute("run_skill_script", {
            "skill_name": "run-cmd",
            "script": "scripts/run_cmd.py",
            "arguments": ["kill", "--job-id", job_id],
        })
        killed_payload = json.loads(killed.output["stdout"])
        assert killed.output["ok"] is True
        assert killed_payload["status"] == "killed"

    __import__("asyncio").run(scenario())


def test_alex_serp_retries_503_then_records_failure(monkeypatch):
    post = AsyncMock(side_effect=[
        HTTPResponseError(503, "busy"),
        HTTPResponseError(503, "busy"),
        HTTPResponseError(503, "busy"),
    ])
    sleep = AsyncMock()
    monkeypatch.setattr("toe_dac.skill_runtime.post_json", post)
    monkeypatch.setattr("toe_dac.skill_runtime.asyncio.sleep", sleep)

    result = __import__("asyncio").run(SkillToolRuntime().execute(
        "alex_serp_search", {"query": "上海天气"},
    ))

    assert result.output["ok"] is False
    assert result.event["status"] == "failed"
    assert result.event["attempt_count"] == 3
    assert len(result.event["attempts"]) == 3
    assert sleep.await_count == 2


def test_agent_browser_observe_registers_real_screenshot(tmp_path, monkeypatch):
    runtime = SkillToolRuntime()
    screenshot_dir = tmp_path / "view" / "screenshots"
    runtime.configure_evidence(screenshot_dir, "ss_test")
    monkeypatch.setattr("toe_dac.skill_runtime.shutil.which", lambda _: "/usr/bin/agent-browser")

    async def fake_run(binary, command, *arguments):
        if command == "screenshot":
            Path(arguments[0]).write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        return "page snapshot" if command == "snapshot" else "ok"

    monkeypatch.setattr(runtime, "_run_browser", fake_run)

    result = __import__("asyncio").run(runtime.execute(
        "agent_browser_observe",
        {"url": "https://example.com/weather", "purpose": "天气事实证据"},
    ))

    assert result.output["ok"] is True
    assert Path(result.output["screenshot_ref"]).is_file()
    assert result.output["page_title"] == "ok"
    assert result.output["body_text"] == "ok"
    assert result.output["screenshot_size_bytes"] > 8
    assert result.output["screenshot_format"] == "png"
    assert result.event["evidence"]["screenshot_ref"] == result.output["screenshot_ref"]
    assert result.event["skill"] == "agent-browser"
