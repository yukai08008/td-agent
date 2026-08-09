from __future__ import annotations

import re
from collections.abc import Sequence


REPOSITORY_URL = "https://github.com/yukai08008/td-agent.git"
RELEASE_VERSION = re.compile(r"^v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")


def normalize_release_version(version: str) -> str:
    match = RELEASE_VERSION.fullmatch(version.strip())
    if not match:
        raise ValueError(f"invalid release version: {version}; expected X.Y.Z")
    normalized = match.group(1)
    if normalized == "0.1.0":
        raise ValueError(
            "v0.1.0 used a private local dependency; public standalone versions start at v0.2.0",
        )
    return normalized


def package_spec(version: str | None = None) -> str:
    if version is None:
        return f"git+{REPOSITORY_URL}"
    normalized = normalize_release_version(version)
    return f"git+{REPOSITORY_URL}@v{normalized}"


def forwarded_version_args(arguments: Sequence[str]) -> list[str]:
    """Remove this launcher’s version selector before invoking the selected release."""
    forwarded: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--use-version":
            skip_next = True
            continue
        if argument.startswith("--use-version="):
            continue
        forwarded.append(argument)
    return forwarded
