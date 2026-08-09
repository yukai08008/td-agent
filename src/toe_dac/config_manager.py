from __future__ import annotations

import json
import os
import re
from getpass import getpass
from pathlib import Path
from typing import Any, Mapping

import questionary
from rich.console import Console
from rich.table import Table

from .cli_settings import enabled_models


ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def environment_root(config_path: str | Path) -> Path:
    path = Path(config_path).expanduser().resolve()
    return path.parent.parent if path.parent.name == "config" else path.parent


def local_env_path(config_path: str | Path) -> Path:
    return environment_root(config_path) / ".env.local"


def model_statuses(
    config_path: str | Path,
    environment: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    values = os.environ if environment is None else environment
    rows: list[dict[str, Any]] = []
    selected = values.get("TOE_DAC_MODEL", "")
    for model in enabled_models(config_path):
        key_env = str(model.get("apiKeyEnv", "")).strip()
        rows.append({
            **model,
            "api_key_env": key_env,
            "configured": bool(key_env and values.get(key_env)),
            "selected": model.get("id") == selected,
        })
    return rows


def resolve_ready_model(
    config_path: str | Path,
    requested: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if environment is None else environment
    rows = model_statuses(config_path, values)
    selected = requested or values.get("TOE_DAC_MODEL")
    if selected:
        matches = [row for row in rows if row.get("id") == selected]
        if not matches:
            raise ValueError(f"model is missing or disabled: {selected}")
        row = matches[0]
        if not row["configured"]:
            raise ValueError(f"model {selected} is missing API key {row['api_key_env']}")
        return str(selected)

    configured = [row for row in rows if row["configured"]]
    defaults = [row for row in configured if row.get("default", False)]
    candidates = defaults or configured
    if candidates:
        return str(candidates[0]["id"])
    raise ValueError("no usable model is configured")


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("configuration values cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_local_value(config_path: str | Path, key: str, value: str) -> Path:
    if not ENV_KEY.fullmatch(key):
        raise ValueError(f"invalid environment key: {key}")
    path = local_env_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={_quote_env(value)}"
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = replacement
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    os.environ[key] = value
    return path


def configure_model(config_path: str | Path, model_id: str, api_key: str | None = None) -> str:
    rows = model_statuses(config_path)
    matches = [row for row in rows if row.get("id") == model_id]
    if not matches:
        raise ValueError(f"model is missing or disabled: {model_id}")
    row = matches[0]
    key_env = str(row["api_key_env"])
    if not key_env:
        raise ValueError(f"model {model_id} does not declare apiKeyEnv")
    if api_key is not None:
        if not api_key.strip():
            raise ValueError("API key cannot be empty")
        set_local_value(config_path, key_env, api_key.strip())
    elif not os.environ.get(key_env):
        raise ValueError(f"model {model_id} is missing API key {key_env}")
    set_local_value(config_path, "TOE_DAC_MODEL", model_id)
    return model_id


def print_config_status(config_path: str | Path) -> None:
    rows = model_statuses(config_path)
    table = Table(title="TD Agent models")
    table.add_column("Model")
    table.add_column("Vendor")
    table.add_column("API key env")
    table.add_column("Status")
    for row in rows:
        status = "ready" if row["configured"] else "missing key"
        if row["selected"]:
            status += ", default"
        table.add_row(
            str(row.get("id", "")),
            str(row.get("vendor", "")),
            str(row["api_key_env"]),
            status,
        )
    Console().print(table)
    print(f"models: {Path(config_path).expanduser().resolve()}")
    print(f"local:  {local_env_path(config_path)}")


def _select_model(config_path: str | Path, message: str, initial: str | None = None) -> str | None:
    rows = model_statuses(config_path)
    choices = [
        questionary.Choice(
            title=f"{row['id']}  ({'ready' if row['configured'] else 'missing key'})",
            value=str(row["id"]),
        )
        for row in rows
    ]
    if not choices:
        raise ValueError(f"no enabled models in {config_path}")
    return questionary.select(message, choices=choices, default=initial).ask()


def configure_interactively(config_path: str | Path, initial: str | None = None) -> str | None:
    model_id = _select_model(config_path, "Choose a model", initial)
    if not model_id:
        return None
    row = next(row for row in model_statuses(config_path) if row["id"] == model_id)
    api_key = None
    if not row["configured"]:
        api_key = getpass(f"{row['api_key_env']}: ").strip()
    configured = configure_model(config_path, model_id, api_key)
    print(f"Configured model: {configured}")
    print(f"Saved machine-local settings to {local_env_path(config_path)}")
    return configured


def ensure_model_ready(
    config_path: str | Path,
    requested: str | None = None,
    *,
    interactive: bool = True,
) -> str:
    try:
        return resolve_ready_model(config_path, requested)
    except ValueError as exc:
        if not interactive:
            raise ValueError(f"{exc}; run `toe-dac config`") from exc
        print(f"Model configuration required: {exc}")
        configured = configure_interactively(config_path, requested)
        if not configured:
            raise ValueError("model configuration cancelled")
        return configured


def run_config_manager(config_path: str | Path) -> None:
    print_config_status(config_path)
    action = questionary.select(
        "Configuration action",
        choices=[
            questionary.Choice("Configure API key and make default", value="configure"),
            questionary.Choice("Change default to an already configured model", value="default"),
            questionary.Choice("Exit", value="exit"),
        ],
    ).ask()
    if action == "configure":
        configure_interactively(config_path)
    elif action == "default":
        ready = [row for row in model_statuses(config_path) if row["configured"]]
        if not ready:
            raise ValueError("no configured model; configure an API key first")
        selected = questionary.select(
            "Default model",
            choices=[questionary.Choice(str(row["id"]), value=str(row["id"])) for row in ready],
        ).ask()
        if selected:
            configure_model(config_path, selected)
            print(f"Default model: {selected}")
