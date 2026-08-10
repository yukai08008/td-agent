from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .storage import TDRepository


DEFAULT_MODEL_CONFIG = "config/models.json"
APP_NAME = "td-agent"


def app_home_dir() -> Path:
    return Path(os.environ.get("TD_AGENT_HOME", Path.home() / ".td-agent")).expanduser()


def user_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def default_data_dir() -> Path:
    return app_home_dir() / "data"


def default_log_dir() -> Path:
    return Path(os.environ.get("TOE_DAC_LOG_DIR", app_home_dir() / "logs")).expanduser()


def credentials_dir() -> Path:
    return app_home_dir() / "credentials"


def model_config_path(value: str | None = None) -> Path:
    requested = value or os.environ.get("TOE_DAC_MODEL_CONFIG")
    if requested:
        return Path(requested).expanduser()
    installed_config = user_config_dir() / "models.json"
    if installed_config.exists():
        return installed_config
    project_config = Path(DEFAULT_MODEL_CONFIG)
    if project_config.exists():
        return project_config
    return project_config


def enabled_models(path: str | Path) -> list[dict[str, Any]]:
    config_path = Path(path)
    if not config_path.exists():
        return []
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return [model for model in data.get("models", []) if model.get("enabled", False)]


def resolve_model(path: str | Path, requested: str | None = None) -> str:
    requested = requested or os.environ.get("TOE_DAC_MODEL")
    models = enabled_models(path)
    if requested:
        if any(model.get("id") == requested for model in models):
            return requested
        raise ValueError(f"model is missing or disabled: {requested}")
    defaults = [model for model in models if model.get("default", False)]
    candidate = (defaults or models)
    if candidate:
        return str(candidate[0]["id"])
    raise ValueError(
        f"no enabled model in {path}; add one or pass --model with a valid local configuration"
    )


def resolve_thread(repository: TDRepository, requested: str | None = None) -> str:
    requested = requested or os.environ.get("TOE_DAC_THREAD")
    if requested:
        return requested
    threads = repository.list_threads()
    if not threads:
        return "ut_default"
    return max(threads, key=lambda item: item.get("updated_at", ""))["user_thread_id"]
