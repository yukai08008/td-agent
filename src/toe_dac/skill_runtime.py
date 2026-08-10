from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
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
from .runtime_content import LoadedSkill


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
        self.session_dir: Path | None = None
        self.loaded_skill_roots: dict[str, Path] = {}
        self.loaded_skill_phases: dict[str, tuple[str, ...]] = {}

    def configure_evidence(self, screenshot_dir: Path, session_id: str) -> None:
        self.evidence_screenshot_dir = screenshot_dir
        self.evidence_screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = screenshot_dir.resolve().parent
        safe_session = "".join(character for character in session_id if character.isalnum() or character in "-_")
        self.browser_session_name = f"td-{safe_session or 'session'}"

    def configure_skills(self, skills: tuple[LoadedSkill, ...]) -> None:
        """Expose roots only for Skills already activated by load_skill."""
        configured: dict[str, Path] = {}
        phases: dict[str, tuple[str, ...]] = {}
        for skill in skills:
            if not skill.root:
                continue
            root = Path(skill.root).resolve()
            if root.is_dir():
                configured[skill.name] = root
                phases[skill.name] = skill.phases
        self.loaded_skill_roots = configured
        self.loaded_skill_phases = phases

    def tool_definitions(self, active_skills: set[str], phase: str = "") -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        executable_skills = sorted(
            name for name in active_skills
            if name in self.loaded_skill_roots and (self.loaded_skill_roots[name] / "scripts").is_dir()
            and (
                not self.loaded_skill_phases.get(name)
                or not phase
                or phase in self.loaded_skill_phases[name]
            )
        )
        if executable_skills:
            tools.append({
                "type": "function",
                "function": {
                    "name": "run_skill_script",
                    "description": (
                        "运行已经 load_skill 的 Skill 所携带的 scripts/ 脚本。"
                        "只能使用参数数组，不执行任意 Shell 字符串。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {"type": "string", "enum": executable_skills},
                            "script": {
                                "type": "string",
                                "description": "相对于 Skill 根目录的 scripts/... 路径",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                            "arguments": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 2000},
                                "maxItems": 64,
                                "default": [],
                            },
                            "timeout": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 600,
                                "default": 120,
                            },
                            "evidence_role": {
                                "type": "string",
                                "enum": ["observation", "before", "action", "after", "result"],
                                "default": "result",
                                "description": "当前调用在阶段证据链中的角色",
                            },
                        },
                        "required": ["skill_name", "script"],
                        "additionalProperties": False,
                    },
                },
            })
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
        if tool_name == "run_skill_script":
            return await self._run_skill_script(arguments)
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

    async def _run_skill_script(self, arguments: dict[str, Any]) -> SkillToolResult:
        skill_name = str(arguments.get("skill_name", "")).strip()
        script_name = str(arguments.get("script", "")).strip()
        raw_arguments = arguments.get("arguments", [])
        timeout = arguments.get("timeout", 120)
        if skill_name not in self.loaded_skill_roots:
            return self._script_result(
                skill_name, script_name, False, 0, error="skill is not loaded",
            )
        if not isinstance(raw_arguments, list) or not all(isinstance(item, str) for item in raw_arguments):
            return self._script_result(
                skill_name, script_name, False, 0, error="arguments must be a list of strings",
            )
        if len(raw_arguments) > 64 or any(len(item) > 2000 for item in raw_arguments):
            return self._script_result(
                skill_name, script_name, False, 0, error="script arguments exceed runtime limits",
            )
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
            return self._script_result(
                skill_name, script_name, False, 0, error="timeout must be an integer from 1 to 600",
            )

        root = self.loaded_skill_roots[skill_name]
        scripts_root = (root / "scripts").resolve()
        requested = Path(script_name)
        if requested.is_absolute() or not script_name.startswith("scripts/"):
            return self._script_result(
                skill_name, script_name, False, 0, error="script must be a relative scripts/... path",
            )
        script = (root / requested).resolve()
        try:
            script.relative_to(scripts_root)
        except ValueError:
            return self._script_result(
                skill_name, script_name, False, 0, error="script path escapes the Skill scripts directory",
            )
        if not script.is_file():
            return self._script_result(
                skill_name, script_name, False, 0, error="script does not exist",
            )

        interpreter = self._script_interpreter(script)
        if not interpreter:
            return self._script_result(
                skill_name,
                script_name,
                False,
                0,
                error="unsupported script type; supported extensions are .py, .sh, and .js",
            )
        command = [interpreter, str(script), *raw_arguments]
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        environment = os.environ.copy()
        if self.session_dir is not None:
            state_dir = self.session_dir / "skill-jobs" / skill_name
            state_dir.mkdir(parents=True, exist_ok=True)
            state_dir.chmod(0o700)
            environment["TOE_DAC_SESSION_DIR"] = str(self.session_dir)
            environment["TOE_DAC_SKILL_STATE_DIR"] = str(state_dir)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(root),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                return self._script_result(
                    skill_name,
                    script_name,
                    False,
                    (time.monotonic() - started) * 1000,
                    error=f"script timed out after {timeout} seconds",
                    exit_code=process.returncode,
                    stdout=self._decode_output(stdout),
                    stderr=self._decode_output(stderr),
                    script_sha256=digest,
                )
            duration_ms = (time.monotonic() - started) * 1000
            stdout_text = self._decode_output(stdout)
            stderr_text = self._decode_output(stderr)
            success = process.returncode == 0
            return self._script_result(
                skill_name,
                script_name,
                success,
                duration_ms,
                error="" if success else f"script exited with code {process.returncode}",
                exit_code=process.returncode,
                stdout=stdout_text,
                stderr=stderr_text,
                script_sha256=digest,
            )
        except Exception as exc:
            return self._script_result(
                skill_name,
                script_name,
                False,
                (time.monotonic() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                script_sha256=digest,
            )

    @staticmethod
    def _script_interpreter(script: Path) -> str | None:
        if script.suffix == ".py":
            return sys.executable
        if script.suffix == ".sh":
            return shutil.which("bash")
        if script.suffix == ".js":
            return shutil.which("node")
        return None

    @staticmethod
    def _decode_output(value: bytes, limit: int = 64_000) -> str:
        decoded = value.decode("utf-8", errors="replace")
        if len(decoded) <= limit:
            return decoded
        return decoded[:limit] + f"\n... [truncated {len(decoded) - limit} characters]"

    @staticmethod
    def _script_result(
        skill_name: str,
        script_name: str,
        success: bool,
        duration_ms: float,
        *,
        error: str,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        script_sha256: str = "",
    ) -> SkillToolResult:
        output = {
            "ok": success,
            "skill_name": skill_name,
            "script": script_name,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        if error:
            output["error"] = error
        evidence = {
            "skill_name": skill_name,
            "script": script_name,
            "script_sha256": script_sha256,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        return SkillToolResult(output, {
            "skill": skill_name,
            "tool": "run_skill_script",
            "status": "succeeded" if success else "failed",
            "attempt_count": 1,
            "duration_ms": round(duration_ms, 1),
            "script": script_name,
            "script_sha256": script_sha256,
            "exit_code": exit_code,
            "error": error,
            "evidence": evidence,
        })

    async def _observe_with_browser(self, arguments: dict[str, Any]) -> SkillToolResult:
        url = str(arguments.get("url", "")).strip()
        purpose = str(arguments.get("purpose", "")).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            return self._result(
                "agent_browser_observe", False, 0, 0, skill_name="agent-browser",
                error="url must be an http(s) URL without embedded credentials",
            )
        binary = shutil.which("agent-browser")
        if not binary:
            return self._result(
                "agent_browser_observe", False, 0, 0, skill_name="agent-browser",
                error="agent-browser is not installed or not available on PATH",
            )
        if self.evidence_screenshot_dir is None:
            return self._result(
                "agent_browser_observe", False, 0, 0, skill_name="agent-browser",
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
                skill_name="agent-browser",
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
        skill_name: str = "alex-serp",
    ) -> SkillToolResult:
        return SkillToolResult(
            {"ok": success, "error": error},
            {
                "skill": skill_name,
                "tool": tool_name,
                "status": "succeeded" if success else "failed",
                "attempt_count": attempt_count,
                "duration_ms": round(duration_ms, 1),
                "error": error,
                "attempts": attempts or [],
            },
        )
