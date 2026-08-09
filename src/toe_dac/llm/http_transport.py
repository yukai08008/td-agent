from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any


class HTTPResponseError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPResponseError(exc.code, body) from exc
    return json.loads(body)


async def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 60,
) -> dict[str, Any]:
    return await asyncio.to_thread(_post_json, url, headers, payload, timeout)
