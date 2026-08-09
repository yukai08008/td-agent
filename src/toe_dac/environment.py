from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


ENV_FILES_LOW_TO_HIGH = (".env.example", ".env", ".env.local")


def find_project_root(start: str | Path | None = None) -> Path:
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current


def load_environment(
    root: str | Path | None = None,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load project env files without allowing them to replace process env.

    Effective precedence is:
    process environment > .env.local > .env > .env.example.
    """

    project_root = find_project_root(root)
    file_values: dict[str, str] = {}
    for filename in ENV_FILES_LOW_TO_HIGH:
        path = project_root / filename
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                file_values[key] = value

    process_values = dict(os.environ if process_environment is None else process_environment)
    effective = {**file_values, **process_values}
    if process_environment is None:
        for key, value in file_values.items():
            os.environ.setdefault(key, value)
    return effective
