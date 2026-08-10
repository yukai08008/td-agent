from __future__ import annotations

from typing import Any, Callable

from .state_machine import Graph

from .states import ACTIVE_STATES, TDState


Guard = Callable[[dict[str, Any]], bool]


def _target_ready(context: dict[str, Any]) -> bool:
    target = context.get("target", {})
    return all(isinstance(target.get(field), list) and bool(target[field]) for field in (
        "positive", "negative", "acceptance_criteria",
    ))


def _observation_ready(context: dict[str, Any]) -> bool:
    return bool(context.get("observation", {}).get("facts"))


def _estimate_is(verdict: str) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        return context.get("estimate", {}).get("verdict") == verdict
    guard.__name__ = f"estimate_is_{verdict}"
    return guard


def _plan_ready(context: dict[str, Any]) -> bool:
    plan = context.get("plan", {})
    action_ids = {
        item.get("action_id") for item in plan.get("actions", []) if isinstance(item, dict)
    }
    return (
        plan.get("status") == "active"
        and bool(action_ids)
        and context.get("execution", {}).get("current_action_id") in action_ids
    )


def _latest_action_check(context: dict[str, Any]) -> dict[str, Any]:
    checks = context.get("checks", {}).get("action_checks", [])
    return checks[-1] if checks else {}


def _action_was_submitted(context: dict[str, Any]) -> bool:
    current = context.get("execution", {}).get("current_action_id")
    attempts = context.get("execution", {}).get("attempts", [])
    return bool(current and attempts and attempts[-1].get("action_id") == current)


def _action_check_failed(context: dict[str, Any]) -> bool:
    return _latest_action_check(context).get("passed") is False


def _action_check_passed_with_pending(context: dict[str, Any]) -> bool:
    return _latest_action_check(context).get("passed") is True and any(
        action.get("status") != "passed"
        for action in context.get("plan", {}).get("actions", [])
    )


def _all_actions_passed(context: dict[str, Any]) -> bool:
    actions = context.get("plan", {}).get("actions", [])
    return (
        _latest_action_check(context).get("passed") is True
        and bool(actions)
        and all(action.get("status") == "passed" for action in actions)
    )


def _target_check_is(passed: bool) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        target_check = context.get("checks", {}).get("target_check") or {}
        return target_check.get("passed") is passed
    guard.__name__ = f"target_check_is_{str(passed).lower()}"
    return guard


def _failure_phase_is(phase: str) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        failure = context.get("recovery", {}).get("last_failure") or {}
        return failure.get("phase") == phase
    guard.__name__ = f"failure_phase_is_{phase}"
    return guard


def _recovery_decision_is(decision: str) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        recovery = context.get("recovery", {})
        value = recovery.get("decision") or {}
        within_budget = int(recovery.get("retry_count", 0)) <= int(recovery.get("retry_budget", 0))
        return value.get("type") == decision and (decision not in {
            "retry_targeting", "retry_action", "replan", "reobserve",
        } or within_budget)
    guard.__name__ = f"recovery_decision_is_{decision}"
    return guard


def _waiting_for(state: TDState) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        control = context.get("control", {})
        return (
            control.get("return_to") == state.value
            and bool(control.get("human_question"))
            and bool(control.get("waiting_reason"))
        )
    guard.__name__ = f"waiting_for_{state.value}"
    return guard


def _human_return_to(state: TDState) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        control = context.get("control", {})
        return control.get("return_to") == state.value and control.get("human_response") is not None
    guard.__name__ = f"human_return_to_{state.value}"
    return guard


def _paused_from(state: TDState) -> Guard:
    def guard(context: dict[str, Any]) -> bool:
        return context.get("control", {}).get("paused_from") == state.value
    guard.__name__ = f"paused_from_{state.value}"
    return guard


def _terminal_failure_ready(context: dict[str, Any]) -> bool:
    failure = context.get("recovery", {}).get("last_failure") or {}
    return bool(failure.get("terminal_at") and context.get("artifacts"))


def build_td_graph() -> Graph:
    transitions: list[tuple[TDState, TDState, dict[str, Any]]] = []

    def edge(source: TDState, target: TDState, event: str, guard: Guard | None = None) -> None:
        transitions.append((source, target, {"event": event, "guard": guard}))

    edge(TDState.IDLE, TDState.TARGETING, "start")
    edge(TDState.TARGETING, TDState.OBSERVING, "target_accepted", _target_ready)
    edge(TDState.TARGETING, TDState.WAITING_HUMAN, "target_needs_input", _waiting_for(TDState.TARGETING))
    edge(TDState.TARGETING, TDState.RECOVERING, "target_failed", _failure_phase_is("target"))

    edge(TDState.OBSERVING, TDState.ESTIMATING, "observation_accepted", _observation_ready)
    edge(TDState.OBSERVING, TDState.WAITING_HUMAN, "observe_needs_input", _waiting_for(TDState.OBSERVING))

    edge(TDState.ESTIMATING, TDState.DECIDING, "estimate_accepted", _estimate_is("feasible"))
    edge(TDState.ESTIMATING, TDState.OBSERVING, "estimate_requests_observation", _estimate_is("needs_observation"))
    edge(TDState.ESTIMATING, TDState.FAILED, "estimate_not_feasible", _terminal_failure_ready)
    edge(TDState.ESTIMATING, TDState.WAITING_HUMAN, "estimate_needs_input", _waiting_for(TDState.ESTIMATING))

    edge(TDState.DECIDING, TDState.ACTING, "plan_accepted", _plan_ready)
    edge(TDState.DECIDING, TDState.WAITING_HUMAN, "decide_needs_input", _waiting_for(TDState.DECIDING))

    edge(TDState.ACTING, TDState.CHECKING_ACTION, "action_submitted", _action_was_submitted)
    edge(TDState.ACTING, TDState.WAITING_HUMAN, "act_needs_input", _waiting_for(TDState.ACTING))

    edge(TDState.CHECKING_ACTION, TDState.ACTING, "advance_action", _action_check_passed_with_pending)
    edge(TDState.CHECKING_ACTION, TDState.CHECKING_TARGET, "actions_completed", _all_actions_passed)
    edge(TDState.CHECKING_ACTION, TDState.RECOVERING, "action_failed", _action_check_failed)
    edge(
        TDState.CHECKING_ACTION, TDState.WAITING_HUMAN,
        "action_check_needs_input", _waiting_for(TDState.CHECKING_ACTION),
    )

    edge(TDState.CHECKING_TARGET, TDState.SUCCEEDED, "target_passed", _target_check_is(True))
    edge(TDState.CHECKING_TARGET, TDState.RECOVERING, "target_failed", _target_check_is(False))
    edge(
        TDState.CHECKING_TARGET, TDState.WAITING_HUMAN,
        "target_check_needs_input", _waiting_for(TDState.CHECKING_TARGET),
    )

    for decision, target in {
        "retry_targeting": TDState.TARGETING,
        "retry_action": TDState.ACTING,
        "replan": TDState.DECIDING,
        "reobserve": TDState.OBSERVING,
        "escalate": TDState.WAITING_HUMAN,
        "give_up": TDState.FAILED,
    }.items():
        guard = _waiting_for(TDState.RECOVERING) if decision == "escalate" else _recovery_decision_is(decision)
        edge(TDState.RECOVERING, target, decision, guard)

    for state, event in {
        TDState.TARGETING: "target_input_received",
        TDState.OBSERVING: "observation_input_received",
        TDState.ESTIMATING: "estimate_input_received",
        TDState.DECIDING: "decision_input_received",
        TDState.ACTING: "act_input_received",
        TDState.CHECKING_ACTION: "action_check_input_received",
        TDState.CHECKING_TARGET: "target_check_input_received",
        TDState.RECOVERING: "recovery_input_received",
    }.items():
        edge(TDState.WAITING_HUMAN, state, event, _human_return_to(state))

    for state in ACTIVE_STATES:
        edge(state, TDState.PAUSED, f"pause_from_{state.value}", _paused_from(state))
        edge(state, TDState.CANCELLED, "cancel")
        edge(state, TDState.FAILED, "runtime_budget_exhausted", _terminal_failure_ready)
        edge(TDState.PAUSED, state, f"resume_to_{state.value}")

    for state in {
        TDState.DECIDING, TDState.ACTING, TDState.CHECKING_ACTION, TDState.CHECKING_TARGET,
    }:
        edge(state, TDState.OBSERVING, "user_reobserve_requested")
        edge(state, TDState.DECIDING, "user_replan_requested")
    edge(TDState.WAITING_HUMAN, TDState.OBSERVING, "user_reobserve_requested")
    edge(TDState.WAITING_HUMAN, TDState.DECIDING, "user_replan_requested")

    edge(TDState.IDLE, TDState.CANCELLED, "cancel")
    edge(TDState.PAUSED, TDState.CANCELLED, "cancel")
    return Graph(transitions=transitions, initial=TDState.IDLE)
