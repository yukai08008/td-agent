from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


CHECK_TYPES = {
    "non_empty", "contains", "language_zh", "max_length",
    "observation_contains", "observation_field_non_empty",
    "artifact_exists", "evidence_exists",
    "references_evidence", "semantic",
}


def _check_spec_errors(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    check_type = value.get("type")
    if check_type not in CHECK_TYPES:
        return [f"{field}.type must be a supported deterministic check or semantic"]
    if check_type in {"contains", "observation_contains", "max_length"} and "value" not in value:
        return [f"{field}.value is required for {check_type}"]
    if check_type == "observation_field_non_empty" and not value.get("field"):
        return [f"{field}.field is required for observation_field_non_empty"]
    return []


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def validate_target(data: dict[str, Any]) -> None:
    errors = []
    if not _non_empty_list(data.get("positive")):
        errors.append("positive must be a non-empty list")
    if not _non_empty_list(data.get("negative")):
        errors.append("negative must be a non-empty list")
    criteria = data.get("acceptance_criteria")
    if not _non_empty_list(criteria):
        errors.append("acceptance_criteria must be a non-empty list")
    elif any(not isinstance(item, dict) or not item.get("description") for item in criteria):
        errors.append("each acceptance criterion must be an object with a description")
    else:
        for index, criterion in enumerate(criteria):
            errors.extend(_check_spec_errors(criterion.get("check"), f"acceptance_criteria[{index}].check"))
    if errors:
        raise ValidationError(errors)


def validate_observation(data: dict[str, Any]) -> None:
    facts = data.get("facts")
    errors = []
    if not _non_empty_list(facts):
        errors.append("facts must be a non-empty list")
    else:
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not fact.get("description"):
                errors.append(f"facts[{index}] needs a description")
            if not isinstance(fact, dict) or not fact.get("source_type"):
                errors.append(f"facts[{index}] needs a source_type")
    if errors:
        raise ValidationError(errors)


def validate_estimate(data: dict[str, Any]) -> None:
    errors = []
    if data.get("verdict") not in {"feasible", "needs_observation", "not_feasible"}:
        errors.append("verdict must be feasible, needs_observation, or not_feasible")
    for field in ("risks", "cost", "information_gaps"):
        if field not in data:
            errors.append(f"{field} is required")
    if errors:
        raise ValidationError(errors)


def validate_plan(data: dict[str, Any]) -> None:
    actions = data.get("actions")
    if not _non_empty_list(actions):
        raise ValidationError(["actions must be a non-empty list"])

    errors: list[str] = []
    ids = [item.get("action_id") for item in actions if isinstance(item, dict)]
    if len(ids) != len(actions) or any(not item for item in ids):
        errors.append("every action needs an action_id")
    if len(ids) != len(set(ids)):
        errors.append("action_id values must be unique")
    known = set(ids)
    for index, action in enumerate(actions):
        if not action.get("objective"):
            errors.append(f"actions[{index}] needs an objective")
        if not _non_empty_list(action.get("assertions")):
            errors.append(f"actions[{index}] needs at least one assertion")
        else:
            for assertion_index, assertion in enumerate(action["assertions"]):
                if not isinstance(assertion, dict):
                    errors.append(f"actions[{index}].assertions[{assertion_index}] must be an object")
                    continue
                errors.extend(_check_spec_errors(
                    assertion.get("check"),
                    f"actions[{index}].assertions[{assertion_index}].check",
                ))
        if action.get("executor") not in {None, "agent_response", "external"}:
            errors.append(f"actions[{index}].executor must be agent_response or external")
        try:
            max_attempts = int(action.get("max_attempts", 1))
        except (TypeError, ValueError):
            errors.append(f"actions[{index}].max_attempts must be an integer")
        else:
            if max_attempts < 1:
                errors.append(f"actions[{index}].max_attempts must be positive")
        for dependency in action.get("depends_on", []):
            if dependency not in known:
                errors.append(f"actions[{index}] has unknown dependency {dependency}")

    if known and not errors and _has_cycle(actions):
        errors.append("action dependencies contain a cycle")
    if errors:
        raise ValidationError(errors)


def _has_cycle(actions: list[dict[str, Any]]) -> bool:
    indegree = {item["action_id"]: 0 for item in actions}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for item in actions:
        for dependency in item.get("depends_on", []):
            outgoing[dependency].append(item["action_id"])
            indegree[item["action_id"]] += 1
    queue = deque(key for key, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in outgoing[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return visited != len(indegree)


def validate_action_result(data: dict[str, Any]) -> None:
    if not isinstance(data.get("result"), dict):
        raise ValidationError(["result must be an object"])


def required_checks_pass(checks: list[dict[str, Any]]) -> bool:
    return bool(checks) and all(
        item.get("passed") is True
        for item in checks
        if item.get("required", True)
    )
