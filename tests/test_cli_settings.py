from __future__ import annotations

import json

import pytest

from toe_dac.cli_settings import default_data_dir, enabled_models, model_config_path, resolve_model, resolve_thread
from toe_dac.service import TDService


def test_model_resolution_prefers_explicit_default(tmp_path):
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


def test_installed_config_and_data_fall_back_to_xdg_directories(tmp_path, monkeypatch):
    work = tmp_path / "outside-project"
    work.mkdir()
    config_home = tmp_path / "config"
    data_home = tmp_path / "share"
    installed_config = config_home / "td-agent" / "models.json"
    installed_config.parent.mkdir(parents=True)
    installed_config.write_text('{"models": []}')
    monkeypatch.chdir(work)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("TOE_DAC_MODEL_CONFIG", raising=False)

    assert model_config_path() == installed_config
    assert default_data_dir() == data_home / "td-agent"
