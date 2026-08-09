from __future__ import annotations

import json

from toe_dac import update_check


def test_update_check_detects_and_caches_newer_version(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("TOE_DAC_UPDATE_CHECK", "true")
    monkeypatch.setenv("TOE_DAC_UPDATE_CHECK_INTERVAL", "86400")
    monkeypatch.setattr(update_check, "_fetch_remote_version", lambda timeout: "0.3.0")

    result = update_check.check_for_update("0.2.0", now=1000)
    assert result == {"current_version": "0.2.0", "remote_version": "0.3.0"}
    cache = json.loads((tmp_path / "td-agent" / "update-check.json").read_text())
    assert cache["remote_version"] == "0.3.0"

    monkeypatch.setattr(update_check, "_fetch_remote_version", lambda timeout: (_ for _ in ()).throw(AssertionError()))
    assert update_check.check_for_update("0.2.0", now=1001) == result


def test_update_check_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("TOE_DAC_UPDATE_CHECK", "false")
    monkeypatch.setattr(update_check, "_fetch_remote_version", lambda timeout: (_ for _ in ()).throw(AssertionError()))
    assert update_check.check_for_update("0.1.0") is None


def test_update_check_ignores_equal_older_and_non_numeric_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("TOE_DAC_UPDATE_CHECK_INTERVAL", "0")
    for remote in ("0.2.0", "0.1.9", "main"):
        monkeypatch.setattr(update_check, "_fetch_remote_version", lambda timeout, value=remote: value)
        assert update_check.check_for_update("0.2.0") is None


def test_update_notification_links_release_notes_before_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(
        update_check,
        "check_for_update",
        lambda current_version: {"current_version": current_version, "remote_version": "0.3.0"},
    )

    update_check.notify_if_update_available("0.2.0")

    message = capsys.readouterr().err
    assert "toe-dac changelog --version 0.3.0" in message
    assert "toe-dac upgrade" in message
