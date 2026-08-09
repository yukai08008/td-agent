from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

from .context import SHANGHAI_TZ, new_context, utc_now


ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def session_id() -> str:
    timestamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S")
    return f"sess-{uuid.uuid4().hex[:8]}-{timestamp}"


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


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text_if_changed(path: Path, content: str) -> bool:
    """Write only when bytes differ so read-only evidence views preserve file time."""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    atomic_write_text(path, content)
    return True


def atomic_write_json_if_changed(path: Path, data: dict[str, Any]) -> bool:
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    return atomic_write_text_if_changed(path, content)


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
    def __init__(
        self,
        root: str | Path = "data",
        access_log_dir: str | Path | None = None,
        credentials_root: str | Path | None = None,
    ):
        self.root = Path(root)
        self.access_log_dir = Path(access_log_dir) if access_log_dir else self.root / "logs"
        self.credentials_root = (
            Path(credentials_root) if credentials_root else self.root.parent / "credentials"
        )

    @property
    def threads_root(self) -> Path:
        return self.root / "user-threads"

    @property
    def legacy_threads_root(self) -> Path:
        return self.root / "threads"

    def is_legacy_thread(self, user_thread_id: str) -> bool:
        thread_id = _validate_id(user_thread_id)
        return not (self.threads_root / thread_id).exists() and (self.legacy_threads_root / thread_id).exists()

    def td_dir(self, user_thread_id: str, td_id: str) -> Path:
        return (
            self.thread_dir(user_thread_id)
            / "td"
            / _validate_id(td_id)
        )

    def thread_dir(self, user_thread_id: str) -> Path:
        thread_id = _validate_id(user_thread_id)
        current = self.threads_root / thread_id
        legacy = self.legacy_threads_root / thread_id
        if current.exists() or not legacy.exists():
            return current
        return legacy

    def thread_info(self, user_thread_id: str) -> dict[str, Any] | None:
        directory = self.thread_dir(user_thread_id)
        legacy_path = directory / "thread.json"
        if legacy_path.exists():
            return json.loads(legacy_path.read_text(encoding="utf-8"))
        meta_path = directory / "meta.json"
        state_path = directory / "state.json"
        if not meta_path.exists() or not state_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return {**meta, **state, "storage_version": 2}

    def active_td_id(self, user_thread_id: str) -> str | None:
        info = self.thread_info(user_thread_id)
        return info.get("active_td_id") if info else None

    def list_threads(self) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for directory in sorted(self.threads_root.glob("*")) if self.threads_root.exists() else []:
            try:
                info = self.thread_info(directory.name)
                if info:
                    result[info["user_thread_id"]] = info
            except (json.JSONDecodeError, OSError):
                continue
        for path in sorted(self.legacy_threads_root.glob("*/thread.json")) if self.legacy_threads_root.exists() else []:
            if path.parent.name in result:
                continue
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                result[info["user_thread_id"]] = info
            except (json.JSONDecodeError, OSError):
                continue
        return list(result.values())

    def create(self, user_thread_id: str | None = None, retry_budget: int = 3) -> dict[str, Any]:
        thread_id = user_thread_id or short_id("ut")
        td_id = short_id("td")
        current_session_id = session_id()
        context = new_context(thread_id, td_id, current_session_id, retry_budget)
        directory = self.td_dir(thread_id, td_id)
        if directory.exists():
            raise FileExistsError(directory)
        directory.mkdir(parents=True)
        atomic_write_json(directory / "state.json", context)
        self._register_td(thread_id, td_id)
        append_jsonl(self._event_log_path(context), {
            "event_id": short_id("ev"),
            "event": "td_created",
            "state": "idle",
            "session_id": current_session_id,
            "td_id": td_id,
            "request_id": None,
            "timestamp": utc_now(),
        })
        self._register_session(context)
        return context

    def _register_td(self, user_thread_id: str, td_id: str) -> None:
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
        self._save_thread_info(user_thread_id, info)

    def _save_thread_info(self, user_thread_id: str, info: dict[str, Any]) -> None:
        directory = self.thread_dir(user_thread_id)
        if self.is_legacy_thread(user_thread_id):
            atomic_write_json(directory / "thread.json", info)
            return
        meta_keys = {
            "user_thread_id", "title", "target_summary", "created_at", "updated_at",
            "host", "service_name", "runtime", "created_version", "last_review",
            "migration",
            "storage_version",
        }
        state_keys = {
            "user_thread_id", "active_td_id", "root_td_id", "td_ids", "latest_session_id",
            "session_ids", "revision", "updated_at",
            "storage_version",
        }
        meta = {key: value for key, value in info.items() if key in meta_keys}
        meta.setdefault("user_thread_id", user_thread_id)
        meta.setdefault("service_name", "td-agent")
        meta.setdefault("title", "")
        meta.setdefault("target_summary", "")
        meta.setdefault("host", {"hostname": platform.node()})
        meta.setdefault("runtime", {"platform": platform.platform(), "python": sys.version.split()[0]})
        try:
            current_version = package_version("toe-dac")
        except PackageNotFoundError:
            current_version = "development"
        meta.setdefault("created_version", current_version)
        meta.setdefault("last_review", None)
        meta["storage_version"] = 2
        state = {key: value for key, value in info.items() if key in state_keys}
        state.setdefault("user_thread_id", user_thread_id)
        state.setdefault("revision", 0)
        state["storage_version"] = 2
        atomic_write_json(directory / "meta.json", meta)
        atomic_write_json(directory / "state.json", state)
        credential_directory = self.credentials_root / "user-threads" / user_thread_id
        credential_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(credential_directory, 0o700)

    def update_thread_meta(self, context: dict[str, Any], updates: dict[str, Any]) -> None:
        if self.is_legacy_thread(context["user_thread_id"]):
            return
        info = self.thread_info(context["user_thread_id"])
        if info is None:
            raise FileNotFoundError(self.thread_dir(context["user_thread_id"]))
        changed = {
            key: {"old": info.get(key), "new": value}
            for key, value in updates.items()
            if info.get(key) != value
        }
        if not changed:
            return
        info.update(updates)
        info["updated_at"] = utc_now()
        info["revision"] = int(info.get("revision", 0)) + 1
        self._save_thread_info(context["user_thread_id"], info)
        append_jsonl(self._event_log_path(context), {
            "event_id": short_id("ev"),
            "event": "thread_meta_updated",
            "td_id": context["td_id"],
            "session_id": context["session_id"],
            "request_id": context.get("current_request_id"),
            "changes": changed,
            "timestamp": utc_now(),
        })

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
        if self.is_legacy_thread(context["user_thread_id"]):
            path = self.thread_dir(context["user_thread_id"]) / "messages.jsonl"
        else:
            path = self.session_dir(context["user_thread_id"], context["session_id"]) / "messages.jsonl"
        append_jsonl(path, record)
        return record

    def write_artifact(
        self,
        context: dict[str, Any],
        name: str,
        content: str,
    ) -> str:
        """Persist a UTF-8 text artifact inside the owning User Thread."""
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip(".-")
        if not safe_name:
            raise ValueError("artifact name is empty")
        root_name = "threads" if self.is_legacy_thread(context["user_thread_id"]) else "user-threads"
        relative = Path(root_name) / context["user_thread_id"] / "artifacts" / context["td_id"] / safe_name
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return str(relative)

    def message_history(
        self,
        user_thread_id: str,
        *,
        td_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        directory = self.thread_dir(user_thread_id)
        if self.is_legacy_thread(user_thread_id):
            records = read_jsonl(directory / "messages.jsonl")
        else:
            records = []
            sessions_root = directory / "trace" / "sessions"
            for path in sorted(sessions_root.glob("*/messages.jsonl")) if sessions_root.exists() else []:
                records.extend(read_jsonl(path))
            records.sort(key=lambda item: item.get("timestamp", ""))
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
        append_jsonl(self._operation_log_path(context), {
            "event_id": event_id,
            "status": "accepted",
            "event": event,
            "from": from_state,
            "to": to_state,
            "data": data or {},
            "session_id": context["session_id"],
            "td_id": context["td_id"],
            "request_id": context.get("current_request_id"),
            "timestamp": timestamp,
        })
        append_jsonl(self._event_log_path(context), {
            "event_id": event_id,
            "event": event,
            "from": from_state,
            "to": to_state,
            "session_id": context["session_id"],
            "td_id": context["td_id"],
            "request_id": context.get("current_request_id"),
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
        append_jsonl(self._operation_log_path(context), {
            "event_id": event_id,
            "status": "rejected",
            "operation": operation,
            "state": context["state"],
            "errors": errors,
            "session_id": context["session_id"],
            "td_id": context["td_id"],
            "request_id": context.get("current_request_id"),
            "timestamp": utc_now(),
        })
        return event_id

    def record_operation(
        self,
        context: dict[str, Any],
        operation: str,
        status: str,
        *,
        phase: str | None = None,
        error_type: str | None = None,
        error: str | None = None,
        data: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        """Record a non-transition operation and optionally persist its evidence."""
        operation_id = short_id("op")
        timestamp = utc_now()
        evidence_ref = None
        if evidence is not None:
            evidence_path = self.session_dir(
                context["user_thread_id"], context["session_id"], td_id=context["td_id"]
            ) / "evidence.jsonl"
            append_jsonl(evidence_path, {
                "operation_id": operation_id,
                "operation": operation,
                "phase": phase,
                "session_id": context["session_id"],
                "timestamp": timestamp,
                "evidence": evidence,
            })
            evidence_ref = str(evidence_path.relative_to(self.root))
        record = {
            "event_id": operation_id,
            "operation": operation,
            "status": status,
            "state": context["state"],
            "phase": phase,
            "session_id": context["session_id"],
            "td_id": context["td_id"],
            "request_id": context.get("current_request_id"),
            "timestamp": timestamp,
            "data": data or {},
        }
        if error_type:
            record["error_type"] = error_type
        if error:
            record["error"] = error
        if evidence_ref:
            record["evidence_ref"] = evidence_ref
        append_jsonl(self._operation_log_path(context), record)
        return operation_id

    def event_log(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        records = read_jsonl(self._event_log_path(context))
        return [item for item in records if not item.get("td_id") or item.get("td_id") == context["td_id"]]

    def operation_log(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        records = read_jsonl(self._operation_log_path(context))
        return [item for item in records if not item.get("td_id") or item.get("td_id") == context["td_id"]]

    def _event_log_path(self, context: dict[str, Any]) -> Path:
        if self.is_legacy_thread(context["user_thread_id"]):
            return self.td_dir(context["user_thread_id"], context["td_id"]) / "event.jsonl"
        return self.thread_dir(context["user_thread_id"]) / "logs" / "event.jsonl"

    def _operation_log_path(self, context: dict[str, Any]) -> Path:
        if self.is_legacy_thread(context["user_thread_id"]):
            return self.td_dir(context["user_thread_id"], context["td_id"]) / "operation.jsonl"
        return self.thread_dir(context["user_thread_id"]) / "logs" / "opr.jsonl"

    def session_dir(self, user_thread_id: str, selected_session_id: str, *, td_id: str | None = None) -> Path:
        thread_id = _validate_id(user_thread_id)
        selected = _validate_id(selected_session_id)
        if self.is_legacy_thread(thread_id):
            if td_id is None:
                info = self.find_session(selected) or {}
                td_id = info.get("td_id") or self.active_td_id(thread_id)
            if not td_id:
                raise ValueError(f"cannot resolve TD for Session {selected}")
            return self.td_dir(thread_id, str(td_id)) / "trace" / selected
        return self.thread_dir(thread_id) / "trace" / "sessions" / selected

    def session_evidence_dir(self, context: dict[str, Any]) -> Path:
        """Return the canonical evidence directory for the active persistent Session."""
        directory = self.session_dir(
            context["user_thread_id"], context["session_id"], td_id=context["td_id"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def artifact_dir(self, context: dict[str, Any]) -> Path:
        return (
            self.thread_dir(context["user_thread_id"])
            / "artifacts"
            / _validate_id(context["td_id"])
        )

    def _build_legacy_evidence_view_for_migration(self, context: dict[str, Any]) -> Path:
        """Legacy migration helper; interactive `/evidence` must never call this writer."""
        if not self.is_legacy_thread(context["user_thread_id"]):
            return self.session_evidence_dir(context)
        raw_directory = self.session_evidence_dir(context)
        directory = raw_directory / "view"
        directory.mkdir(parents=True, exist_ok=True)
        session_id = context["session_id"]
        thread_dir = self.thread_dir(context["user_thread_id"])
        td_dir = self.td_dir(context["user_thread_id"], context["td_id"])
        session_path = thread_dir / "sessions" / f"{session_id}.json"
        session = (
            json.loads(session_path.read_text(encoding="utf-8"))
            if session_path.exists()
            else {"session_id": session_id}
        )
        session["evidence_directory"] = str(directory)
        atomic_write_json_if_changed(directory / "session.json", session)

        operations = [
            item for item in read_jsonl(td_dir / "operation.jsonl")
            if item.get("session_id") == session_id
        ]
        messages = [
            item for item in read_jsonl(thread_dir / "messages.jsonl")
            if item.get("session_id") == session_id
        ]
        # Older events did not carry session_id, so retain them with the TD-wide
        # timeline instead of silently hiding potentially relevant evidence.
        events = read_jsonl(td_dir / "event.jsonl")
        atomic_write_text_if_changed(
            directory / "operation.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in operations),
        )
        atomic_write_text_if_changed(
            directory / "messages.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in messages),
        )
        atomic_write_text_if_changed(
            directory / "td-event.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in events),
        )
        evidence_records = []
        for path in sorted(raw_directory.glob("op_*.json")):
            try:
                evidence_records.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                evidence_records.append({
                    "status": "unreadable", "source_file": path.name,
                })
        atomic_write_text_if_changed(
            directory / "evidence.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in evidence_records),
        )
        artifact_records = []
        for reference in context.get("artifacts", []):
            path = Path(str(reference))
            resolved = path if path.is_absolute() else self.root / path
            artifact_records.append({
                "artifact_ref": str(reference),
                "exists": resolved.is_file(),
                "path": str(resolved),
            })
        atomic_write_text_if_changed(
            directory / "artifacts.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in artifact_records),
        )
        screenshots = directory / "screenshots"
        screenshots.mkdir(exist_ok=True)
        screenshot_files = [path for path in screenshots.iterdir() if path.is_file()]
        screenshot_records = [
            {"status": "captured", "file": path.name, "path": str(path)}
            for path in sorted(screenshot_files)
        ]
        if not screenshot_records:
            screenshot_records.append({
                "status": "not_applicable",
                "reason": (
                    "No visual executor registered a screenshot for this Session. "
                    "Model and HTTP/API calls have no truthful screen image to capture."
                ),
            })
        atomic_write_text_if_changed(
            directory / "screenshots.jsonl",
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in screenshot_records),
        )
        atomic_write_text_if_changed(directory / "README.md", (
            f"# Session evidence: {session_id}\n\n"
            "This is the human-facing, aggregated evidence view. Raw operation files remain one level up.\n\n"
            "- `session.json`: Session metadata\n"
            "- `messages.jsonl`: messages from this Session\n"
            "- `operation.jsonl`: operations from this Session\n"
            "- `td-event.jsonl`: TD-wide event timeline (includes legacy events without Session IDs)\n"
            "- `evidence.jsonl`: aggregated model/tool/validation evidence\n"
            "- `artifacts.jsonl`: final Artifact references and existence checks\n"
            "- `screenshots.jsonl`: screenshot index or an explicit not-applicable reason\n"
            "- `screenshots/`: screenshots from visual executors\n\n"
            "Run `/evidence` again to refresh the log snapshots.\n"
        ))
        return directory

    def start_new_session(self, context: dict[str, Any]) -> str:
        selected_session_id = session_id()
        context["session_id"] = selected_session_id
        context["updated_at"] = utc_now()
        self.save(context)
        self._register_session(context)
        return selected_session_id

    def attach_session(
        self,
        context: dict[str, Any],
        session_id: str | None = None,
        *,
        read_only: bool = False,
    ) -> str | None:
        """Attach a CLI connection to a persistent Session without creating a new one."""
        thread_id = context["user_thread_id"]
        selected = _validate_id(session_id or context["session_id"])
        path = self._session_record_path(thread_id, selected, td_id=context["td_id"])
        if not path.exists():
            raise FileNotFoundError(path)
        session = json.loads(path.read_text(encoding="utf-8"))
        if session.get("user_thread_id") != thread_id:
            raise ValueError(f"Session {selected} does not belong to User Thread {thread_id}")
        if session.get("td_id") != context["td_id"]:
            raise ValueError(f"Session {selected} does not belong to TD {context['td_id']}")
        if read_only:
            # Select only in this in-memory view. Browsing must not reopen or
            # otherwise mutate a completed persistent Session.
            context["session_id"] = selected
            return None
        terminal_states = {"succeeded", "failed", "cancelled"}
        if context.get("state") in terminal_states:
            raise ValueError(f"TD has ended with state {context.get('state')}; Session {selected} is read-only")
        if session.get("status") in terminal_states:
            raise ValueError(f"Session {selected} has ended with status {session.get('status')}")
        attached_at = utc_now()
        connection_id = short_id("conn")
        # Before persistent Session semantics, CLI exit incorrectly wrote `completed`.
        # A non-terminal TD proves that record is safe to reattach as a legacy Session.
        if session.get("status") == "completed":
            session["ended_at"] = None
            session["legacy_reopened"] = True
        session.update({
            "status": "active",
            "current_connection_id": connection_id,
            "last_attached_at": attached_at,
            "attach_count": int(session.get("attach_count", 0)) + 1,
        })
        context["session_id"] = selected
        context["updated_at"] = attached_at
        self.save(context)
        atomic_write_json(path, session)
        info = self.thread_info(thread_id)
        if info is None:
            raise FileNotFoundError(self.thread_dir(thread_id) / "state.json")
        info["latest_session_id"] = selected
        info["updated_at"] = attached_at
        self._save_thread_info(thread_id, info)
        return connection_id

    def _register_session(self, context: dict[str, Any]) -> None:
        thread_id = context["user_thread_id"]
        session_id = context["session_id"]
        started_at = utc_now()
        path = self._session_record_path(thread_id, session_id, td_id=context["td_id"])
        atomic_write_json(path, {
            "session_id": session_id,
            "user_thread_id": thread_id,
            "td_id": context["td_id"],
            "status": "active",
            "started_at": started_at,
            "ended_at": None,
            "attach_count": 1,
            "last_attached_at": started_at,
            "last_detached_at": None,
            "current_connection_id": short_id("conn"),
        })
        info = self.thread_info(thread_id)
        if info is None:
            raise FileNotFoundError(self.thread_dir(thread_id))
        session_ids = info.setdefault("session_ids", [])
        if session_id not in session_ids:
            session_ids.append(session_id)
        info["latest_session_id"] = session_id
        info["updated_at"] = started_at
        self._save_thread_info(thread_id, info)

    def detach_session(self, context: dict[str, Any]) -> None:
        thread_id = context["user_thread_id"]
        session_id = context["session_id"]
        path = self._session_record_path(thread_id, session_id, td_id=context["td_id"])
        if not path.exists():
            return
        session = json.loads(path.read_text(encoding="utf-8"))
        if context.get("state") in {"succeeded", "failed", "cancelled"}:
            self.end_session(context, context["state"])
            return
        session["status"] = "detached"
        session["last_detached_at"] = utc_now()
        session["current_connection_id"] = None
        session["ended_at"] = None
        atomic_write_json(path, session)

    def end_session(self, context: dict[str, Any], status: str = "completed") -> None:
        thread_id = context["user_thread_id"]
        session_id = context["session_id"]
        path = self._session_record_path(thread_id, session_id, td_id=context["td_id"])
        if not path.exists():
            return
        session = json.loads(path.read_text(encoding="utf-8"))
        session["status"] = status
        session["ended_at"] = utc_now()
        session["current_connection_id"] = None
        atomic_write_json(path, session)

    def find_session(self, session_id: str) -> dict[str, Any] | None:
        selected = _validate_id(session_id)
        candidates = []
        if self.threads_root.exists():
            candidates.extend(self.threads_root.glob(f"*/trace/sessions/{selected}/session.json"))
        if self.legacy_threads_root.exists():
            candidates.extend(self.legacy_threads_root.glob(f"*/sessions/{selected}.json"))
            candidates.extend(self.legacy_threads_root.glob(f"*/td/*/sessions/{selected}.json"))
        for path in candidates:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def list_sessions(self, user_thread_id: str) -> list[dict[str, Any]]:
        thread_directory = self.thread_dir(user_thread_id)
        directory = (
            thread_directory / "sessions"
            if self.is_legacy_thread(user_thread_id)
            else thread_directory / "trace" / "sessions"
        )
        result = []
        known_ids: set[str] = set()
        pattern = "*.json" if self.is_legacy_thread(user_thread_id) else "*/session.json"
        for path in sorted(directory.glob(pattern)) if directory.exists() else []:
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
                result.append(session)
                known_ids.add(session["session_id"])
            except (json.JSONDecodeError, OSError):
                continue
        # Read legacy POC sessions in place so existing threads remain inspectable.
        for path in sorted(thread_directory.glob("td/*/sessions/*.json")) if self.is_legacy_thread(user_thread_id) else []:
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

    def _session_record_path(
        self, user_thread_id: str, selected_session_id: str, *, td_id: str | None = None,
    ) -> Path:
        if self.is_legacy_thread(user_thread_id):
            return self.thread_dir(user_thread_id) / "sessions" / f"{_validate_id(selected_session_id)}.json"
        return self.session_dir(user_thread_id, selected_session_id, td_id=td_id) / "session.json"

    def thread_credentials_dir(self, user_thread_id: str, *, create: bool = False) -> Path:
        directory = self.credentials_root / "user-threads" / _validate_id(user_thread_id)
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        return directory

    def resolve_thread_credential(self, user_thread_id: str, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("credential name must be a single safe file name")
        directory = self.thread_credentials_dir(user_thread_id).resolve()
        path = directory / name
        if path.is_symlink():
            raise ValueError("credential symlinks are not allowed")
        resolved = path.resolve(strict=True)
        if resolved.parent != directory:
            raise ValueError("credential path escapes the User Thread boundary")
        return resolved
