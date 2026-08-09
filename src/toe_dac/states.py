from enum import Enum


class TDState(Enum):
    IDLE = "idle"
    TARGETING = "targeting"
    OBSERVING = "observing"
    ESTIMATING = "estimating"
    DECIDING = "deciding"
    ACTING = "acting"
    CHECKING_ACTION = "checking_action"
    CHECKING_TARGET = "checking_target"
    RECOVERING = "recovering"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {TDState.SUCCEEDED, TDState.FAILED, TDState.CANCELLED}
ACTIVE_STATES = set(TDState) - TERMINAL_STATES - {TDState.IDLE, TDState.PAUSED}
