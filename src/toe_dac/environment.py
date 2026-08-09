from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping


ENV_FILES_LOW_TO_HIGH = (".env.example", ".env", ".env.local")
ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_ASSIGNMENT.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = str(json.loads(value))
            except json.JSONDecodeError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


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
        file_values.update(dotenv_values(path))

    process_values = dict(os.environ if process_environment is None else process_environment)
    effective = {**file_values, **process_values}
    if process_environment is None:
        for key, value in file_values.items():
            os.environ.setdefault(key, value)
    return effective
