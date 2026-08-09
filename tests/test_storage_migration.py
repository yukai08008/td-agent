from __future__ import annotations

import json
import stat

import pytest

from toe_dac.storage import atomic_write_json, atomic_write_text, append_jsonl
from toe_dac.storage_migration import StorageMigrator


def _legacy_thread(repository):
    source = repository.legacy_threads_root / "ut_legacy"
    atomic_write_json(source / "thread.json", {
        "user_thread_id": "ut_legacy",
        "active_td_id": "td_root",
        "root_td_id": "td_root",
        "td_ids": ["td_root"],
        "latest_session_id": "ss_legacy",
        "session_ids": ["ss_legacy"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T01:00:00+00:00",
    })
    atomic_write_json(source / "sessions" / "ss_legacy.json", {
        "session_id": "ss_legacy", "user_thread_id": "ut_legacy", "td_id": "td_root",
        "status": "succeeded", "started_at": "2026-01-01T00:00:00+00:00",
    })
    state = {
        "user_thread_id": "ut_legacy", "td_id": "td_root", "parent_td_id": None,
        "session_id": "ss_legacy", "state": "succeeded", "revision": 3,
        "target": {"positive": ["生成报告"]},
        "artifacts": ["threads/ut_legacy/artifacts/td_root/result.md"],
    }
    atomic_write_json(source / "td" / "td_root" / "state.json", state)
    append_jsonl(source / "td" / "td_root" / "event.jsonl", {
        "event_id": "ev_1", "event": "target_passed", "session_id": "ss_legacy",
    })
    append_jsonl(source / "td" / "td_root" / "operation.jsonl", {
        "event_id": "op_1", "operation": "generate", "status": "succeeded",
        "session_id": "ss_legacy",
        "evidence_ref": "threads/ut_legacy/td/td_root/trace/ss_legacy/op_1.json",
    })
    atomic_write_json(source / "td" / "td_root" / "trace" / "ss_legacy" / "op_1.json", {
        "operation_id": "op_1", "session_id": "ss_legacy", "evidence": {"ok": True},
    })
    append_jsonl(source / "messages.jsonl", {
        "message_id": "msg_1", "role": "user", "content": "生成报告",
        "td_id": "td_root", "session_id": "ss_legacy", "timestamp": "2026-01-01T00:00:01+00:00",
    })
    atomic_write_text(source / "artifacts" / "td_root" / "result.md", "# result\n")
    return source


def test_storage_v2_migration_dry_run_writes_nothing(repository):
    source = _legacy_thread(repository)

    report = StorageMigrator(repository, "0.5.0").migrate(dry_run=True)

    assert report["threads"][0]["status"] == "ready"
    assert source.exists()
    assert not (repository.threads_root / "ut_legacy").exists()


def test_storage_v2_migration_preserves_and_verifies_data(repository):
    source = _legacy_thread(repository)

    report = StorageMigrator(repository, "0.5.0").migrate(dry_run=False)

    item = report["threads"][0]
    assert item["status"] == "migrated"
    assert item["verification"]["passed"] is True
    assert source.exists()
    target = repository.threads_root / "ut_legacy"
    assert (target / "meta.json").exists()
    assert (target / "state.json").exists()
    assert (target / "logs" / "event.jsonl").exists()
    assert (target / "logs" / "opr.jsonl").exists()
    session = target / "trace" / "sessions" / "ss_legacy"
    assert "生成报告" in (session / "messages.jsonl").read_text()
    assert '"ok": true' in (session / "evidence.jsonl").read_text()
    assert (target / "artifacts" / "td_root" / "result.md").read_text() == "# result\n"
    state = json.loads((target / "td" / "td_root" / "state.json").read_text())
    assert state["artifacts"] == ["user-threads/ut_legacy/artifacts/td_root/result.md"]
    assert repository.find_session("ss_legacy")["user_thread_id"] == "ut_legacy"
    credential_mode = stat.S_IMODE(repository.thread_credentials_dir("ut_legacy").stat().st_mode)
    assert credential_mode == 0o700


def test_credentials_cannot_escape_or_follow_symlink(repository, tmp_path):
    directory = repository.thread_credentials_dir("ut_secure", create=True)
    secret = directory / "token"
    secret.write_text("safe")
    outside = tmp_path / "outside"
    outside.write_text("unsafe")
    (directory / "linked").symlink_to(outside)

    assert repository.resolve_thread_credential("ut_secure", "token") == secret.resolve()
    with pytest.raises(ValueError, match="single safe file name"):
        repository.resolve_thread_credential("ut_secure", "../outside")
    with pytest.raises(ValueError, match="symlinks"):
        repository.resolve_thread_credential("ut_secure", "linked")
