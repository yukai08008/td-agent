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


def test_system_experience_matches_across_threads_but_private_experience_does_not(tmp_path):
    store = ExperienceStore(tmp_path)
    signature = {
        "phase": "decide",
        "cause": "semantic_validation_failed",
        "error_code": "plan.screenshot_relocation_conflict",
    }
    system_id = store.observe_exception(
        scope_id="ut_source", user_thread_id="ut_source", td_id="td_a", session_id="ss_a",
        exception={**signature, "message": "negated copy rule was misclassified"},
        signature=signature, visibility="system",
    )
    store.record_resolution(system_id, "ut_source", {
        "type": "code_fix", "summary": "safe summary",
        "source_refs": {"artifact_refs": ["private/path.json"]},
    })
    private_id = store.observe_exception(
        scope_id="ut_source", user_thread_id="ut_source", td_id="td_b", session_id="ss_b",
        exception={"phase": "decide", "cause": "private_context"},
        visibility="thread",
    )

    matches = store.match("ut_other", signature)

    assert [item["experience_id"] for item in matches] == [system_id]
    assert matches[0]["exception"] == {}
    assert matches[0]["signature"] == signature
    assert matches[0]["resolutions"] == [{
        "type": "code_fix", "summary": "safe summary",
        "recorded_at": matches[0]["resolutions"][0]["recorded_at"],
    }]
    assert "source_refs" not in matches[0]["resolutions"][0]
    assert private_id not in {item["experience_id"] for item in matches}


def test_experience_preserves_treatment_trace_resolution_and_source_refs(tmp_path):
    store = ExperienceStore(tmp_path)
    experience_id = store.observe_exception(
        scope_id="ut_a", user_thread_id="ut_a", td_id="td_a", session_id="ss_a",
        exception={"phase": "decide", "cause": "semantic_validation_failed"},
        visibility="system",
        signature={"phase": "decide", "cause": "semantic_validation_failed", "error_code": "rule.a"},
        source_refs={"operation_ids": ["op_1"]},
    )
    treatment_id = store.treatment_started(
        experience_id, "ut_a", "repair_plan", {"source_refs": {"operation_ids": ["op_2"]}},
    )
    store.treatment_finished(
        experience_id, "ut_a", True,
        {"source_refs": {"artifact_refs": ["artifact.json"]}},
        treatment_id=treatment_id,
    )
    store.record_resolution(experience_id, "ut_a", {
        "type": "code_fix", "version": "0.6.1", "commit": "b51c52d",
        "regression_test": "test_report_plan_that_forbids_screenshot_relocation_is_valid",
    })

    item = store.get(experience_id)

    assert item["treatments"][0]["status"] == "succeeded"
    assert item["source_refs"]["operation_ids"] == ["op_1", "op_2"]
    assert item["source_refs"]["artifact_refs"] == ["artifact.json"]
    assert item["resolutions"][0]["commit"] == "b51c52d"
    store.index_path.unlink()
    rebuilt = store.rebuild_index()["experiences"][experience_id]
    assert rebuilt == item


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


def test_reading_experience_index_does_not_rewrite_it(tmp_path):
    store = ExperienceStore(tmp_path)
    store.observe_exception(
        scope_id="scope", user_thread_id="ut", td_id="td", session_id="ss",
        exception={"phase": "target", "cause": "timeout"},
    )
    before = store.index_path.stat().st_mtime_ns

    assert store.index()["experiences"]

    assert store.index_path.stat().st_mtime_ns == before


def test_state_file_is_valid_json_after_each_commit(service):
    service.start()
    path = service.repository.td_dir(service.context["user_thread_id"], service.context["td_id"]) / "state.json"
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["state"] == "targeting"
