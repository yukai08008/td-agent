from __future__ import annotations

import pytest
from state_machine import TransitionError

from toe_dac.states import TDState


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
