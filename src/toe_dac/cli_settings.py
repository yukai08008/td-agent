from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .storage import TDRepository


DEFAULT_MODEL_CONFIG = "config/models.json"


def model_config_path(value: str | None = None) -> Path:
    return Path(value or os.environ.get("TOE_DAC_MODEL_CONFIG", DEFAULT_MODEL_CONFIG))


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
