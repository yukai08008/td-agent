from __future__ import annotations

import json
import stat

import pytest

from toe_dac.config_manager import (
    configure_model,
    ensure_model_ready,
    local_env_path,
    model_statuses,
    resolve_ready_model,
    set_local_value,
)


def model_config(tmp_path):
    path = tmp_path / "config" / "models.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "models": [
            {
                "id": "default-missing",
                "vendor": "Example",
                "enabled": True,
                "default": True,
                "apiKeyEnv": "MISSING_KEY",
            },
            {
                "id": "ready",
                "vendor": "Example",
                "enabled": True,
                "apiKeyEnv": "READY_KEY",
            },
        ],
    }), encoding="utf-8")
    return path


def test_status_and_resolution_require_a_real_api_key(tmp_path):
    path = model_config(tmp_path)
    environment = {"READY_KEY": "secret"}

    rows = model_statuses(path, environment)

    assert [row["configured"] for row in rows] == [False, True]
    assert resolve_ready_model(path, environment=environment) == "ready"
    with pytest.raises(ValueError, match="missing API key MISSING_KEY"):
        resolve_ready_model(path, "default-missing", environment)


def test_configure_model_writes_machine_local_secret_and_default(tmp_path, monkeypatch):
    path = model_config(tmp_path)
    monkeypatch.setenv("READY_KEY", "")
    monkeypatch.setenv("TOE_DAC_MODEL", "")

    assert configure_model(path, "ready", "sk-local-test") == "ready"

    env_path = local_env_path(path)
    content = env_path.read_text(encoding="utf-8")
    assert 'READY_KEY="sk-local-test"' in content
    assert 'TOE_DAC_MODEL="ready"' in content
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert resolve_ready_model(path) == "ready"


def test_set_local_value_preserves_other_settings_and_replaces_key(tmp_path, monkeypatch):
    path = model_config(tmp_path)
    monkeypatch.setenv("READY_KEY", "")
    env_path = local_env_path(path)
    env_path.write_text("OTHER=value\nREADY_KEY=old\n", encoding="utf-8")

    set_local_value(path, "READY_KEY", "new-value")

    content = env_path.read_text(encoding="utf-8")
    assert "OTHER=value" in content
    assert content.count("READY_KEY=") == 1
    assert 'READY_KEY="new-value"' in content


def test_noninteractive_startup_points_to_config_command(tmp_path, monkeypatch):
    path = model_config(tmp_path)
    monkeypatch.setenv("READY_KEY", "")
    monkeypatch.setenv("MISSING_KEY", "")
    monkeypatch.setenv("TOE_DAC_MODEL", "")
    with pytest.raises(ValueError, match=r"run `toe-dac config`"):
        ensure_model_ready(path, interactive=False)
