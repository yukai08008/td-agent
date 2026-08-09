from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path


CHANGELOG_URL = "https://raw.githubusercontent.com/yukai08008/td-agent/main/CHANGELOG.md"
VERSION_HEADING = re.compile(r"^## \[([^]]+)](?:\s+-\s+.*)?$", re.MULTILINE)


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "td-agent" / "CHANGELOG.md"


def load_changelog() -> str:
    local = Path.cwd() / "CHANGELOG.md"
    if local.exists():
        return local.read_text(encoding="utf-8")

    cache = _cache_path()
    try:
        timeout = max(0.1, float(os.environ.get("TOE_DAC_UPDATE_CHECK_TIMEOUT", "1.5") or "1.5"))
    except ValueError:
        timeout = 1.5
    request = urllib.request.Request(CHANGELOG_URL, headers={"User-Agent": "td-agent-changelog"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(256_000).decode("utf-8")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(content, encoding="utf-8")
        return content
    except (OSError, UnicodeError):
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        raise RuntimeError(f"Unable to load changelog. Open {CHANGELOG_URL}") from None


def extract_version(content: str, version: str) -> str:
    requested = version.removeprefix("v")
    matches = list(VERSION_HEADING.finditer(content))
    for index, match in enumerate(matches):
        if match.group(1) != requested:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[match.start():end].strip()
        section = re.sub(r"\n\[[^\n]+]: https?://[^\n]+", "", section).strip()
        return section
    raise ValueError(f"version not found in changelog: {version}")
