from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


REMOTE_PYPROJECT = "https://raw.githubusercontent.com/yukai08008/td-agent/main/pyproject.toml"
VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _enabled(value: str | None) -> bool:
    return str(value or "true").strip().lower() not in {"0", "false", "no", "off"}


def _version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"(\d+(?:\.\d+)*)", value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "td-agent" / "update-check.json"


def _read_cache(path: Path, now: float, interval: int) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if now - float(data["checked_at"]) < interval:
            return str(data["remote_version"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        pass
    return None


def _fetch_remote_version(timeout: float) -> str | None:
    request = urllib.request.Request(REMOTE_PYPROJECT, headers={"User-Agent": "td-agent-update-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(64_000).decode("utf-8")
    except (OSError, UnicodeError):
        return None
    match = VERSION_PATTERN.search(content)
    return match.group(1) if match else None


def check_for_update(current_version: str, *, now: float | None = None) -> dict[str, Any] | None:
    if not _enabled(os.environ.get("TOE_DAC_UPDATE_CHECK")):
        return None
    try:
        interval = max(0, int(os.environ.get("TOE_DAC_UPDATE_CHECK_INTERVAL", "86400")))
        timeout = max(0.1, float(os.environ.get("TOE_DAC_UPDATE_CHECK_TIMEOUT", "1.5")))
    except ValueError:
        return None

    current_time = time.time() if now is None else now
    path = _cache_path()
    remote_version = _read_cache(path, current_time, interval)
    if remote_version is None:
        remote_version = _fetch_remote_version(timeout)
        if remote_version:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "checked_at": current_time,
                    "remote_version": remote_version,
                }), encoding="utf-8")
            except OSError:
                pass

    current_key, remote_key = _version_key(current_version), _version_key(remote_version or "")
    if current_key and remote_key and remote_key > current_key:
        return {"current_version": current_version, "remote_version": remote_version}
    return None


def notify_if_update_available(current_version: str) -> None:
    update = check_for_update(current_version)
    if not update:
        return
    print(
        f"TD Agent update available: {update['current_version']} → {update['remote_version']}. "
        "Run `toe-dac upgrade`.",
        file=sys.stderr,
    )
