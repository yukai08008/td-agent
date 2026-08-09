from __future__ import annotations

import json

from rich.console import Console

from toe_dac.chat_ui import ChatUI
from toe_dac.conversation import ConversationController


def _ui(repository) -> ChatUI:
    controller = ConversationController.open(repository, object(), "ut_chat_ui")
    return ChatUI(controller, "fake-model", Console(file=None, force_terminal=False))


def test_prompt_reuses_session_scoped_history(repository, monkeypatch):
    ui = _ui(repository)
    captured = {}

    class FakeQuestion:
        def ask(self):
            return "/status"

    def fake_text(*args, **kwargs):
        captured.update(kwargs)
        return FakeQuestion()

    monkeypatch.setattr("toe_dac.chat_ui.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("toe_dac.chat_ui.questionary.text", fake_text)

    assert ui._prompt() == "/status"
    assert captured["history"] is ui._input_history
    assert ui.controller.service.context["session_id"] in str(ui._input_history.filename)


def test_evidence_command_only_opens_canonical_trace_directory_without_writes(repository, monkeypatch):
    ui = _ui(repository)
    context = ui.controller.service.context
    repository.record_message(context, "user", "测试消息")
    repository.record_operation(context, "test_operation", "succeeded", evidence={"ok": True})
    directory = repository.session_evidence_dir(context)
    tracked_before = {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in directory.iterdir() if path.is_file()
    }
    opened = []
    monkeypatch.setattr(ui, "_open_directory", opened.append)

    assert ui._handle_command("/evidence") is True

    assert opened[0] == directory
    assert json.loads((directory / "session.json").read_text())["session_id"] == context["session_id"]
    assert "测试消息" in (directory / "messages.jsonl").read_text()
    assert '"ok": true' in (directory / "evidence.jsonl").read_text()
    assert not list(directory.glob("op_*.json"))

    tracked_after = {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in directory.iterdir() if path.is_file()
    }
    assert tracked_after == tracked_before


def test_read_only_ui_blocks_mutation_and_opens_artifacts(repository, monkeypatch):
    controller = ConversationController.open(repository, object(), "ut_read_only_ui")
    artifact_ref = repository.write_artifact(controller.service.context, "result.md", "done\n")
    controller.service.context["artifacts"].append(artifact_ref)
    repository.save(controller.service.context)
    controller.service.cancel()
    readonly = ConversationController.open(
        repository,
        object(),
        "ut_read_only_ui",
        session_id=controller.service.context["session_id"],
    )
    ui = ChatUI(readonly, "fake-model", Console(file=None, force_terminal=False))
    opened = []
    monkeypatch.setattr(ui, "_open_directory", opened.append)

    assert ui._handle_command("/replan 修改计划") is True
    assert readonly.service.state.value == "cancelled"
    assert ui._handle_command("/artifacts") is True
    assert opened == [repository.artifact_dir(readonly.service.context)]


def test_read_only_unknown_command_suggests_instead_of_reporting_mutation(repository, capsys):
    controller = ConversationController.open(repository, object(), "ut_command_suggestion")
    controller.service.cancel()
    readonly = ConversationController.open(repository, object(), "ut_command_suggestion")
    ui = ChatUI(readonly, "fake-model")

    assert ui._handle_command("/arfifacts") is True

    output = capsys.readouterr().out
    assert "未知命令" in output
    assert "/artifacts" in output
    assert "修改状态" not in output


def test_bare_show_is_read_only_summary(repository, capsys):
    controller = ConversationController.open(repository, object(), "ut_bare_show")
    controller.service.cancel()
    readonly = ConversationController.open(repository, object(), "ut_bare_show")
    ui = ChatUI(readonly, "fake-model")

    assert ui._handle_command("/show") is True

    output = capsys.readouterr().out
    assert "summary" in output
    assert "cancelled" in output
