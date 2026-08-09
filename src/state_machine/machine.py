from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Optional

from .graph import Graph


logger = logging.getLogger(__name__)


class TransitionError(Exception):
    def __init__(self, state: Enum, event: str, reason: str = ""):
        self.state = state
        self.event = event
        message = f"[{state.value}] cannot process event {event!r}"
        if reason:
            message += f": {reason}"
        super().__init__(message)


class Machine:
    """Event-driven machine backed by a declarative Graph."""

    def __init__(self, graph: Graph, context: Optional[dict[str, Any]] = None):
        self.graph = graph
        self.state = graph.initial
        self.context = context or {}
        self._log: list[dict[str, Any]] = []

    def send(self, event: str, data: Any = None) -> Enum:
        if data:
            if isinstance(data, dict):
                self.context.update(data)
            else:
                self.context.setdefault("_data", data)

        result = self.graph.find_transition(self.state, event)
        if result is None:
            raise TransitionError(
                self.state,
                event,
                f"available events: {self.graph.available_events(self.state)}",
            )
        _, to_state, transition = result
        if transition.guard and not transition.guard(self.context):
            raise TransitionError(self.state, event, "guard rejected transition")

        old_state = self.state
        if transition.on_exit:
            transition.on_exit(self.context)
        self.state = to_state
        self._log.append({
            "from": old_state.value,
            "to": to_state.value,
            "event": event,
            "ts": time.time(),
        })
        logger.info("[%s] --%s--> [%s]", old_state.value, event, to_state.value)
        if transition.on_enter:
            transition.on_enter(self.context)
        return to_state

    def can_send(self, event: str) -> bool:
        result = self.graph.find_transition(self.state, event)
        if result is None:
            return False
        _, _, transition = result
        return not transition.guard or transition.guard(self.context)

    @property
    def available_events(self) -> list[str]:
        return self.graph.available_events(self.state)

    @property
    def log(self) -> list[dict[str, Any]]:
        return list(self._log)

    def to_mermaid(self) -> str:
        return self.graph.to_mermaid()
