from __future__ import annotations

import pytest

from toe_dac.e2e import CaseRegistry, E2ERunner


def test_case_registry_lists_first_executable_cases():
    cases = CaseRegistry().list()
    assert [case.case_id for case in cases] == ["LIVE-001", "LIVE-002", "LIVE-006"]
    assert all(CaseRegistry.fixture_root(case).is_dir() for case in cases)


def test_live_002_mock_runs_complete_workspace_flow(tmp_path):
    runner = E2ERunner(tmp_path / "data")
    record = runner.run("LIVE-002", mode="mock")
    assert record["status"] == "succeeded"
    assert record["td_state"] == "succeeded"
    assert record["metrics"]["actions"] == 3
    assert record["artifacts"]["baseline"]["exit_code"] == 1
    assert record["artifacts"]["final"]["exit_code"] == 0
    report = runner.report(record["run_id"])
    assert report["target_passed"] is True


def test_live_006_rejects_invalid_plan_then_repairs(tmp_path):
    record = E2ERunner(tmp_path / "data").run("LIVE-006", mode="mock")
    assert record["status"] == "succeeded"
    assert record["injected_failure"]["type"] == "invalid_model_output"
    assert "actions must be a non-empty list" in record["injected_failure"]["errors"]


def test_live_001_waits_and_resumes_in_new_session(tmp_path):
    runner = E2ERunner(tmp_path / "data")
    first = runner.run("LIVE-001", mode="mock")
    assert first["status"] == "waiting_human"
    resumed = runner.resume(first["run_id"], {
        "scope": "只补充 README",
        "acceptance": "包含安装、运行和测试说明",
    })
    assert resumed["status"] == "succeeded"
    assert runner.report(first["run_id"])["target_passed"] is True


def test_live_mode_rejects_cases_without_safe_executor(tmp_path):
    with pytest.raises(NotImplementedError, match="supports LIVE-001 only"):
        E2ERunner(tmp_path / "data").run("LIVE-002", mode="live", model_id="glm-5")


def test_live_001_requires_model_and_model_config(tmp_path):
    with pytest.raises(ValueError, match="requires --model and --model-config"):
        E2ERunner(tmp_path / "data").run("LIVE-001", mode="live", model_config_path=None)
