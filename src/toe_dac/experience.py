from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .context import utc_now
from .storage import append_jsonl, atomic_write_json, read_jsonl, short_id


TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


class ExperienceStore:
    def __init__(self, root: str | Path):
        self.directory = Path(root) / "experience"
        self.ledger_path = self.directory / "ledger.jsonl"
        self.index_path = self.directory / "index.json"

    def observe_exception(
        self,
        *,
        scope_id: str,
        user_thread_id: str,
        td_id: str,
        session_id: str,
        exception: dict[str, Any],
    ) -> str:
        experience_id = short_id("exp")
        self._append("exception_observed", experience_id, scope_id, {
            "user_thread_id": user_thread_id,
            "td_id": td_id,
            "session_id": session_id,
            "exception": exception,
        })
        return experience_id

    def match(self, scope_id: str, signature: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        index = self._load_index()
        candidates = []
        for experience_id, item in index["experiences"].items():
            if item.get("scope_id") != scope_id:
                continue
            score = _similarity(signature, item.get("exception", {}))
            if score <= 0:
                continue
            effectiveness = item.get("effectiveness", 0.0)
            sample_size = item.get("success_count", 0) + item.get("failure_count", 0)
            rank = score + (effectiveness * min(sample_size, 5) * 0.03)
            candidates.append({
                "experience_id": experience_id,
                "similarity": round(score, 4),
                "rank": round(rank, 4),
                "stats": _public_stats(item),
                "exception": item.get("exception", {}),
            })
        candidates.sort(key=lambda item: item["rank"], reverse=True)
        selected = candidates[:limit]
        for candidate in selected:
            self._append("experience_matched", candidate["experience_id"], scope_id, {
                "signature": signature,
                "similarity": candidate["similarity"],
            })
        return selected

    def adopt(self, experience_id: str, scope_id: str, reason: str, confidence: float) -> None:
        self._append("experience_adopted", experience_id, scope_id, {
            "reason": reason,
            "confidence": confidence,
        })

    def reject(self, experience_id: str, scope_id: str, reason: str) -> None:
        self._append("experience_rejected", experience_id, scope_id, {"reason": reason})

    def treatment_started(self, experience_id: str, scope_id: str, strategy: str) -> None:
        self._append("treatment_started", experience_id, scope_id, {"strategy": strategy})

    def treatment_finished(
        self,
        experience_id: str,
        scope_id: str,
        success: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        event_type = "treatment_succeeded" if success else "treatment_failed"
        self._append(event_type, experience_id, scope_id, {"details": details or {}})

    def stats(self, experience_id: str) -> dict[str, Any]:
        item = self._load_index()["experiences"].get(experience_id)
        if item is None:
            raise KeyError(experience_id)
        return _public_stats(item)

    def rebuild_index(self) -> dict[str, Any]:
        index = {"seen_event_ids": [], "experiences": {}}
        for event in read_jsonl(self.ledger_path):
            self._project(index, event)
        atomic_write_json(self.index_path, index)
        return index

    def _append(self, event_type: str, experience_id: str, scope_id: str, payload: dict[str, Any]) -> str:
        event = {
            "event_id": short_id("xee"),
            "event_type": event_type,
            "experience_id": experience_id,
            "scope_id": scope_id,
            "timestamp": utc_now(),
            "payload": payload,
        }
        append_jsonl(self.ledger_path, event)
        index = self._load_index()
        self._project(index, event)
        atomic_write_json(self.index_path, index)
        return event["event_id"]

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"seen_event_ids": [], "experiences": {}}
        with self.index_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _project(index: dict[str, Any], event: dict[str, Any]) -> None:
        if event["event_id"] in index["seen_event_ids"]:
            return
        index["seen_event_ids"].append(event["event_id"])
        experience_id = event["experience_id"]
        item = index["experiences"].setdefault(experience_id, {
            "scope_id": event["scope_id"],
            "match_count": 0,
            "adopt_count": 0,
            "use_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "effectiveness": 0.0,
        })
        event_type = event["event_type"]
        payload = event.get("payload", {})
        if event_type == "exception_observed":
            item["exception"] = payload.get("exception", {})
            item["source_refs"] = {
                key: payload.get(key) for key in ("user_thread_id", "td_id", "session_id")
            }
        elif event_type == "experience_matched":
            item["match_count"] += 1
            item["last_matched_at"] = event["timestamp"]
        elif event_type == "experience_adopted":
            item["adopt_count"] += 1
        elif event_type == "treatment_started":
            item["use_count"] += 1
            item["last_used_at"] = event["timestamp"]
            item["last_strategy"] = payload.get("strategy")
        elif event_type == "treatment_succeeded":
            item["success_count"] += 1
        elif event_type == "treatment_failed":
            item["failure_count"] += 1
        samples = item["success_count"] + item["failure_count"]
        item["effectiveness"] = item["success_count"] / samples if samples else 0.0


def _tokens(value: dict[str, Any]) -> set[str]:
    return set(TOKEN_PATTERN.findall(json.dumps(value, ensure_ascii=False).lower()))


def _similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    score = 0.0
    if left.get("phase") and left.get("phase") == right.get("phase"):
        score += 0.3
    if left.get("cause") and left.get("cause") == right.get("cause"):
        score += 0.4
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if left_tokens or right_tokens:
        score += 0.3 * (len(left_tokens & right_tokens) / len(left_tokens | right_tokens))
    return score


def _public_stats(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key, 0)
        for key in ("match_count", "adopt_count", "use_count", "success_count", "failure_count", "effectiveness")
    }
