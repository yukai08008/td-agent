from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import utc_now
from .storage import TDRepository, short_id


URL_PATTERN = re.compile(r"https?://[^\s<>\]\[(){}\"'，。；！？]+", re.IGNORECASE)


@dataclass
class DeterministicCheckResult:
    checks: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]

    @property
    def complete(self) -> bool:
        return bool(self.checks) and not self.unresolved


class DeterministicControlPlane:
    """Mechanical support for TOE-DAC stages, never a replacement for stage judgment."""

    def __init__(self, repository: TDRepository, context: dict[str, Any], adapter: Any):
        self.repository = repository
        self.context = context
        self.adapter = adapter

    @property
    def evidence_directory(self) -> Path:
        base = self.repository.session_evidence_dir(self.context)
        return base / (
            "view/screenshots"
            if self.repository.is_legacy_thread(self.context["user_thread_id"])
            else "screenshots"
        )

    def storage_contract(self) -> dict[str, Any]:
        return {
            "canonical_evidence_directory": str(self.evidence_directory),
            "screenshot_policy": (
                "A screenshot created here is already formal evidence. Do not create a "
                "copy, move, or archive Action unless the user explicitly requested another location."
            ),
            "artifact_directory": str(self.repository.artifact_dir(self.context)),
            "artifact_policy": "Reports and final deliverables are written here by the controller.",
        }

    def normalize_target(self, target: dict[str, Any], user_text: str) -> tuple[dict[str, Any], list[str]]:
        normalized = copy.deepcopy(target)
        changes: list[str] = []
        explicit_noncanonical = "./evidence" in user_text.casefold() or " evidence/" in user_text.casefold()

        def rewrite(value: Any) -> Any:
            if isinstance(value, str) and not explicit_noncanonical:
                replaced = re.sub(
                    r"(?:\./)?evidence/",
                    "Session canonical evidence directory/",
                    value,
                    flags=re.IGNORECASE,
                )
                if replaced != value:
                    changes.append("replaced invented evidence directory with canonical Session evidence")
                return replaced
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        normalized = rewrite(normalized)
        evidence_markers = (
            "canonical evidence", "evidence directory", "evidence/", "证据目录",
            "证据留存", "证据留存", "证据归档", "保留为证据",
            "截图作为证据", "日志作为证据",
        )
        positives = []
        for item in normalized.get("positive", []):
            text = str(item)
            if any(marker in text.casefold() for marker in evidence_markers):
                changes.append("removed runtime evidence handling from Target positive")
                continue
            positives.append(item)
        normalized["positive"] = positives
        criteria = []
        for criterion in normalized.get("acceptance_criteria", []):
            if not isinstance(criterion, dict):
                criteria.append(criterion)
                continue
            description = str(criterion.get("description", "")).casefold()
            check_type = str((criterion.get("check") or {}).get("type", ""))
            if check_type == "evidence_exists" or any(marker in description for marker in evidence_markers):
                changes.append("removed runtime evidence handling from Target acceptance criteria")
                continue
            criteria.append(criterion)
        normalized["acceptance_criteria"] = criteria
        replace_title_expectation = False
        replace_body_expectation = False
        for index, criterion in enumerate(normalized.get("acceptance_criteria", []), start=1):
            criterion.setdefault("criterion_id", f"criterion-{index}")
            criterion.setdefault("required", True)
            check = criterion.get("check") or {}
            if check.get("type") == "observation_field_non_empty":
                field_aliases = {
                    "title": "page_title",
                    "page.title": "page_title",
                    "content": "body_text",
                    "body": "body_text",
                    "main_content": "body_text",
                }
                field_name = str(check.get("field", "")).casefold()
                if field_name in field_aliases:
                    check["field"] = field_aliases[field_name]
                    criterion["check"] = check
                    changes.append(
                        f"normalized Observation field alias {field_name} to {field_aliases[field_name]}"
                    )
            if check.get("type") == "max_length" and isinstance(check.get("value"), dict):
                check["value"] = int(check["value"].get("max", 1200))
                criterion["check"] = check
                changes.append("normalized max_length check to a scalar value")
            if check.get("type") == "max_length" and not self._user_specified_number(user_text):
                if int(check.get("value", 1200)) != 1200:
                    check["value"] = 1200
                    criterion["check"] = check
                    changes.append("replaced model-invented length limit with the default brief-report limit")
            if check.get("type") == "observation_contains":
                expected = str(check.get("value", ""))
                if expected and expected.casefold() not in user_text.casefold():
                    description = str(criterion.get("description", "")).casefold()
                    if any(marker in description for marker in ("标题", "title")):
                        criterion["check"] = {"type": "observation_field_non_empty", "field": "page_title"}
                        criterion["description"] = "实际页面标题已通过 Observation 记录"
                        replace_title_expectation = True
                        changes.append("replaced model-guessed page title with an Observation field check")
                    elif any(marker in description for marker in ("主要内容", "正文", "body", "content")):
                        criterion["check"] = {"type": "observation_field_non_empty", "field": "body_text"}
                        criterion["description"] = "实际页面主要内容已通过 Observation 记录"
                        replace_body_expectation = True
                        changes.append("replaced model-guessed page content with an Observation field check")
        if replace_title_expectation or replace_body_expectation:
            positives: list[Any] = []
            for item in normalized.get("positive", []):
                text = str(item)
                lowered = text.casefold()
                if replace_title_expectation and any(marker in lowered for marker in ("标题", "title")):
                    positives.append("确认并记录页面实际标题")
                    continue
                if replace_body_expectation and any(
                    marker in lowered for marker in ("主要内容", "正文", "body", "content")
                ):
                    positives.append("确认并记录页面实际主要内容")
                    continue
                positives.append(item)
            normalized["positive"] = positives
        return normalized, list(dict.fromkeys(changes))

    def normalize_estimate(self, estimate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        normalized = copy.deepcopy(estimate)
        changes: list[str] = []
        defaults = {
            "risks": [],
            "information_gaps": [],
            "cost": {
                "model_decisions_remaining": 2,
                "mechanical_operations": "derived by controller",
            },
        }
        for field_name, default in defaults.items():
            if field_name not in normalized:
                normalized[field_name] = copy.deepcopy(default)
                changes.append(f"filled deterministic estimate field: {field_name}")
        return normalized, changes

    def normalize_plan(self, plan: dict[str, Any], user_text: str) -> tuple[dict[str, Any], list[str]]:
        normalized = copy.deepcopy(plan)
        changes: list[str] = []
        explicit_relocation = any(
            marker in user_text.casefold()
            for marker in ("复制截图", "移动截图", "归档至", "./evidence", " evidence/")
        )
        retained: list[dict[str, Any]] = []
        removed_ids: set[str] = set()
        for index, action in enumerate(normalized.get("actions", []), start=1):
            action.setdefault("action_id", f"action-{index}")
            action.setdefault("depends_on", [])
            action.setdefault("max_attempts", 2)
            action.setdefault("changes_state", False)
            action.setdefault("assertions", [{
                "description": "Action produced a non-empty result",
                "required": True,
                "check": {"type": "non_empty"},
            }])
            text = self._action_text(action)
            screenshot_relocation = (
                self._has_screenshot_evidence()
                and not explicit_relocation
                and any(marker in text for marker in ("截图", "screenshot", ".png"))
                and any(marker in text for marker in ("复制", "移动", "迁移", "归档", "./evidence"))
                and not any(
                    marker in text
                    for marker in ("生成报告", "中文报告", "输出报告", "交付报告", "report")
                )
            )
            duplicated_check = (
                any(marker in text for marker in ("验收标准", "acceptance criteria", "target check"))
                and any(marker in text for marker in ("核验", "验证", "检查", "verify", "check"))
            ) or any(marker in text for marker in ("核验全部验收", "验证全部验收", "verify acceptance"))
            if screenshot_relocation or duplicated_check:
                removed_ids.add(str(action["action_id"]))
                changes.append(
                    f"removed mechanical action {action['action_id']}: "
                    + ("canonical evidence is already retained" if screenshot_relocation else "Target Check owns acceptance verification")
                )
                continue
            for assertion_index, assertion in enumerate(action.get("assertions", []), start=1):
                assertion.setdefault("assertion_id", f"{action['action_id']}.assertion-{assertion_index}")
                assertion.setdefault("required", True)
                check = assertion.get("check") or {}
                if check.get("type") == "max_length" and isinstance(check.get("value"), dict):
                    check["value"] = int(check["value"].get("max", 1200))
                    assertion["check"] = check
                    changes.append(f"normalized max_length check for {assertion['assertion_id']}")
                if check.get("type") == "max_length" and not self._user_specified_number(user_text):
                    if int(check.get("value", 1200)) != 1200:
                        check["value"] = 1200
                        assertion["check"] = check
                        changes.append(
                            f"replaced model-invented length limit for {assertion['assertion_id']}"
                        )
                inferred = self._infer_check(assertion.get("description", ""))
                if inferred and "check" not in assertion:
                    assertion["check"] = inferred
                    changes.append(f"inferred deterministic check for {assertion['assertion_id']}")
            retained.append(action)
        for action in retained:
            action["depends_on"] = [
                dependency for dependency in action.get("depends_on", []) if dependency not in removed_ids
            ]
        normalized["actions"] = retained
        normalized.setdefault("plan_id", short_id("plan"))
        normalized.setdefault("version", 1)
        return normalized, changes

    def evidence_records_from_tool_events(
        self,
        tool_events: list[dict[str, Any]],
        *,
        phase: str,
    ) -> list[dict[str, Any]]:
        """Turn runtime-created files into canonical evidence records.

        The model decides which source/tool is relevant during Observe.  Once a tool
        has created a file, path validation, hashing and registration are mechanical.
        """
        canonical = self.evidence_directory.resolve()
        records: list[dict[str, Any]] = []
        for event in tool_events:
            raw_payload = event.get("raw_output")
            if isinstance(raw_payload, dict):
                records.append(self.persist_json_evidence(
                    phase,
                    str(event.get("tool") or "runtime-tool"),
                    {
                        "input": event.get("raw_input"),
                        "output": raw_payload,
                        "status": event.get("status"),
                        "error_type": event.get("error_type"),
                        "error": event.get("error"),
                    },
                    evidence_role=str(event.get("evidence_role") or "result"),
                ))
            if event.get("status") != "succeeded":
                continue
            payload = event.get("evidence")
            if not isinstance(payload, dict) or not payload.get("screenshot_ref"):
                continue
            screenshot = Path(str(payload["screenshot_ref"])).expanduser().resolve()
            try:
                screenshot.relative_to(canonical)
            except ValueError:
                continue
            if not screenshot.is_file():
                continue
            digest = hashlib.sha256(screenshot.read_bytes()).hexdigest()
            screenshot_format = str(payload.get("screenshot_format", "")).casefold()
            path_digest = hashlib.sha256(str(screenshot).encode("utf-8")).hexdigest()
            records.append({
                "evidence_id": f"evi_{path_digest[:16]}",
                "type": "screenshot",
                "source": str(event.get("tool") or "runtime_tool"),
                "source_url": str(payload.get("url") or payload.get("final_url") or ""),
                "path": str(screenshot),
                "mime_type": "image/png" if screenshot_format == "png" else "application/octet-stream",
                "size_bytes": screenshot.stat().st_size,
                "sha256": digest,
                "created_at": payload.get("observed_at") or utc_now(),
                "phase": phase,
                "evidence_role": str(event.get("evidence_role") or "result"),
                "metadata": {
                    "page_title": payload.get("page_title"),
                    "body_text": payload.get("body_text"),
                    "snapshot": payload.get("snapshot"),
                },
            })
        return records

    def persist_json_evidence(
        self,
        phase: str,
        source: str,
        payload: dict[str, Any],
        *,
        evidence_role: str = "result",
    ) -> dict[str, Any]:
        raw_directory = self.repository.session_evidence_dir(self.context) / "raw"
        raw_directory.mkdir(parents=True, exist_ok=True)
        raw_directory.chmod(0o700)
        safe_phase = re.sub(r"[^a-z0-9_-]+", "-", phase.casefold()).strip("-") or "phase"
        safe_source = re.sub(r"[^a-z0-9_-]+", "-", source.casefold()).strip("-") or "source"
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        digest = hashlib.sha256(
            f"{phase}\n{source}\n{evidence_role}\n{serialized}".encode("utf-8")
        ).hexdigest()
        safe_role = re.sub(r"[^a-z0-9_-]+", "-", evidence_role.casefold()).strip("-") or "result"
        path = raw_directory / f"{safe_phase}-{safe_role}-{safe_source}-{digest[:10]}.json"
        if not path.exists():
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(path)
        return {
            "evidence_id": f"evi_{digest[:16]}",
            "type": "raw_json",
            "source": source,
            "phase": phase,
            "evidence_role": evidence_role,
            "path": str(path),
            "mime_type": "application/json",
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "created_at": utc_now(),
        }

    def check_action(self, action: dict[str, Any], attempt: dict[str, Any]) -> DeterministicCheckResult:
        content = str(attempt.get("result", {}).get("content", ""))
        checks: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for index, assertion in enumerate(action.get("assertions", []), start=1):
            spec = assertion.get("check") or self._infer_check(str(assertion.get("description", "")))
            if not spec:
                unresolved.append(copy.deepcopy(assertion))
                continue
            check = self._evaluate_check(spec, content, assertion, index)
            if check is None:
                unresolved.append(copy.deepcopy(assertion))
            else:
                checks.append(check)
        return DeterministicCheckResult(checks, unresolved)

    def check_target(self) -> DeterministicCheckResult:
        content = self._latest_content()
        checks: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        action_checks = self.context.get("checks", {}).get("action_checks", [])
        passed_action_descriptions = [
            str(check.get("description", ""))
            for group in action_checks
            for check in group.get("checks", [])
            if check.get("passed") is True
        ]
        for index, criterion in enumerate(self.context.get("target", {}).get("acceptance_criteria", []), start=1):
            description = str(criterion.get("description", ""))
            spec = criterion.get("check") or self._infer_check(description)
            if spec:
                evaluated = self._evaluate_check(spec, content, criterion, index)
                if evaluated is not None:
                    checks.append(evaluated)
                    continue
            if self._covered_by_passed_action(description, passed_action_descriptions):
                checks.append(self._check_record(criterion, index, True, "covered by a passed Action assertion"))
            else:
                unresolved.append(copy.deepcopy(criterion))
        return DeterministicCheckResult(checks, unresolved)

    def _evaluate_check(
        self,
        spec: dict[str, Any],
        content: str,
        assertion: dict[str, Any],
        index: int,
    ) -> dict[str, Any] | None:
        check_type = str(spec.get("type", ""))
        if check_type == "non_empty":
            return self._check_record(assertion, index, bool(content.strip()), "result content is non-empty")
        if check_type == "contains":
            expected = str(spec.get("value", ""))
            return self._check_record(assertion, index, bool(expected) and expected.casefold() in content.casefold(), f"content contains {expected!r}")
        if check_type == "observation_contains":
            expected = str(spec.get("value", ""))
            observed = json.dumps(self.context.get("observation", {}), ensure_ascii=False)
            return self._check_record(
                assertion, index,
                bool(expected) and expected.casefold() in observed.casefold(),
                f"Observation contains {expected!r}",
            )
        if check_type == "observation_field_non_empty":
            field_name = str(spec.get("field", ""))
            values = [
                fact.get("value", {}).get(field_name)
                for fact in self.context.get("observation", {}).get("facts", [])
                if isinstance(fact.get("value"), dict)
            ]
            values.extend(
                item.get("metadata", {}).get(field_name)
                for item in self.context.get("evidence_registry", [])
                if isinstance(item.get("metadata"), dict)
            )
            present = [
                value for value in values
                if value is not None and value != "" and value != [] and value != {}
            ]
            return self._check_record(
                assertion, index, bool(present),
                f"Observation contains {len(present)} non-empty {field_name} value(s)",
            )
        if check_type == "language_zh":
            chinese = sum("\u4e00" <= char <= "\u9fff" for char in content)
            return self._check_record(assertion, index, chinese >= 8, f"detected {chinese} Chinese characters")
        if check_type == "max_length":
            configured = spec.get("value", 1200)
            if isinstance(configured, dict):
                configured = configured.get("max", 1200)
            maximum = int(configured)
            return self._check_record(assertion, index, len(content) <= maximum, f"content length {len(content)} <= {maximum}")
        if check_type == "evidence_exists":
            evidence_type = str(spec.get("evidence_type", "screenshot"))
            matches = [
                item for item in self.context.get("evidence_registry", [])
                if item.get("type") == evidence_type and Path(str(item.get("path", ""))).is_file()
            ]
            return self._check_record(assertion, index, bool(matches), f"found {len(matches)} persisted {evidence_type} evidence item(s)")
        if check_type == "artifact_exists":
            matches = []
            for reference in self.context.get("artifacts", []):
                path = Path(str(reference))
                resolved = path if path.is_absolute() else self.repository.root / path
                if resolved.is_file():
                    matches.append(str(reference))
            return self._check_record(assertion, index, bool(matches), f"found {len(matches)} persisted Artifact(s)")
        if check_type == "references_evidence":
            paths = [str(item.get("path", "")) for item in self.context.get("evidence_registry", [])]
            matched = [path for path in paths if path and path in content]
            return self._check_record(assertion, index, bool(matched), f"content references {len(matched)} registered evidence path(s)")
        return None

    def _has_screenshot_evidence(self) -> bool:
        if any(item.get("type") == "screenshot" for item in self.context.get("evidence_registry", [])):
            return True
        observation = json.dumps(self.context.get("observation", {}), ensure_ascii=False).casefold()
        return any(marker in observation for marker in ("screenshot", "截图", ".png"))

    def _latest_content(self) -> str:
        for attempt in reversed(self.context.get("execution", {}).get("attempts", [])):
            content = str(attempt.get("result", {}).get("content", ""))
            if content:
                return content
        return ""

    @staticmethod
    def _action_text(action: dict[str, Any]) -> str:
        return " ".join([
            str(action.get("objective", "")),
            str(action.get("instruction", "")),
            json.dumps(action.get("assertions", []), ensure_ascii=False),
        ]).casefold()

    @staticmethod
    def _user_specified_number(user_text: str) -> bool:
        return bool(re.search(r"\d+\s*(?:字|字符|words?|characters?)", user_text, re.IGNORECASE))

    @staticmethod
    def _infer_check(description: str) -> dict[str, Any] | None:
        text = description.casefold()
        quoted = re.findall(r'["“「]([^"”」]{2,200})["”」]', description)
        categories = sum([
            any(marker in text for marker in ("截图", "screenshot", "证据", "evidence")),
            any(marker in text for marker in ("中文", "chinese", "语言", "language")),
            any(marker in text for marker in ("标题", "包含", "title", "contains")),
            any(marker in text for marker in ("简短", "长度", "length", "brief")),
        ])
        if categories > 1 and any(marker in text for marker in ("且", "同时", "并且", " and ")):
            return None
        if any(marker in text for marker in ("截图", "screenshot")) and any(
            marker in text for marker in ("存在", "保存", "保留", "persist", "retain")
        ):
            return {"type": "evidence_exists", "evidence_type": "screenshot"}
        if any(marker in text for marker in ("引用", "路径", "reference")) and any(
            marker in text for marker in ("证据", "截图", "evidence", "screenshot")
        ):
            return {"type": "references_evidence"}
        if quoted and any(marker in text for marker in ("包含", "标题", "contains", "title")):
            if any(marker in text for marker in ("实际", "观察", "记录", "访问", "observed", "actual")):
                return {"type": "observation_contains", "value": quoted[0]}
            return {"type": "contains", "value": quoted[0]}
        urls = URL_PATTERN.findall(description)
        if urls and any(marker in text for marker in ("访问", "可达", "打开", "visit", "reachable", "open")):
            return {"type": "observation_contains", "value": urls[0]}
        if any(marker in text for marker in ("artifact", "产物", "交付物")) and any(
            marker in text for marker in ("存在", "保存", "生成", "persist", "created")
        ):
            return {"type": "artifact_exists"}
        if any(marker in text for marker in ("中文", "chinese")):
            return {"type": "language_zh"}
        if any(marker in text for marker in ("非空", "non-empty")):
            return {"type": "non_empty"}
        return None

    @staticmethod
    def _covered_by_passed_action(description: str, passed_descriptions: list[str]) -> bool:
        keywords = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_.-]{3,}", description.casefold()))
        if not keywords:
            return False
        for candidate in passed_descriptions:
            other = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_.-]{3,}", candidate.casefold()))
            if len(keywords & other) >= min(2, len(keywords)):
                return True
        return False

    @staticmethod
    def _check_record(item: dict[str, Any], index: int, passed: bool, evidence: str) -> dict[str, Any]:
        return {
            "assertion_id": item.get("assertion_id") or item.get("criterion_id") or f"check-{index}",
            "description": str(item.get("description", "")),
            "required": bool(item.get("required", True)),
            "passed": passed,
            "evidence": evidence,
            "decision_source": "deterministic_control_plane",
        }
