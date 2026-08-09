from __future__ import annotations

import json

import pytest

from toe_dac.cli_settings import enabled_models, resolve_model, resolve_thread
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
