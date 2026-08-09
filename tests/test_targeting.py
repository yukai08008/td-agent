from __future__ import annotations

import pytest

from toe_dac.states import TDState
from toe_dac.validation import ValidationError


def test_valid_target_advances_and_creates_revision(service, target):
    service.start()
    service.submit_target(target)
    assert service.state == TDState.OBSERVING
    assert service.context["target"]["revision"] == 1
    assert len(service.context["target_revisions"]) == 1


@pytest.mark.parametrize("missing", ["positive", "negative", "acceptance_criteria"])
def test_invalid_target_is_rejected_without_transition_or_budget(service, target, missing):
    service.start()
    invalid = dict(target)
    invalid[missing] = []
    revision = service.context["revision"]
    budget_count = service.context["recovery"]["retry_count"]
    with pytest.raises(ValidationError):
        service.submit_target(invalid)
    assert service.state == TDState.TARGETING
    assert service.context["revision"] == revision
    assert service.context["recovery"]["retry_count"] == budget_count
    assert service.repository.operation_log(service.context)[-1]["status"] == "rejected"
    assert service.repository.event_log(service.context)[-1]["event"] != "target_rejected"


def test_target_needs_human_and_resumes_across_reload(service, repository):
    service.start()
    service.target_needs_input("标题是什么？", "缺少标题")
    assert service.state == TDState.WAITING_HUMAN

    loaded = service.load(repository, service.context["user_thread_id"], service.context["td_id"])
    assert loaded.state == TDState.WAITING_HUMAN
    assert loaded.context["control"]["return_to"] == "targeting"
    loaded.human_reply({"title": "TOE-DAC"})
    assert loaded.state == TDState.TARGETING
    assert loaded.context["control"]["human_response"]["title"] == "TOE-DAC"


def test_target_execution_failure_enters_recovery_and_can_retry(service):
    service.start()
    service.fail_targeting("timeout", "model timed out")
    assert service.state == TDState.RECOVERING
    assert service.context["recovery"]["last_failure"]["cause"] == "timeout"
    experience_id = service.context["recovery"]["active_experience_id"]
    service.recover("retry_targeting", reason="transient timeout")
    assert service.state == TDState.TARGETING
    assert service.context["recovery"]["retry_count"] == 1
    service.submit_target({
        "positive": ["目标"],
        "negative": ["不越界"],
        "acceptance_criteria": [{"description": "通过", "required": True}],
    })
    assert service.experience.stats(experience_id)["success_count"] == 1


def test_malformed_acceptance_criterion_is_rejected(service, target):
    service.start()
    target["acceptance_criteria"] = ["not-an-object"]
    with pytest.raises(ValidationError, match="must be an object"):
        service.submit_target(target)
    assert service.state == TDState.TARGETING
