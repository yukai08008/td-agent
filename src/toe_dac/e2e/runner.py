from __future__ import annotations

import hashlib
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..context import utc_now
from ..conversation import ConversationController
from ..llm_adapter import TARGET_TOOL_SCHEMA, TOEDACLLMAdapter
from ..service import TDService
from ..states import TDState
from ..storage import TDRepository, atomic_write_json, short_id
from ..validation import ValidationError
from .cases import CaseDefinition, CaseRegistry


PASS = [{"assertion_id": "mock_check", "required": True, "passed": True}]


class E2ERunner:
    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root)
        self.repository = TDRepository(self.data_root)
        self.registry = CaseRegistry()
        self.runs_root = self.data_root / "runs"

    def run(
        self,
        case_id: str,
        mode: str = "mock",
        model_id: str | None = None,
        model_config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        case = self.registry.get(case_id)
        if mode not in {"mock", "live"}:
            raise ValueError(f"unsupported mode: {mode}")
        if mode == "live" and case.case_id not in {"LIVE-001", "REG-001"}:
            raise NotImplementedError(
                "live mode currently supports LIVE-001 only; use --mode mock for this case"
            )
        if mode == "live" and (not model_id or not model_config_path):
            raise ValueError("live mode requires --model and --model-config")

        run_id = short_id("run")
        run_dir = self.runs_root / run_id
        workspace = run_dir / "workspace"
        run_dir.mkdir(parents=True)
        shutil.copytree(self.registry.fixture_root(case), workspace)
        service = TDService.create(self.repository, f"ut_{run_id}", case.budgets["max_recoveries"])
        record = {
            "run_id": run_id,
            "case_id": case.case_id,
            "title": case.title,
            "mode": mode,
            "model_id": model_id,
            "status": "running",
            "started_at": utc_now(),
            "user_thread_id": service.context["user_thread_id"],
            "td_id": service.context["td_id"],
            "workspace": str(workspace),
            "metrics": {"human_interrupts": 0, "llm_calls": 0, "actions": 0, "recoveries": 0},
        }
        self._save_run(record)
        started = time.monotonic()
        try:
            if mode == "live":
                if case.case_id == "REG-001":
                    asyncio.run(self._run_reg_001_live(
                        service, record, str(model_config_path), str(model_id),
                    ))
                else:
                    asyncio.run(self._run_live_001_model(
                        service, record, workspace, str(model_config_path), str(model_id),
                    ))
            elif case.case_id == "REG-001":
                self._run_reg_001_mock(service, record)
            elif case.case_id == "LIVE-001":
                self._run_live_001_until_human(service, record)
            elif case.case_id == "LIVE-002":
                self._run_live_002(service, record, workspace)
            elif case.case_id == "LIVE-006":
                self._run_live_006(service, record, workspace)
            else:
                raise NotImplementedError(case.case_id)
        except Exception as exc:
            record["status"] = "error"
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            record["metrics"]["wall_seconds"] = round(time.monotonic() - started, 4)
            record["td_state"] = service.state.value
            record["updated_at"] = utc_now()
            self._save_run(record)
            self.repository.detach_session(service.context)
        return record

    def resume(self, run_id: str, human_response: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.load_run(run_id)
        if record["status"] != "waiting_human":
            raise ValueError(f"run {run_id} is not waiting for human input")
        if record["case_id"] != "LIVE-001":
            raise NotImplementedError("resume is currently implemented for LIVE-001")
        response = human_response or {
            "scope": "只补充 README，不修改代码",
            "acceptance": "README 包含安装、运行和测试说明",
        }
        service = TDService.load(self.repository, record["user_thread_id"], record["td_id"])
        self.repository.attach_session(service.context)
        workspace = Path(record["workspace"])
        started = time.monotonic()
        try:
            service.human_reply(response)
            service.submit_target(self.registry.get("LIVE-001").target)
            service.submit_observation({
                "facts": [{"description": "README 目前只有项目简介", "source_type": "file", "source_ref": "README.md"}],
                "unknowns": [],
            })
            service.submit_estimate({"verdict": "feasible", "risks": [], "cost": {"max_actions": 1}, "information_gaps": []})
            service.submit_plan(self._readme_plan())
            readme = workspace / "README.md"
            readme.write_text(
                "# Demo Project\n\n## 安装\n无需额外依赖。\n\n## 运行\n`python app.py`\n\n## 测试\n`pytest -q`\n",
                encoding="utf-8",
            )
            record["metrics"]["actions"] += 1
            service.submit_action_result({"result": {"file": "README.md", "written": True}})
            service.check_action(PASS)
            content = readme.read_text(encoding="utf-8")
            target_passed = all(header in content for header in ("## 安装", "## 运行", "## 测试"))
            service.check_target([{"assertion_id": "readme_sections", "required": True, "passed": target_passed}])
            record["status"] = "succeeded" if service.state == TDState.SUCCEEDED else "failed"
        finally:
            record["metrics"]["wall_seconds"] = record["metrics"].get("wall_seconds", 0) + round(time.monotonic() - started, 4)
            record["td_state"] = service.state.value
            record["updated_at"] = utc_now()
            self._save_run(record)
            self.repository.detach_session(service.context)
        return record

    def report(self, run_id: str) -> dict[str, Any]:
        record = self.load_run(run_id)
        service = TDService.load(self.repository, record["user_thread_id"], record["td_id"])
        return {
            **record,
            "target_passed": service.state == TDState.SUCCEEDED,
            "revision": service.context["revision"],
            "events": self.repository.event_log(service.context),
            "operation_count": len(self.repository.operation_log(service.context)),
            "artifacts": {
                "workspace": record["workspace"],
                "run_record": str(self._run_path(run_id)),
            },
        }

    def load_run(self, run_id: str) -> dict[str, Any]:
        path = self._run_path(run_id)
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _run_live_001_until_human(self, service: TDService, record: dict[str, Any]) -> None:
        service.start()
        service.target_needs_input(
            "整理是指补充文档、修改代码，还是修复测试？成功标准是什么？",
            "用户需求无法形成可验证 Target",
        )
        record["status"] = "waiting_human"
        record["metrics"]["human_interrupts"] = 1
        record["human_request"] = service.context["control"]["human_question"]

    def _run_reg_001_mock(self, service: TDService, record: dict[str, Any]) -> None:
        case = self.registry.get("REG-001")
        service.start()
        service.submit_target(case.target)
        screenshot_dir = self.repository.session_evidence_dir(service.context) / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot = screenshot_dir / "observe-example-domain.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"TOE-DAC REG-001 deterministic screenshot evidence")
        service.submit_observation({
            "facts": [
                {"description": "页面 URL 为 https://example.com", "source_type": "browser", "source_ref": str(screenshot)},
                {"description": "页面标题为 Example Domain", "source_type": "browser", "source_ref": str(screenshot)},
                {"description": "正文说明该域名用于文档中的示例，无需事先协调或申请许可", "source_type": "browser", "source_ref": str(screenshot)},
            ],
            "unknowns": [],
        })
        service.submit_estimate({
            "verdict": "feasible", "risks": [],
            "cost": {"max_actions": 1}, "information_gaps": [],
        })
        service.submit_plan({
            "plan_id": "plan_reg_001", "version": 1,
            "actions": [{
                "action_id": "a_report", "objective": "向用户输出简短中文网页报告",
                "depends_on": [], "instruction": "仅依据已保存的网页事实和截图生成报告",
                "executor": "agent_response",
                "assertions": [
                    {"description": "报告包含标题和主要内容", "required": True},
                    {"description": "截图证据存在", "required": True},
                ],
                "max_attempts": 1,
            }],
        })
        report_text = (
            "# Example.com 网页简报\n\n"
            "页面标题为 **Example Domain**。主要内容说明该域名专门用于文档中的示例，"
            "可以直接用于说明材料，无需事先协调或申请许可。\n\n"
            f"截图证据：`{screenshot.name}`\n"
        )
        report_ref = self.repository.write_artifact(service.context, "example-com-report.md", report_text)
        service.context["artifacts"].append(report_ref)
        service.submit_action_result({"result": {
            "executor": "agent_response", "content": report_text,
            "artifact_ref": report_ref, "screenshot_ref": str(screenshot),
        }})
        record["metrics"]["actions"] = 1
        service.check_action(PASS)
        service.check_target([
            {"assertion_id": "title", "required": True, "passed": "Example Domain" in report_text},
            {"assertion_id": "content", "required": True, "passed": "文档中的示例" in report_text},
            {"assertion_id": "report", "required": True, "passed": bool(report_text.strip())},
            {"assertion_id": "screenshot", "required": True, "passed": self._valid_png(screenshot)},
        ])
        record["oracle"] = self._reg_001_oracle(service, screenshot)
        record["status"] = "succeeded" if all(record["oracle"].values()) else "failed"
        record["artifacts"] = {"report": report_ref, "screenshot": str(screenshot)}

    async def _run_reg_001_live(
        self,
        service: TDService,
        record: dict[str, Any],
        model_config_path: str,
        model_id: str,
    ) -> None:
        case = self.registry.get("REG-001")
        adapter = TOEDACLLMAdapter(model_config_path, model_id)
        controller = ConversationController(self.repository, adapter, service)
        events = await controller.handle_user_events(case.user_request)
        operations = self.repository.operation_log(service.context)
        record["metrics"]["llm_calls"] = len([
            item for item in operations if item.get("operation") == "generate_structured"
        ])
        record["metrics"]["actions"] = len(service.context.get("execution", {}).get("attempts", []))
        record["metrics"]["recoveries"] = int(service.context["recovery"].get("retry_count", 0)) + sum(
            int(value) for value in service.context["recovery"].get("runtime_retry_counts", {}).values()
        )
        record["metrics"]["human_interrupts"] = len([
            event for event in events if event.type == "human_question"
        ])
        screenshots = sorted((self.repository.session_evidence_dir(service.context) / "screenshots").glob("*.png"))
        screenshot = screenshots[-1] if screenshots else None
        record["oracle"] = self._reg_001_oracle(service, screenshot)
        if service.state == TDState.WAITING_HUMAN:
            record["status"] = "waiting_human"
            record["human_request"] = service.context["control"].get("human_question")
        else:
            record["status"] = "succeeded" if all(record["oracle"].values()) else "failed"
        record["artifacts"] = {
            "report_refs": list(service.context.get("artifacts", [])),
            "screenshot": str(screenshot) if screenshot else None,
        }

    def _reg_001_oracle(self, service: TDService, screenshot: Path | None) -> dict[str, bool]:
        observation_text = json.dumps(service.context.get("observation", {}), ensure_ascii=False)
        artifact_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for reference in service.context.get("artifacts", [])
            if (path := (Path(str(reference)) if Path(str(reference)).is_absolute()
                        else self.repository.root / str(reference))).is_file()
        )
        return {
            "terminal_succeeded": service.state == TDState.SUCCEEDED,
            "title_observed": "Example Domain" in observation_text,
            "main_content_observed": any(
                marker in observation_text.lower()
                for marker in ("documentation examples", "illustrative examples", "示例", "文档")
            ),
            "chinese_report_created": bool(artifact_text) and any(
                "\u4e00" <= char <= "\u9fff" for char in artifact_text
            ),
            "valid_png_screenshot": self._valid_png(screenshot),
            "no_human_wait": service.state != TDState.WAITING_HUMAN,
        }

    @staticmethod
    def _valid_png(path: Path | None) -> bool:
        return bool(
            path and path.is_file() and path.stat().st_size > 8
            and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        )

    async def _run_live_001_model(
        self,
        service: TDService,
        record: dict[str, Any],
        workspace: Path,
        model_config_path: str,
        model_id: str,
    ) -> None:
        service.start()
        adapter = TOEDACLLMAdapter(model_config_path, model_id)
        files = [str(path.relative_to(workspace)) for path in sorted(workspace.rglob("*")) if path.is_file()]
        result = await adapter.generate_structured(
            phase="target",
            system_prompt=(
                "你是 TOE-DAC 的 Target 决策器。只定义可验证目标，不执行 Observe 或后续阶段。"
                "如果用户需求存在会实质改变工作范围或验收标准的歧义，必须通过 submit_target 返回 needs_human。"
            ),
            payload={
                "user_request": "帮我把这个项目整理好。",
                "workspace_files": files,
                "permissions": {"read": ["workspace/**"], "write": []},
                "current_state": "targeting",
            },
            tool_name="submit_target",
            schema=TARGET_TOOL_SCHEMA,
        )
        record["metrics"]["llm_calls"] = 2 if result.repaired else 1
        record["model_id"] = result.model_id or model_id
        trace = {
            "phase": "target",
            "model_id": record["model_id"],
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "repaired": result.repaired,
            "structured_output": result.data,
        }
        atomic_write_json(self.runs_root / record["run_id"] / "llm-target.json", trace)
        status = result.data.get("status")
        if status == "needs_human":
            question = str(result.data.get("question", "")).strip()
            reason = str(result.data.get("reason", "")).strip()
            if not question or not reason:
                raise ValidationError(["needs_human output requires question and reason"])
            service.target_needs_input(question, reason)
            record["status"] = "waiting_human"
            record["metrics"]["human_interrupts"] = 1
            record["human_request"] = question
            return
        if status == "accepted":
            target = result.data.get("target")
            if not isinstance(target, dict):
                raise ValidationError(["accepted output requires target object"])
            service.submit_target(target)
            record["status"] = "failed"
            record["expectation_failure"] = "model accepted an intentionally ambiguous request without human clarification"
            return
        raise ValidationError([f"unsupported target status: {status}"])

    def _run_live_002(self, service: TDService, record: dict[str, Any], workspace: Path) -> None:
        case = self.registry.get("LIVE-002")
        tests_before = _hash_tree(workspace / "tests")
        service.start()
        service.submit_target(case.target)
        baseline = _run_pytest(workspace)
        service.submit_observation({
            "facts": [
                {"description": "基线 pytest 有 1 个失败", "source_type": "command", "source_ref": "pytest-baseline"},
                {"description": "divide(1, 0) 返回 None", "source_type": "file", "source_ref": "calculator.py"},
            ],
            "unknowns": [],
        })
        service.submit_estimate({"verdict": "feasible", "risks": ["不得改变签名"], "cost": {"max_actions": 3}, "information_gaps": []})
        service.submit_plan(self._calculator_plan())

        service.submit_action_result({"result": {"exit_code": baseline["exit_code"], "purpose": "baseline"}})
        record["metrics"]["actions"] += 1
        service.check_action(PASS)

        source = workspace / "calculator.py"
        original = source.read_text(encoding="utf-8")
        expected = "    if b == 0:\n        return None\n"
        replacement = "    if b == 0:\n        raise ZeroDivisionError(\"division by zero\")\n"
        if expected not in original:
            raise RuntimeError("fixture no longer contains expected bug")
        source.write_text(original.replace(expected, replacement), encoding="utf-8")
        service.submit_action_result({"result": {"file": "calculator.py", "patched": True}})
        record["metrics"]["actions"] += 1
        service.check_action(PASS)

        final = _run_pytest(workspace)
        service.submit_action_result({"result": {"exit_code": final["exit_code"], "purpose": "target"}})
        record["metrics"]["actions"] += 1
        service.check_action(PASS if final["exit_code"] == 0 else [{"required": True, "passed": False}])
        tests_unchanged = tests_before == _hash_tree(workspace / "tests")
        service.check_target([
            {"assertion_id": "pytest", "required": True, "passed": final["exit_code"] == 0},
            {"assertion_id": "tests_unchanged", "required": True, "passed": tests_unchanged},
        ])
        record["status"] = "succeeded" if service.state == TDState.SUCCEEDED else "failed"
        record["artifacts"] = {"baseline": baseline, "final": final}

    def _run_live_006(self, service: TDService, record: dict[str, Any], workspace: Path) -> None:
        case = self.registry.get("LIVE-006")
        service.start()
        service.submit_target(case.target)
        service.submit_observation({
            "facts": [{"description": "fixture 可读取", "source_type": "file", "source_ref": str(workspace)}],
            "unknowns": [],
        })
        service.submit_estimate({"verdict": "feasible", "risks": [], "cost": {"max_actions": 1}, "information_gaps": []})
        try:
            service.submit_plan({"plan_id": "invalid_plan", "version": 1})
        except ValidationError as exc:
            record["injected_failure"] = {"type": "invalid_model_output", "errors": exc.errors}
        service.submit_plan({
            "plan_id": "repaired_plan",
            "version": 1,
            "actions": [{
                "action_id": "a_validate",
                "objective": "验证修复后的 Plan Schema",
                "depends_on": [],
                "instruction": "检查 actions 和 assertions",
                "assertions": [{"description": "Plan 结构合法", "required": True}],
                "max_attempts": 1,
            }],
        })
        service.submit_action_result({"result": {"schema_valid": True}})
        record["metrics"]["actions"] += 1
        service.check_action(PASS)
        service.check_target(PASS)
        record["status"] = "succeeded" if service.state == TDState.SUCCEEDED else "failed"

    @staticmethod
    def _calculator_plan() -> dict[str, Any]:
        return {
            "plan_id": "plan_calculator_fix",
            "version": 1,
            "actions": [
                {"action_id": "a_baseline", "objective": "运行基线测试", "depends_on": [], "assertions": [{"description": "获得基线结果", "required": True}], "max_attempts": 1},
                {"action_id": "a_patch", "objective": "修复除零行为", "depends_on": ["a_baseline"], "assertions": [{"description": "补丁应用成功", "required": True}], "max_attempts": 1},
                {"action_id": "a_test", "objective": "运行目标测试", "depends_on": ["a_patch"], "assertions": [{"description": "测试通过", "required": True}], "max_attempts": 1},
            ],
        }

    @staticmethod
    def _readme_plan() -> dict[str, Any]:
        return {
            "plan_id": "plan_readme",
            "version": 1,
            "actions": [{
                "action_id": "a_readme",
                "objective": "补充 README 使用说明",
                "depends_on": [],
                "assertions": [{"description": "三个章节存在", "required": True}],
                "max_attempts": 1,
            }],
        }

    def _save_run(self, record: dict[str, Any]) -> None:
        atomic_write_json(self._run_path(record["run_id"]), record)

    def _run_path(self, run_id: str) -> Path:
        if not run_id.startswith("run_") or "/" in run_id or ".." in run_id:
            raise ValueError(f"invalid run id: {run_id}")
        return self.runs_root / run_id / "run.json"


def _run_pytest(workspace: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["python", "-m", "pytest", "-q"],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-2000:],
    }


def _hash_tree(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
