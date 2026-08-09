from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_context(user_thread_id: str, td_id: str, session_id: str, retry_budget: int = 3) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": "0.1",
        "user_thread_id": user_thread_id,
        "td_id": td_id,
        "parent_td_id": None,
        "session_id": session_id,
        "state": "idle",
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "target": {},
        "target_revisions": [],
        "observation": {},
        "estimate": {},
        "plan": {},
        "plan_history": [],
        "execution": {
            "current_action_id": None,
            "attempts": [],
            "completed_action_ids": [],
        },
        "checks": {"action_checks": [], "target_check": None},
        "recovery": {
            "retry_count": 0,
            "retry_budget": retry_budget,
            "last_failure": None,
            "decision": None,
            "active_experience_id": None,
        },
        "control": {
            "paused_from": None,
            "waiting_reason": None,
            "return_to": None,
            "human_question": None,
            "human_response": None,
        },
        "artifacts": [],
    }
