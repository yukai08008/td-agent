from __future__ import annotations

import pytest
from toe_dac.state_machine import Graph, Machine, TransitionError

from toe_dac.states import TDState


def test_parallel_edges_are_preserved_and_guards_select_a_target():
    graph = Graph([
        (TDState.IDLE, TDState.SUCCEEDED, {"event": "finish", "guard": lambda ctx: ctx.get("ok")}),
        (TDState.IDLE, TDState.FAILED, {"event": "finish", "guard": lambda ctx: not ctx.get("ok")}),
    ], initial=TDState.IDLE)

    assert Machine(graph, {"ok": True}).send("finish") == TDState.SUCCEEDED
    assert Machine(graph, {"ok": False}).send("finish") == TDState.FAILED


def test_guard_rejection_and_hook_failure_are_atomic():
    rejected_context = {"allowed": False}
    rejected = Machine(Graph({
        (TDState.IDLE, TDState.TARGETING): {"event": "start", "guard": lambda ctx: ctx["allowed"]},
    }, initial=TDState.IDLE), rejected_context)
    with pytest.raises(TransitionError):
        rejected.send("start", {"temporary": True})
    assert rejected.state == TDState.IDLE
    assert rejected_context == {"allowed": False}
    assert rejected.log == []

    def broken_enter(context):
        context["changed"] = True
        raise RuntimeError("hook failed")

    hook_context = {}
    hooked = Machine(Graph({
        (TDState.IDLE, TDState.TARGETING): {"event": "start", "on_enter": broken_enter},
    }, initial=TDState.IDLE), hook_context)
    with pytest.raises(RuntimeError, match="hook failed"):
        hooked.send("start")
    assert hooked.state == TDState.IDLE
    assert hook_context == {}
    assert hooked.log == []


def test_initial_state_and_start(service):
    assert service.state == TDState.IDLE
    assert "start" in service.available_events
    service.start()
    assert service.state == TDState.TARGETING
    assert service.context["revision"] == 1


def test_illegal_event_does_not_change_state(service):
    with pytest.raises(TransitionError):
        service.machine.send("plan_accepted")
    assert service.state == TDState.IDLE


def test_pause_and_resume_restore_exact_state(service):
    service.start()
    service.pause()
    assert service.state == TDState.PAUSED
    assert service.context["control"]["paused_from"] == "targeting"
    service.resume()
    assert service.state == TDState.TARGETING
    assert service.context["control"]["paused_from"] is None


def test_cancel_is_terminal(service):
    service.start()
    service.cancel()
    assert service.state == TDState.CANCELLED
    with pytest.raises(TransitionError):
        service.cancel()


def test_graph_keeps_parallel_recovery_failure_edges_and_renders_guards(service):
    edges = [
        edge.event
        for source, target, edge in service.machine.graph.all_transitions()
        if source == TDState.RECOVERING and target == TDState.FAILED
    ]
    assert set(edges) == {"give_up", "runtime_budget_exhausted"}
    mermaid = service.machine.to_mermaid()
    assert "[*] --> idle" in mermaid
    assert "target_accepted [target_ready]" in mermaid


def test_recovery_give_up_edge_reaches_failed(service):
    service.start()
    service.fail_targeting("not_recoverable", "stop")

    state = service.recover("give_up", reason="no viable path")

    assert state == TDState.FAILED
