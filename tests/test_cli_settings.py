from __future__ import annotations

import json

import pytest

from toe_dac.cli_settings import (
    app_home_dir,
    credentials_dir,
    default_data_dir,
    default_log_dir,
    enabled_models,
    model_config_path,
    resolve_model,
    resolve_thread,
)
from toe_dac.service import TDService


def test_model_resolution_prefers_explicit_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TOE_DAC_MODEL", raising=False)
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [
        {"id": "slow", "enabled": True},
        {"id": "fast", "enabled": True, "default": True},
        {"id": "off", "enabled": False},
    ]}))
    assert [item["id"] for item in enabled_models(path)] == ["slow", "fast"]
    assert resolve_model(path) == "fast"
    assert resolve_model(path, "slow") == "slow"
    with pytest.raises(ValueError, match="missing or disabled"):
        resolve_model(path, "off")


def test_thread_resolution_uses_latest_updated_thread(repository):
    TDService.create(repository, "ut_old")
    TDService.create(repository, "ut_latest")
    assert resolve_thread(repository) == "ut_latest"
    assert resolve_thread(repository, "ut_explicit") == "ut_explicit"


def test_installed_config_uses_xdg_but_runtime_uses_td_agent_home(tmp_path, monkeypatch):
    work = tmp_path / "outside-project"
    work.mkdir()
    config_home = tmp_path / "config"
    runtime_home = tmp_path / ".td-agent"
    installed_config = config_home / "td-agent" / "models.json"
    installed_config.parent.mkdir(parents=True)
    installed_config.write_text('{"models": []}')
    monkeypatch.chdir(work)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("TD_AGENT_HOME", str(runtime_home))
    monkeypatch.delenv("TOE_DAC_MODEL_CONFIG", raising=False)

    assert model_config_path() == installed_config
    assert app_home_dir() == runtime_home
    assert default_data_dir() == runtime_home / "data"
    assert default_log_dir() == runtime_home / "logs"
    assert credentials_dir() == runtime_home / "credentials"


def test_user_model_config_wins_even_inside_source_checkout(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.joinpath("config").mkdir(parents=True)
    source.joinpath("config/models.json").write_text('{"models": [{"id": "source"}]}')
    config_home = tmp_path / "config-home"
    installed = config_home / "td-agent" / "models.json"
    installed.parent.mkdir(parents=True)
    installed.write_text('{"models": [{"id": "installed"}]}')
    monkeypatch.chdir(source)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("TOE_DAC_MODEL_CONFIG", raising=False)

    assert model_config_path() == installed
