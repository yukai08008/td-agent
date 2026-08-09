from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .context import new_context, utc_now


ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _validate_id(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
    return records


class TDRepository:
    def __init__(self, root: str | Path = "data"):
        self.root = Path(root)

    def td_dir(self, user_thread_id: str, td_id: str) -> Path:
        return (
            self.root
            / "threads"
            / _validate_id(user_thread_id)
            / "td"
            / _validate_id(td_id)
        )

    def thread_dir(self, user_thread_id: str) -> Path:
        return self.root / "threads" / _validate_id(user_thread_id)

    def thread_info(self, user_thread_id: str) -> dict[str, Any] | None:
        path = self.thread_dir(user_thread_id) / "thread.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def active_td_id(self, user_thread_id: str) -> str | None:
        info = self.thread_info(user_thread_id)
        return info.get("active_td_id") if info else None

    def list_threads(self) -> list[dict[str, Any]]:
        threads_root = self.root / "threads"
        if not threads_root.exists():
            return []
        result = []
        for path in sorted(threads_root.glob("*/thread.json")):
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return result

    def create(self, user_thread_id: str | None = None, retry_budget: int = 3) -> dict[str, Any]:
        thread_id = user_thread_id or short_id("ut")
        td_id = short_id("td")
        session_id = short_id("ss")
        context = new_context(thread_id, td_id, session_id, retry_budget)
        directory = self.td_dir(thread_id, td_id)
        if directory.exists():
            raise FileExistsError(directory)
        directory.mkdir(parents=True)
        atomic_write_json(directory / "state.json", context)
        append_jsonl(directory / "event.jsonl", {
            "event_id": short_id("ev"),
            "event": "td_created",
            "state": "idle",
            "timestamp": utc_now(),
        })
        self._register_td(thread_id, td_id)
        self._register_session(context)
        return context

    def _register_td(self, user_thread_id: str, td_id: str) -> None:
        path = self.thread_dir(user_thread_id) / "thread.json"
        info = self.thread_info(user_thread_id) or {
            "user_thread_id": user_thread_id,
            "created_at": utc_now(),
            "td_ids": [],
        }
        if td_id not in info["td_ids"]:
            info["td_ids"].append(td_id)
        info["active_td_id"] = td_id
        info.setdefault("root_td_id", td_id)
        info["updated_at"] = utc_now()
        atomic_write_json(path, info)

    def record_message(
        self,
        context: dict[str, Any],
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "message_id": short_id("msg"),
            "role": role,
            "content": content,
            "user_thread_id": context["user_thread_id"],
            "td_id": context["td_id"],
            "session_id": context["session_id"],
            "timestamp": utc_now(),
            "metadata": metadata or {},
        }
        append_jsonl(self.thread_dir(context["user_thread_id"]) / "messages.jsonl", record)
        return record

    def message_history(
        self,
        user_thread_id: str,
        *,
        td_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        records = read_jsonl(self.thread_dir(user_thread_id) / "messages.jsonl")
        if td_id:
            records = [item for item in records if item["td_id"] == td_id]
        return records[-limit:] if limit else records

    def load(self, user_thread_id: str, td_id: str) -> dict[str, Any]:
        path = self.td_dir(user_thread_id, td_id) / "state.json"
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, context: dict[str, Any]) -> None:
        directory = self.td_dir(context["user_thread_id"], context["td_id"])
        atomic_write_json(directory / "state.json", context)

    def commit_transition(
        self,
        context: dict[str, Any],
        event: str,
        from_state: str,
        to_state: str,
        data: dict[str, Any] | None = None,
    ) -> str:
        event_id = short_id("ev")
        timestamp = utc_now()
        context["state"] = to_state
        context["revision"] += 1
        context["updated_at"] = timestamp
        self.save(context)
        directory = self.td_dir(context["user_thread_id"], context["td_id"])
        append_jsonl(directory / "operation.jsonl", {
            "event_id": event_id,
            "status": "accepted",
            "event": event,
            "from": from_state,
            "to": to_state,
            "data": data or {},
            "session_id": context["session_id"],
            "timestamp": timestamp,
        })
        append_jsonl(directory / "event.jsonl", {
            "event_id": event_id,
            "event": event,
            "from": from_state,
            "to": to_state,
            "timestamp": timestamp,
        })
        return event_id

    def record_rejection(
        self,
        context: dict[str, Any],
        operation: str,
        errors: list[str],
    ) -> str:
        event_id = short_id("op")
        directory = self.td_dir(context["user_thread_id"], context["td_id"])
        append_jsonl(directory / "operation.jsonl", {
            "event_id": event_id,
            "status": "rejected",
            "operation": operation,
            "state": context["state"],
            "errors": errors,
            "session_id": context["session_id"],
            "timestamp": utc_now(),
        })
        return event_id

    def event_log(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        return read_jsonl(self.td_dir(context["user_thread_id"], context["td_id"]) / "event.jsonl")

    def operation_log(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        return read_jsonl(self.td_dir(context["user_thread_id"], context["td_id"]) / "operation.jsonl")

    def start_new_session(self, context: dict[str, Any]) -> str:
        session_id = short_id("ss")
        context["session_id"] = session_id
        context["updated_at"] = utc_now()
        self.save(context)
        self._register_session(context)
        return session_id

    def _register_session(self, context: dict[str, Any]) -> None:
        thread_id = context["user_thread_id"]
        session_id = context["session_id"]
        started_at = utc_now()
        atomic_write_json(self.thread_dir(thread_id) / "sessions" / f"{session_id}.json", {
            "session_id": session_id,
            "user_thread_id": thread_id,
            "td_id": context["td_id"],
            "status": "active",
            "started_at": started_at,
            "ended_at": None,
        })
        path = self.thread_dir(thread_id) / "thread.json"
        info = self.thread_info(thread_id)
        if info is None:
            raise FileNotFoundError(path)
        session_ids = info.setdefault("session_ids", [])
        if session_id not in session_ids:
            session_ids.append(session_id)
        info["latest_session_id"] = session_id
        info["updated_at"] = started_at
        atomic_write_json(path, info)

    def end_session(self, context: dict[str, Any], status: str = "completed") -> None:
        thread_id = context["user_thread_id"]
        session_id = context["session_id"]
        path = self.thread_dir(thread_id) / "sessions" / f"{session_id}.json"
        if not path.exists():
            return
        session = json.loads(path.read_text(encoding="utf-8"))
        if session.get("status") != "active":
            return
        session["status"] = status
        session["ended_at"] = utc_now()
        atomic_write_json(path, session)

    def list_sessions(self, user_thread_id: str) -> list[dict[str, Any]]:
        directory = self.thread_dir(user_thread_id) / "sessions"
        result = []
        known_ids: set[str] = set()
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
                result.append(session)
                known_ids.add(session["session_id"])
            except (json.JSONDecodeError, OSError):
                continue
        # Read legacy POC sessions in place so existing threads remain inspectable.
        for path in sorted((self.thread_dir(user_thread_id) / "td").glob("*/sessions/*.json")):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if session.get("session_id") in known_ids:
                continue
            session.update({
                "user_thread_id": user_thread_id,
                "td_id": path.parent.parent.name,
                "status": "legacy" if session.get("status") == "started" else session.get("status", "legacy"),
                "legacy_path": str(path),
            })
            result.append(session)
        return sorted(result, key=lambda item: item.get("started_at", ""))
