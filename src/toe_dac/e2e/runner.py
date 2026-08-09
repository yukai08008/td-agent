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
        if mode == "live" and case.case_id != "LIVE-001":
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
                asyncio.run(self._run_live_001_model(service, record, workspace, str(model_config_path), str(model_id)))
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
            self.repository.end_session(service.context)
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
        self.repository.start_new_session(service.context)
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
            self.repository.end_session(service.context)
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
