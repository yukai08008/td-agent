from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

from .llm.http_transport import HTTPResponseError, post_json


@dataclass(frozen=True)
class SkillToolResult:
    output: dict[str, Any]
    event: dict[str, Any]


class SkillToolRuntime:
    """Small allow-listed runtime for tools activated by progressive skills."""

    ALEX_SERP_URL = "http://106.75.97.247:24656/serp"

    def __init__(self) -> None:
        self.evidence_screenshot_dir: Path | None = None
        self.browser_session_name = "toe-dac"

    def configure_evidence(self, screenshot_dir: Path, session_id: str) -> None:
        self.evidence_screenshot_dir = screenshot_dir
        self.evidence_screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe_session = "".join(character for character in session_id if character.isalnum() or character in "-_")
        self.browser_session_name = f"td-{safe_session or 'session'}"

    def tool_definitions(self, active_skills: set[str], phase: str = "") -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if "alex-serp" in active_skills and phase == "observe":
            tools.append({
                "type": "function",
                "function": {
                    "name": "alex_serp_search",
                    "description": "通过 Alex SERP API 搜索百度结果，返回标题、摘要和链接。",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            })
        if "agent-browser" in active_skills and phase == "observe":
            tools.append({
                "type": "function",
                "function": {
                    "name": "agent_browser_observe",
                    "description": "打开关键网页、提取可访问性快照并把整页截图登记为当前 Session 的视觉证据。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "purpose": {"type": "string", "minLength": 1, "maxLength": 300},
                        },
                        "required": ["url", "purpose"],
                        "additionalProperties": False,
                    },
                },
            })
        return tools

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> SkillToolResult:
        if tool_name == "agent_browser_observe":
            return await self._observe_with_browser(arguments)
        if tool_name != "alex_serp_search":
            raise ValueError(f"unsupported skill tool: {tool_name}")
        query = str(arguments.get("query", "")).strip()
        if not query or len(query) > 500:
            return self._result(tool_name, False, 0, 0, error="query must contain 1-500 characters")

        started = time.monotonic()
        attempts: list[dict[str, Any]] = []
        for attempt in range(3):
            try:
                response = await post_json(
                    self.ALEX_SERP_URL,
                    {"Content-Type": "application/json"},
                    {"query": query},
                    timeout=60,
                )
                results = response.get("results")
                if not isinstance(results, list):
                    raise ValueError("SERP response results must be a list")
                output = {"ok": True, "results": results, "count": int(response.get("count", len(results)))}
                return SkillToolResult(output, {
                    "skill": "alex-serp",
                    "tool": tool_name,
                    "status": "succeeded",
                    "attempt_count": attempt + 1,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "attempts": attempts,
                })
            except HTTPResponseError as exc:
                attempts.append({"attempt": attempt + 1, "error_type": type(exc).__name__, "status": exc.status})
                if exc.status not in {429, 503} or attempt == 2:
                    return self._result(
                        tool_name, False, attempt + 1, (time.monotonic() - started) * 1000,
                        error=str(exc), attempts=attempts,
                    )
                delay = 1 if attempt == 0 else 2
                if progress_callback:
                    progress_callback({
                        "type": "skill_tool_retry",
                        "tool": tool_name,
                        "attempt": attempt + 1,
                        "status": exc.status,
                        "delay_seconds": delay,
                    })
                await asyncio.sleep(delay)
            except Exception as exc:
                attempts.append({"attempt": attempt + 1, "error_type": type(exc).__name__, "error": str(exc)})
                return self._result(
                    tool_name, False, attempt + 1, (time.monotonic() - started) * 1000,
                    error=str(exc), attempts=attempts,
                )
        raise RuntimeError("unreachable SERP retry loop")

    async def _observe_with_browser(self, arguments: dict[str, Any]) -> SkillToolResult:
        url = str(arguments.get("url", "")).strip()
        purpose = str(arguments.get("purpose", "")).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            return self._result(
                "agent_browser_observe", False, 0, 0,
                error="url must be an http(s) URL without embedded credentials",
            )
        binary = shutil.which("agent-browser")
        if not binary:
            return self._result(
                "agent_browser_observe", False, 0, 0,
                error="agent-browser is not installed or not available on PATH",
            )
        if self.evidence_screenshot_dir is None:
            return self._result(
                "agent_browser_observe", False, 0, 0,
                error="Session screenshot evidence directory is not configured",
            )
        screenshot = self.evidence_screenshot_dir / f"observe-{uuid.uuid4().hex[:8]}.png"
        started = time.monotonic()
        opened = False
        try:
            await self._run_browser(binary, "open", url)
            opened = True
            snapshot = await self._run_browser(binary, "snapshot", "-c")
            page_title = await self._run_browser(binary, "get", "title")
            body_text = await self._run_browser(binary, "get", "text", "body")
            await self._run_browser(binary, "screenshot", str(screenshot), "--full")
            if not screenshot.is_file():
                raise RuntimeError("agent-browser did not create the requested screenshot")
            screenshot_size = screenshot.stat().st_size
            screenshot_signature = screenshot.read_bytes()[:8]
            screenshot_format = "png" if screenshot_signature == b"\x89PNG\r\n\x1a\n" else "unknown"
            observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            duration = round((time.monotonic() - started) * 1000, 1)
            index_path = screenshot.parent.parent / "screenshots.jsonl"
            with index_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "status": "captured",
                    "file": screenshot.name,
                    "path": str(screenshot),
                    "url": url,
                    "purpose": purpose,
                    "captured_at": observed_at,
                    "size_bytes": screenshot_size,
                    "format": screenshot_format,
                }, ensure_ascii=False, sort_keys=True) + "\n")
            return SkillToolResult(
                {
                    "ok": True,
                    "url": url,
                    "purpose": purpose,
                    "page_title": page_title,
                    "body_text": body_text[-20000:],
                    "snapshot": snapshot[-20000:],
                    "screenshot_ref": str(screenshot),
                    "screenshot_size_bytes": screenshot_size,
                    "screenshot_format": screenshot_format,
                    "observed_at": observed_at,
                },
                {
                    "skill": "agent-browser",
                    "tool": "agent_browser_observe",
                    "status": "succeeded",
                    "attempt_count": 1,
                    "duration_ms": duration,
                    "url": url,
                    "purpose": purpose,
                    "screenshot_ref": str(screenshot),
                    "evidence": {
                        "url": url,
                        "page_title": page_title,
                        "body_text": body_text[-20000:],
                        "snapshot": snapshot[-20000:],
                        "screenshot_ref": str(screenshot),
                        "screenshot_size_bytes": screenshot_size,
                        "screenshot_format": screenshot_format,
                        "observed_at": observed_at,
                    },
                },
            )
        except Exception as exc:
            return self._result(
                "agent_browser_observe", False, 1,
                (time.monotonic() - started) * 1000, error=str(exc),
            )
        finally:
            if opened:
                try:
                    await self._run_browser(binary, "close")
                except Exception:
                    pass

    async def _run_browser(self, binary: str, command: str, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--session", self.browser_session_name,
            command,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError(f"agent-browser {command} timed out")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"agent-browser {command} failed: {detail}")
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _result(
        tool_name: str,
        success: bool,
        attempt_count: int,
        duration_ms: float,
        *,
        error: str,
        attempts: list[dict[str, Any]] | None = None,
    ) -> SkillToolResult:
        return SkillToolResult(
            {"ok": success, "error": error},
            {
                "skill": "alex-serp",
                "tool": tool_name,
                "status": "succeeded" if success else "failed",
                "attempt_count": attempt_count,
                "duration_ms": round(duration_ms, 1),
                "error": error,
                "attempts": attempts or [],
            },
        )
