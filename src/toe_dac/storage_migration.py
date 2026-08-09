from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .context import utc_now
from .storage import TDRepository, atomic_write_json, atomic_write_text, read_jsonl


def _jsonl_text(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class StorageMigrator:
    """Explicit, verified migration from the legacy threads layout to Storage V2."""

    def __init__(self, repository: TDRepository, version: str):
        self.repository = repository
        self.version = version

    def migrate(self, *, thread_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        sources = sorted(self.repository.legacy_threads_root.glob("*/thread.json"))
        if thread_id:
            sources = [path for path in sources if path.parent.name == thread_id]
            if not sources:
                raise ValueError(f"legacy User Thread not found: {thread_id}")
        reports = [self._migrate_one(path.parent, dry_run=dry_run) for path in sources]
        return {
            "storage_version": 2,
            "dry_run": dry_run,
            "thread_count": len(reports),
            "threads": reports,
        }

    def _migrate_one(self, source: Path, *, dry_run: bool) -> dict[str, Any]:
        thread_id = source.name
        target = self.repository.threads_root / thread_id
        inventory = self._inventory(source)
        if target.exists():
            verification = self._verify(target, inventory)
            if not verification["passed"]:
                raise RuntimeError(f"existing V2 verification failed for {thread_id}: {verification}")
            if not dry_run:
                self._ensure_v2_metadata(target)
            return {
                **inventory, "status": "already_migrated", "target": str(target),
                "verification": verification,
            }
        if dry_run:
            return {**inventory, "status": "ready", "target": str(target)}

        self.repository.threads_root.mkdir(parents=True, exist_ok=True)
        temporary = self.repository.threads_root / f".{thread_id}.migrating-{uuid.uuid4().hex[:8]}"
        try:
            self._build_v2(source, temporary, inventory)
            verification = self._verify(temporary, inventory)
            if not verification["passed"]:
                raise RuntimeError(f"migration verification failed for {thread_id}: {verification}")
            os.replace(temporary, target)
            credential_directory = self.repository.credentials_root / "user-threads" / thread_id
            credential_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(credential_directory, 0o700)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {
            **inventory,
            "status": "migrated",
            "target": str(target),
            "verification": verification,
            "legacy_retained": str(source),
        }

    def _inventory(self, source: Path) -> dict[str, Any]:
        info = json.loads((source / "thread.json").read_text(encoding="utf-8"))
        sessions = self._sessions(source)
        messages = read_jsonl(source / "messages.jsonl")
        events: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        evidence_count = 0
        for td_directory in sorted((source / "td").glob("*")):
            if not td_directory.is_dir():
                continue
            events.extend(read_jsonl(td_directory / "event.jsonl"))
            operations.extend(read_jsonl(td_directory / "operation.jsonl"))
            evidence_count += len(list((td_directory / "trace").glob("*/op_*.json")))
        artifact_hashes = {
            str(path.relative_to(source / "artifacts")): _sha256(path)
            for path in sorted((source / "artifacts").rglob("*"))
            if path.is_file()
        } if (source / "artifacts").exists() else {}
        return {
            "user_thread_id": info["user_thread_id"],
            "td_count": len(list((source / "td").glob("*/state.json"))),
            "session_count": len(sessions),
            "message_count": len(messages),
            "event_count": len(events),
            "operation_count": len(operations),
            "evidence_count": evidence_count,
            "artifact_count": len(artifact_hashes),
            "artifact_hashes": artifact_hashes,
        }

    @staticmethod
    def _sessions(source: Path) -> dict[str, dict[str, Any]]:
        sessions: dict[str, dict[str, Any]] = {}
        paths = [*(source / "td").glob("*/sessions/*.json"), *(source / "sessions").glob("*.json")]
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            record.setdefault("td_id", path.parent.parent.name if path.parent.name == "sessions" else None)
            sessions[record["session_id"]] = record
        return sessions

    def _build_v2(self, source: Path, target: Path, inventory: dict[str, Any]) -> None:
        legacy_info = json.loads((source / "thread.json").read_text(encoding="utf-8"))
        td_states: dict[str, dict[str, Any]] = {}
        for state_path in sorted((source / "td").glob("*/state.json")):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["artifacts"] = [self._rewrite_ref(item) for item in state.get("artifacts", [])]
            td_states[state["td_id"]] = state
            atomic_write_json(target / "td" / state["td_id"] / "state.json", state)

        root_state = td_states.get(legacy_info.get("root_td_id")) or next(iter(td_states.values()), {})
        target_summary = ""
        positives = (root_state.get("target") or {}).get("positive") or []
        if positives:
            target_summary = str(positives[0])
        meta = {
            "user_thread_id": legacy_info["user_thread_id"],
            "title": target_summary,
            "target_summary": target_summary,
            "created_at": legacy_info.get("created_at"),
            "updated_at": legacy_info.get("updated_at"),
            "host": {"hostname": platform.node()},
            "service_name": "td-agent",
            "runtime": {"platform": platform.platform(), "python": sys.version.split()[0]},
            "created_version": self.version,
            "last_review": None,
            "storage_version": 2,
            "migration": {
                "from": "legacy-threads-v1",
                "migrated_at": utc_now(),
                "source": str(source),
                "legacy_session_ids_preserved": True,
            },
        }
        thread_state = {
            key: legacy_info.get(key)
            for key in (
                "user_thread_id", "active_td_id", "root_td_id", "td_ids",
                "latest_session_id", "session_ids", "updated_at",
            )
        }
        thread_state["revision"] = int(legacy_info.get("revision", 0))
        thread_state["storage_version"] = 2
        atomic_write_json(target / "meta.json", meta)
        atomic_write_json(target / "state.json", thread_state)

        events: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        evidence_by_session: dict[str, list[dict[str, Any]]] = {}
        for td_directory in sorted((source / "td").glob("*")):
            if not td_directory.is_dir():
                continue
            td_id = td_directory.name
            for item in read_jsonl(td_directory / "event.jsonl"):
                item.setdefault("td_id", td_id)
                item.setdefault("request_id", None)
                events.append(item)
            for item in read_jsonl(td_directory / "operation.jsonl"):
                item.setdefault("td_id", td_id)
                item.setdefault("request_id", None)
                if item.get("evidence_ref"):
                    item["evidence_ref"] = str(
                        Path("user-threads") / legacy_info["user_thread_id"]
                        / "trace" / "sessions" / str(item.get("session_id")) / "evidence.jsonl"
                    )
                operations.append(item)
            for evidence_path in sorted((td_directory / "trace").glob("*/op_*.json")):
                try:
                    record = json.loads(evidence_path.read_text(encoding="utf-8"))
                    evidence_by_session.setdefault(record["session_id"], []).append(record)
                except (json.JSONDecodeError, OSError, KeyError):
                    continue
        atomic_write_text(target / "logs" / "event.jsonl", _jsonl_text(events))
        atomic_write_text(target / "logs" / "opr.jsonl", _jsonl_text(operations))

        all_messages = read_jsonl(source / "messages.jsonl")
        sessions = self._sessions(source)
        for selected, session in sessions.items():
            session_directory = target / "trace" / "sessions" / selected
            atomic_write_json(session_directory / "session.json", session)
            session_messages = [item for item in all_messages if item.get("session_id") == selected]
            atomic_write_text(session_directory / "messages.jsonl", _jsonl_text(session_messages))
            evidence = evidence_by_session.get(selected, [])
            atomic_write_text(session_directory / "evidence.jsonl", _jsonl_text(evidence))
            legacy_trace = source / "td" / str(session.get("td_id")) / "trace" / selected
            history = legacy_trace / ".input-history"
            if history.exists():
                shutil.copy2(history, session_directory / ".input-history")
            screenshot_sources = [legacy_trace / "screenshots", legacy_trace / "view" / "screenshots"]
            for screenshot_source in screenshot_sources:
                if screenshot_source.exists():
                    shutil.copytree(
                        screenshot_source, session_directory / "screenshots", dirs_exist_ok=True,
                    )
            screenshot_index = legacy_trace / "view" / "screenshots.jsonl"
            if screenshot_index.exists():
                shutil.copy2(screenshot_index, session_directory / "screenshots.jsonl")

        if (source / "artifacts").exists():
            shutil.copytree(source / "artifacts", target / "artifacts", dirs_exist_ok=True)

    def _verify(self, target: Path, expected: dict[str, Any]) -> dict[str, Any]:
        actual = {
            "td_count": len(list((target / "td").glob("*/state.json"))),
            "session_count": len(list((target / "trace" / "sessions").glob("*/session.json"))),
            "message_count": sum(
                len(read_jsonl(path)) for path in (target / "trace" / "sessions").glob("*/messages.jsonl")
            ),
            "event_count": len(read_jsonl(target / "logs" / "event.jsonl")),
            "operation_count": len(read_jsonl(target / "logs" / "opr.jsonl")),
            "evidence_count": sum(
                len(read_jsonl(path)) for path in (target / "trace" / "sessions").glob("*/evidence.jsonl")
            ),
        }
        actual_hashes = {
            str(path.relative_to(target / "artifacts")): _sha256(path)
            for path in sorted((target / "artifacts").rglob("*"))
            if path.is_file()
        } if (target / "artifacts").exists() else {}
        expected_counts = {key: expected[key] for key in actual}
        return {
            "passed": actual == expected_counts and actual_hashes == expected["artifact_hashes"],
            "expected": expected_counts,
            "actual": actual,
            "artifact_hashes_match": actual_hashes == expected["artifact_hashes"],
        }

    @staticmethod
    def _ensure_v2_metadata(target: Path) -> None:
        for name in ("meta.json", "state.json"):
            path = target / name
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("storage_version") != 2:
                data["storage_version"] = 2
                atomic_write_json(path, data)

    @staticmethod
    def _rewrite_ref(reference: str) -> str:
        return reference.replace("threads/", "user-threads/", 1) if reference.startswith("threads/") else reference
