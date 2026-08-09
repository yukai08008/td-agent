from __future__ import annotations

from unittest.mock import AsyncMock
from pathlib import Path

from toe_dac.llm.http_transport import HTTPResponseError
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
