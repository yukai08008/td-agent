from __future__ import annotations

import pytest

from conftest import advance_to_deciding
from toe_dac.states import TDState
from toe_dac.validation import ValidationError


PASS = [{"assertion_id": "check", "required": True, "passed": True}]
FAIL = [{"assertion_id": "check", "required": True, "passed": False}]


def test_happy_path_keeps_action_and_target_checks_separate(
    service, target, observation, estimate, two_action_plan,
):
    advance_to_deciding(service, target, observation, estimate)
    service.submit_plan(two_action_plan)
    assert service.state == TDState.ACTING
    assert service.context["execution"]["current_action_id"] == "a_001"

    service.submit_action_result({"result": {"created": True}})
    service.check_action(PASS)
    assert service.state == TDState.ACTING
    assert service.context["execution"]["current_action_id"] == "a_002"

    service.submit_action_result({"result": {"title": True}})
    service.check_action(PASS)
    assert service.state == TDState.CHECKING_TARGET
    assert service.state != TDState.SUCCEEDED

    service.check_target(PASS)
    assert service.state == TDState.SUCCEEDED


def test_invalid_plan_cycle_is_rejected(service, target, observation, estimate, two_action_plan):
    advance_to_deciding(service, target, observation, estimate)
    two_action_plan["actions"][0]["depends_on"] = ["a_002"]
    with pytest.raises(ValidationError, match="cycle"):
        service.submit_plan(two_action_plan)
    assert service.state == TDState.DECIDING


def test_invalid_plan_attempt_type_is_rejected(service, target, observation, estimate, two_action_plan):
    advance_to_deciding(service, target, observation, estimate)
    two_action_plan["actions"][0]["max_attempts"] = "many"
    with pytest.raises(ValidationError, match="must be an integer"):
        service.submit_plan(two_action_plan)
    assert service.state == TDState.DECIDING


def test_action_failure_retry_success_creates_two_attempts(
    service, target, observation, estimate, two_action_plan,
):
    advance_to_deciding(service, target, observation, estimate)
    one_action = dict(two_action_plan)
    one_action["actions"] = [two_action_plan["actions"][0]]
    service.submit_plan(one_action)
    service.submit_action_result({"result": {"created": False}})
    service.check_action(FAIL)
    assert service.state == TDState.RECOVERING
    experience_id = service.context["recovery"]["active_experience_id"]

    service.recover("retry_action", reason="retry with corrected input")
    service.submit_action_result({"result": {"created": True}})
    service.check_action(PASS)
    assert service.state == TDState.CHECKING_TARGET
    assert len(service.context["execution"]["attempts"]) == 2
    assert service.experience.stats(experience_id)["success_count"] == 1


def test_target_failure_requires_recovery_not_success(
    service, target, observation, estimate, two_action_plan,
):
    advance_to_deciding(service, target, observation, estimate)
    one_action = dict(two_action_plan)
    one_action["actions"] = [two_action_plan["actions"][0]]
    service.submit_plan(one_action)
    service.submit_action_result({"result": {"created": True}})
    service.check_action(PASS)
    service.check_target(FAIL)
    assert service.state == TDState.RECOVERING
    assert service.context["checks"]["target_check"]["passed"] is False


def test_target_failure_experience_is_not_resolved_by_first_action_check(
    service, target, observation, estimate, two_action_plan,
):
    advance_to_deciding(service, target, observation, estimate)
    one_action = dict(two_action_plan)
    one_action["actions"] = [two_action_plan["actions"][0]]
    service.submit_plan(one_action)
    service.submit_action_result({"result": {"created": True}})
    service.check_action(PASS)
    service.check_target(FAIL)
    experience_id = service.context["recovery"]["active_experience_id"]
    service.recover("replan", reason="add a corrective action")
    service.submit_plan(one_action)
    service.submit_action_result({"result": {"created": True}})
    service.check_action(PASS)
    assert service.experience.stats(experience_id)["success_count"] == 0
    service.check_target(PASS)
    assert service.experience.stats(experience_id)["success_count"] == 1


def test_retry_budget_is_enforced(service):
    service.start()
    for expected_count in (1, 2):
        service.fail_targeting("timeout", "temporary")
        service.recover("retry_targeting")
        assert service.context["recovery"]["retry_count"] == expected_count
    service.fail_targeting("timeout", "again")
    with pytest.raises(ValidationError, match="budget exhausted"):
        service.recover("retry_targeting")
    assert service.state == TDState.RECOVERING


def test_failure_can_escalate_to_human_and_return(service):
    service.start()
    service.fail_targeting("execution_error", "cannot decide")
    service.recover("escalate", reason="need authorization", human_question="Replan?")
    assert service.state == TDState.WAITING_HUMAN
    service.human_reply({"decision": "replan"})
    assert service.state == TDState.RECOVERING
