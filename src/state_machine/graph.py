from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Optional, TypeVar


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
    """Declarative state-transition graph indexed by event name."""

    def __init__(self, transitions: dict[tuple[S, S], dict[str, Any]], initial: S):
        self.initial = initial
        self._transitions: dict[tuple[S, S], TransitionDef] = {}
        self._event_index: dict[str, list[tuple[S, S]]] = {}
        for (from_state, to_state), config in transitions.items():
            transition = TransitionDef(
                event=config.get("event", ""),
                guard=config.get("guard"),
                on_exit=config.get("on_exit"),
                on_enter=config.get("on_enter"),
            )
            self._transitions[(from_state, to_state)] = transition
            self._event_index.setdefault(transition.event, []).append((from_state, to_state))

    def find_transition(self, current: S, event: str) -> Optional[tuple[S, S, TransitionDef]]:
        for from_state, to_state in self._event_index.get(event, []):
            if from_state == current:
                return from_state, to_state, self._transitions[(from_state, to_state)]
        return None

    def available_events(self, current: S) -> list[str]:
        return sorted({
            transition.event
            for (from_state, _), transition in self._transitions.items()
            if from_state == current
        })

    def transitions_from(self, state: S) -> list[tuple[S, TransitionDef]]:
        return [
            (to_state, transition)
            for (from_state, to_state), transition in self._transitions.items()
            if from_state == state
        ]

    def all_transitions(self) -> list[tuple[S, S, TransitionDef]]:
        return [(source, target, transition) for (source, target), transition in self._transitions.items()]

    def to_mermaid(self) -> str:
        lines = ["stateDiagram-v2"]
        for (source, target), transition in self._transitions.items():
            guard = " [guard]" if transition.guard else ""
            lines.append(f"    {source.value} --> {target.value} : {transition.event}{guard}")
        return "\n".join(lines)
