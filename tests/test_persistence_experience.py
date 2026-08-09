from __future__ import annotations

import json

from toe_dac import TDService
from toe_dac.experience import ExperienceStore
from toe_dac.states import TDState


def test_reload_restores_non_initial_machine_state(service, repository, target):
    service.start()
    service.submit_target(target)
    loaded = TDService.load(repository, service.context["user_thread_id"], service.context["td_id"])
    assert loaded.state == TDState.OBSERVING
    assert loaded.context["revision"] == service.context["revision"]


def test_success_transition_links_event_and_operation(service):
    service.start()
    event = service.repository.event_log(service.context)[-1]
    operation = service.repository.operation_log(service.context)[-1]
    assert event["event_id"] == operation["event_id"]


def test_experience_tracks_match_adopt_use_success_and_failure(tmp_path):
    store = ExperienceStore(tmp_path)
    signature = {
        "phase": "act",
        "cause": "assertion_failed",
        "target_summary": "部署容器服务",
        "action_summary": "启动容器",
    }
    experience_id = store.observe_exception(
        scope_id="scope_a", user_thread_id="ut_a", td_id="td_a", session_id="ss_a",
        exception=signature,
    )
    matches = store.match("scope_a", dict(signature))
    assert matches[0]["experience_id"] == experience_id
    store.adopt(experience_id, "scope_a", "same environment", 0.9)
    store.treatment_started(experience_id, "scope_a", "increase startup grace period")
    store.treatment_finished(experience_id, "scope_a", False)
    store.treatment_started(experience_id, "scope_a", "inspect container logs")
    store.treatment_finished(experience_id, "scope_a", True)
    stats = store.stats(experience_id)
    assert stats == {
        "match_count": 1,
        "adopt_count": 1,
        "use_count": 2,
        "success_count": 1,
        "failure_count": 1,
        "effectiveness": 0.5,
    }


def test_experience_scope_isolation(tmp_path):
    store = ExperienceStore(tmp_path)
    experience_id = store.observe_exception(
        scope_id="private_a", user_thread_id="ut_a", td_id="td_a", session_id="ss_a",
        exception={"phase": "act", "cause": "timeout", "message": "docker timeout"},
    )
    assert store.match("private_b", {"phase": "act", "cause": "timeout"}) == []
    assert store.match("private_a", {"phase": "act", "cause": "timeout"})[0]["experience_id"] == experience_id


def test_experience_index_can_be_rebuilt(tmp_path):
    store = ExperienceStore(tmp_path)
    experience_id = store.observe_exception(
        scope_id="scope", user_thread_id="ut", td_id="td", session_id="ss",
        exception={"phase": "target", "cause": "timeout"},
    )
    store.treatment_started(experience_id, "scope", "retry")
    store.treatment_finished(experience_id, "scope", False)
    store.index_path.unlink()
    store.rebuild_index()
    assert store.stats(experience_id)["use_count"] == 1
    assert store.stats(experience_id)["failure_count"] == 1


def test_state_file_is_valid_json_after_each_commit(service):
    service.start()
    path = service.repository.td_dir(service.context["user_thread_id"], service.context["td_id"]) / "state.json"
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["state"] == "targeting"
