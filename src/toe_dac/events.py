from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ConversationEventType = Literal[
    "assistant_message",
    "phase_started",
    "phase_completed",
    "progress",
    "human_question",
    "executor_boundary",
    "background_job_running",
    "recovery_required",
    "paused",
    "terminal",
]


@dataclass(frozen=True)
class ConversationEvent:
    """UI-neutral output emitted by the conversational TD controller."""

    type: ConversationEventType
    message: str
    phase: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    visible: bool = True
