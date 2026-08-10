from __future__ import annotations

import copy
import logging
import time
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Callable, Optional, TypeVar


logger = logging.getLogger(__name__)
S = TypeVar("S", bound=Enum)
GuardFn = Callable[..., bool]
ActionFn = Callable[..., None]


class TransitionDef:
    def __init__(
        self,
        event: str,
        guard: Optional[GuardFn] = None,
        on_exit: Optional[ActionFn] = None,
        on_enter: Optional[ActionFn] = None,
    ):
        self.event = event
        self.guard = guard
        self.on_exit = on_exit
        self.on_enter = on_enter


class Graph:
    """Declarative directed multigraph indexed by event name."""

    def __init__(
        self,
        transitions: Mapping[tuple[S, S], dict[str, Any]]
        | Iterable[tuple[S, S, dict[str, Any]]],
        initial: S,
    ):
        self.initial = initial
        self._transitions: list[tuple[S, S, TransitionDef]] = []
        self._event_index: dict[str, list[tuple[S, S, TransitionDef]]] = {}
        entries = (
            ((source, target, config) for (source, target), config in transitions.items())
            if isinstance(transitions, Mapping)
            else iter(transitions)
        )
        for from_state, to_state, config in entries:
            transition = TransitionDef(
                event=config.get("event", ""),
                guard=config.get("guard"),
                on_exit=config.get("on_exit"),
                on_enter=config.get("on_enter"),
            )
            edge = (from_state, to_state, transition)
            self._transitions.append(edge)
            self._event_index.setdefault(transition.event, []).append(edge)

    def find_transition(self, current: S, event: str) -> Optional[tuple[S, S, TransitionDef]]:
        values = self.find_transitions(current, event)
        return values[0] if values else None

    def find_transitions(self, current: S, event: str) -> list[tuple[S, S, TransitionDef]]:
        return [
            (source, target, transition)
            for source, target, transition in self._event_index.get(event, [])
            if source == current
        ]

    def available_events(self, current: S) -> list[str]:
        return sorted({
            transition.event
            for source, _, transition in self._transitions
            if source == current
        })

    def transitions_from(self, state: S) -> list[tuple[S, TransitionDef]]:
        return [
            (target, transition)
            for source, target, transition in self._transitions
            if source == state
        ]

    def all_transitions(self) -> list[tuple[S, S, TransitionDef]]:
        return list(self._transitions)

    def to_mermaid(self) -> str:
        lines = ["stateDiagram-v2", f"    [*] --> {self.initial.value}"]
        for source, target, transition in self._transitions:
            guard_name = transition.guard.__name__.lstrip("_") if transition.guard else ""
            guard = f" [{guard_name}]" if guard_name else ""
            lines.append(f"    {source.value} --> {target.value} : {transition.event}{guard}")
        return "\n".join(lines)


class TransitionError(Exception):
    def __init__(self, state: Enum, event: str, reason: str = ""):
        self.state = state
        self.event = event
        message = f"[{state.value}] cannot process event {event!r}"
        if reason:
            message += f": {reason}"
        super().__init__(message)


class Machine:
    """Atomic event-driven machine backed by a declarative Graph."""

    def __init__(self, graph: Graph, context: Optional[dict[str, Any]] = None):
        self.graph = graph
        self.state = graph.initial
        self.context = context if context is not None else {}
        self._log: list[dict[str, Any]] = []

    def send(self, event: str, data: Any = None) -> Enum:
        context_before = copy.deepcopy(self.context)
        state_before = self.state
        log_length = len(self._log)
        if data is not None:
            if isinstance(data, dict):
                self.context.update(data)
            else:
                self.context["_data"] = data

        candidates = self.graph.find_transitions(self.state, event)
        if not candidates:
            self._restore(context_before, state_before, log_length)
            raise TransitionError(
                self.state, event,
                f"available events: {self.graph.available_events(self.state)}",
            )

        selected = None
        guard_errors = []
        for _, target, transition in candidates:
            try:
                if transition.guard and not transition.guard(self.context):
                    continue
            except Exception as exc:
                logger.warning("guard failed closed for event=%s: %s", event, exc)
                guard_errors.append(type(exc).__name__)
                continue
            selected = (target, transition)
            break
        if selected is None:
            self._restore(context_before, state_before, log_length)
            suffix = f"; guard errors: {guard_errors}" if guard_errors else ""
            raise TransitionError(self.state, event, f"all guards rejected transition{suffix}")

        target, transition = selected
        try:
            old_state = self.state
            if transition.on_exit:
                transition.on_exit(self.context)
            self.state = target
            self._log.append({
                "from": old_state.value,
                "to": target.value,
                "event": event,
                "data": copy.deepcopy(data),
                "ts": time.time(),
            })
            if transition.on_enter:
                transition.on_enter(self.context)
            logger.info("[%s] --%s--> [%s]", old_state.value, event, target.value)
            return target
        except Exception:
            self._restore(context_before, state_before, log_length)
            raise

    def can_send(self, event: str) -> bool:
        for _, _, transition in self.graph.find_transitions(self.state, event):
            try:
                if not transition.guard or transition.guard(self.context):
                    return True
            except Exception as exc:
                logger.warning("guard failed closed for event=%s: %s", event, exc)
        return False

    @property
    def available_events(self) -> list[str]:
        return [event for event in self.graph.available_events(self.state) if self.can_send(event)]

    @property
    def log(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._log)

    def to_mermaid(self) -> str:
        return self.graph.to_mermaid()

    def _restore(self, context: dict[str, Any], state: Enum, log_length: int) -> None:
        self.context.clear()
        self.context.update(context)
        self.state = state
        del self._log[log_length:]
