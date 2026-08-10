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
        visibility: str = "thread",
        signature: dict[str, Any] | None = None,
        source_refs: dict[str, Any] | None = None,
    ) -> str:
        if visibility not in {"thread", "system"}:
            raise ValueError(f"unsupported experience visibility: {visibility}")
        experience_id = short_id("exp")
        self._append("exception_observed", experience_id, scope_id, {
            "user_thread_id": user_thread_id,
            "td_id": td_id,
            "session_id": session_id,
            "exception": exception,
            "visibility": visibility,
            "signature": signature or exception,
            "source_refs": source_refs or {},
        })
        return experience_id

    def match(
        self,
        scope_id: str,
        signature: dict[str, Any],
        limit: int = 5,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        index = self._load_index()
        candidates = []
        excluded = exclude_ids or set()
        for experience_id, item in index["experiences"].items():
            if experience_id in excluded:
                continue
            visibility = item.get("visibility", "thread")
            if visibility != "system" and item.get("scope_id") != scope_id:
                continue
            score = _similarity(signature, item.get("signature") or item.get("exception", {}))
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
                "exception": (
                    {} if visibility == "system" else item.get("exception", {})
                ),
                "signature": item.get("signature", {}),
                "visibility": visibility,
                "resolutions": _candidate_resolutions(item, visibility),
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

    def treatment_started(
        self,
        experience_id: str,
        scope_id: str,
        strategy: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        treatment_id = short_id("trt")
        self._append("treatment_started", experience_id, scope_id, {
            "treatment_id": treatment_id,
            "strategy": strategy,
            "details": details or {},
        })
        return treatment_id

    def treatment_finished(
        self,
        experience_id: str,
        scope_id: str,
        success: bool,
        details: dict[str, Any] | None = None,
        *,
        treatment_id: str | None = None,
    ) -> None:
        event_type = "treatment_succeeded" if success else "treatment_failed"
        self._append(event_type, experience_id, scope_id, {
            "treatment_id": treatment_id,
            "details": details or {},
        })

    def record_resolution(
        self,
        experience_id: str,
        scope_id: str,
        resolution: dict[str, Any],
    ) -> None:
        self._append("resolution_recorded", experience_id, scope_id, {
            "resolution": resolution,
        })

    def record_outcome(
        self,
        experience_id: str,
        scope_id: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._append("outcome_recorded", experience_id, scope_id, {
            "outcome": outcome,
            "details": details or {},
        })

    def classify(
        self,
        experience_id: str,
        scope_id: str,
        *,
        visibility: str,
        signature: dict[str, Any],
    ) -> None:
        if visibility not in {"thread", "system"}:
            raise ValueError(f"unsupported experience visibility: {visibility}")
        self._append("experience_classified", experience_id, scope_id, {
            "visibility": visibility,
            "signature": signature,
        })

    def stats(self, experience_id: str) -> dict[str, Any]:
        item = self._load_index()["experiences"].get(experience_id)
        if item is None:
            raise KeyError(experience_id)
        return _public_stats(item)

    def get(self, experience_id: str) -> dict[str, Any]:
        item = self._load_index()["experiences"].get(experience_id)
        if item is None:
            raise KeyError(experience_id)
        return item

    def index(self) -> dict[str, Any]:
        """Return the materialized index without modifying ledger or index timestamps."""
        return self._load_index()

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
            "visibility": "thread",
            "match_count": 0,
            "adopt_count": 0,
            "reject_count": 0,
            "use_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "effectiveness": 0.0,
            "treatments": [],
            "resolutions": [],
        })
        item.setdefault("visibility", "thread")
        item.setdefault("treatments", [])
        item.setdefault("resolutions", [])
        item.setdefault("reject_count", 0)
        event_type = event["event_type"]
        payload = event.get("payload", {})
        if event_type == "exception_observed":
            item["exception"] = payload.get("exception", {})
            item["visibility"] = payload.get("visibility", "thread")
            item["signature"] = payload.get("signature") or payload.get("exception", {})
            base_refs = {
                key: payload.get(key) for key in ("user_thread_id", "td_id", "session_id")
            }
            item["source_refs"] = _merge_refs(base_refs, payload.get("source_refs", {}))
        elif event_type == "experience_classified":
            item["visibility"] = payload.get("visibility", item["visibility"])
            item["signature"] = payload.get("signature") or item.get("signature", {})
        elif event_type == "experience_matched":
            item["match_count"] += 1
            item["last_matched_at"] = event["timestamp"]
        elif event_type == "experience_adopted":
            item["adopt_count"] += 1
        elif event_type == "experience_rejected":
            item["reject_count"] += 1
        elif event_type == "treatment_started":
            item["use_count"] += 1
            item["last_used_at"] = event["timestamp"]
            item["last_strategy"] = payload.get("strategy")
            item["treatments"].append({
                "treatment_id": payload.get("treatment_id"),
                "scope_id": event["scope_id"],
                "strategy": payload.get("strategy"),
                "details": payload.get("details", {}),
                "started_at": event["timestamp"],
                "status": "running",
            })
            item["source_refs"] = _merge_refs(
                item.get("source_refs", {}), payload.get("details", {}).get("source_refs", {}),
            )
        elif event_type == "treatment_succeeded":
            item["success_count"] += 1
            _finish_treatment(item, payload, event, "succeeded")
            item["source_refs"] = _merge_refs(
                item.get("source_refs", {}), payload.get("details", {}).get("source_refs", {}),
            )
        elif event_type == "treatment_failed":
            item["failure_count"] += 1
            _finish_treatment(item, payload, event, "failed")
            item["source_refs"] = _merge_refs(
                item.get("source_refs", {}), payload.get("details", {}).get("source_refs", {}),
            )
        elif event_type == "resolution_recorded":
            resolution = payload.get("resolution", {})
            item["resolutions"].append({
                **resolution,
                "recorded_at": event["timestamp"],
            })
            item["source_refs"] = _merge_refs(
                item.get("source_refs", {}), resolution.get("source_refs", {}),
            )
        elif event_type == "outcome_recorded":
            item["last_outcome"] = {
                "outcome": payload.get("outcome"),
                "details": payload.get("details", {}),
                "recorded_at": event["timestamp"],
            }
            item["source_refs"] = _merge_refs(
                item.get("source_refs", {}), payload.get("details", {}).get("source_refs", {}),
            )
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
    if left.get("error_code") and left.get("error_code") == right.get("error_code"):
        score += 0.5
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if left_tokens or right_tokens:
        score += 0.3 * (len(left_tokens & right_tokens) / len(left_tokens | right_tokens))
    return min(score, 1.0)


def _finish_treatment(
    item: dict[str, Any],
    payload: dict[str, Any],
    event: dict[str, Any],
    status: str,
) -> None:
    treatment_id = payload.get("treatment_id")
    treatment = next(
        (entry for entry in reversed(item["treatments"])
         if entry.get("status") == "running"
         and (not treatment_id or entry.get("treatment_id") == treatment_id)),
        None,
    )
    if treatment is None:
        treatment = {
            "treatment_id": treatment_id,
            "scope_id": event["scope_id"],
            "strategy": None,
            "started_at": None,
        }
        item["treatments"].append(treatment)
    treatment.update({
        "status": status,
        "finished_at": event["timestamp"],
        "result_details": payload.get("details", {}),
    })


def _merge_refs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if value in (None, "", [], {}):
            continue
        if key.endswith("_ids") or key.endswith("_refs"):
            current = merged.get(key, [])
            current = current if isinstance(current, list) else [current]
            incoming = value if isinstance(value, list) else [value]
            merged[key] = list(dict.fromkeys([*current, *incoming]))
        else:
            merged[key] = value
    return merged


def _candidate_resolutions(item: dict[str, Any], visibility: str) -> list[dict[str, Any]]:
    resolutions = item.get("resolutions", [])
    if visibility != "system":
        return resolutions
    safe_keys = {
        "resolution_key", "type", "summary", "instruction", "version",
        "commit", "regression_test", "recorded_at",
    }
    return [
        {key: value for key, value in resolution.items() if key in safe_keys}
        for resolution in resolutions
    ]


def _public_stats(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key, 0)
        for key in (
            "match_count", "adopt_count", "use_count",
            "success_count", "failure_count", "effectiveness",
        )
    }
