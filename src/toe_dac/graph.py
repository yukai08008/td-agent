from state_machine import Graph

from .states import ACTIVE_STATES, TDState


def build_td_graph() -> Graph:
    transitions = {
        (TDState.IDLE, TDState.TARGETING): {"event": "start"},
        (TDState.TARGETING, TDState.OBSERVING): {"event": "target_accepted"},
        (TDState.TARGETING, TDState.WAITING_HUMAN): {"event": "target_needs_input"},
        (TDState.TARGETING, TDState.RECOVERING): {"event": "target_failed"},
        (TDState.OBSERVING, TDState.ESTIMATING): {"event": "observation_accepted"},
        (TDState.ESTIMATING, TDState.DECIDING): {"event": "estimate_accepted"},
        (TDState.ESTIMATING, TDState.OBSERVING): {"event": "estimate_requests_observation"},
        (TDState.DECIDING, TDState.ACTING): {"event": "plan_accepted"},
        (TDState.ACTING, TDState.CHECKING_ACTION): {"event": "action_submitted"},
        (TDState.ACTING, TDState.WAITING_HUMAN): {"event": "act_needs_input"},
        (TDState.CHECKING_ACTION, TDState.ACTING): {"event": "advance_action"},
        (TDState.CHECKING_ACTION, TDState.CHECKING_TARGET): {"event": "actions_completed"},
        (TDState.CHECKING_ACTION, TDState.RECOVERING): {"event": "action_failed"},
        (TDState.CHECKING_ACTION, TDState.WAITING_HUMAN): {"event": "action_check_needs_input"},
        (TDState.CHECKING_TARGET, TDState.SUCCEEDED): {"event": "target_passed"},
        (TDState.CHECKING_TARGET, TDState.RECOVERING): {"event": "target_failed"},
        (TDState.CHECKING_TARGET, TDState.WAITING_HUMAN): {"event": "target_check_needs_input"},
        (TDState.RECOVERING, TDState.TARGETING): {"event": "retry_targeting"},
        (TDState.RECOVERING, TDState.ACTING): {"event": "retry_action"},
        (TDState.RECOVERING, TDState.DECIDING): {"event": "replan"},
        (TDState.RECOVERING, TDState.OBSERVING): {"event": "reobserve"},
        (TDState.RECOVERING, TDState.WAITING_HUMAN): {"event": "escalate"},
        (TDState.RECOVERING, TDState.FAILED): {"event": "give_up"},
        (TDState.WAITING_HUMAN, TDState.TARGETING): {"event": "target_input_received"},
        (TDState.WAITING_HUMAN, TDState.RECOVERING): {"event": "recovery_input_received"},
        (TDState.OBSERVING, TDState.WAITING_HUMAN): {"event": "observe_needs_input"},
        (TDState.ESTIMATING, TDState.WAITING_HUMAN): {"event": "estimate_needs_input"},
        (TDState.DECIDING, TDState.WAITING_HUMAN): {"event": "decide_needs_input"},
        (TDState.WAITING_HUMAN, TDState.OBSERVING): {"event": "observation_input_received"},
        (TDState.WAITING_HUMAN, TDState.ESTIMATING): {"event": "estimate_input_received"},
        (TDState.WAITING_HUMAN, TDState.DECIDING): {"event": "decision_input_received"},
        (TDState.WAITING_HUMAN, TDState.ACTING): {"event": "act_input_received"},
        (TDState.WAITING_HUMAN, TDState.CHECKING_ACTION): {"event": "action_check_input_received"},
        (TDState.WAITING_HUMAN, TDState.CHECKING_TARGET): {"event": "target_check_input_received"},
    }

    for state in ACTIVE_STATES:
        transitions[(state, TDState.PAUSED)] = {"event": f"pause_from_{state.value}"}
        transitions[(state, TDState.CANCELLED)] = {"event": "cancel"}
        transitions[(state, TDState.FAILED)] = {"event": "runtime_budget_exhausted"}
        transitions[(TDState.PAUSED, state)] = {"event": f"resume_to_{state.value}"}

    for state in {
        TDState.DECIDING, TDState.ACTING,
        TDState.CHECKING_ACTION, TDState.CHECKING_TARGET,
    }:
        transitions[(state, TDState.OBSERVING)] = {"event": "user_reobserve_requested"}

    for state in {
        TDState.DECIDING, TDState.ACTING, TDState.CHECKING_ACTION,
        TDState.CHECKING_TARGET,
    }:
        transitions[(state, TDState.DECIDING)] = {"event": "user_replan_requested"}

    transitions[(TDState.IDLE, TDState.CANCELLED)] = {"event": "cancel"}
    transitions[(TDState.PAUSED, TDState.CANCELLED)] = {"event": "cancel"}
    return Graph(transitions=transitions, initial=TDState.IDLE)
