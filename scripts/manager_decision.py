"""Choose one fail-closed action for the resident turn owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class ManagerAction(StrEnum):
    """One action the manager loop may execute."""

    STOP = "stop"
    WAIT = "wait"
    START = "start"
    RESUME_GOAL = "resume_goal"
    INTERRUPT = "interrupt"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class ManagerView:
    """The complete state needed to choose a manager action."""

    enabled: bool
    handoff_requested: bool
    managed_turn_id: str | None
    restart_request_turn_id: str | None
    goal_companion: bool = False


@dataclass(frozen=True, slots=True)
class ManagerDecision:
    """A manager action with its exact target or failure reason."""

    action: ManagerAction
    turn_id: str | None = None
    reason_code: str | None = None


def decide_manager_action(view: ManagerView, active_turn_id: str | None) -> ManagerDecision:
    """Require exact ownership before interruption and restart."""
    if not view.enabled:
        return ManagerDecision(ManagerAction.STOP)
    if view.restart_request_turn_id is not None:
        return _restart_decision(view, active_turn_id)
    if view.handoff_requested and view.managed_turn_id is None:
        return _handoff_decision(view.goal_companion, active_turn_id)
    return ManagerDecision(ManagerAction.WAIT)


def _restart_decision(view: ManagerView, active_turn_id: str | None) -> ManagerDecision:
    requested = view.restart_request_turn_id
    if requested != view.managed_turn_id:
        return ManagerDecision(
            ManagerAction.FAIL_CLOSED,
            reason_code="restart_turn_not_owned",
        )
    if active_turn_id is None:
        return ManagerDecision(ManagerAction.WAIT)
    if active_turn_id != requested:
        return ManagerDecision(
            ManagerAction.FAIL_CLOSED,
            reason_code="active_turn_mismatch",
        )
    return ManagerDecision(ManagerAction.INTERRUPT, turn_id=requested)


def _handoff_decision(
    goal_companion: bool,
    active_turn_id: str | None,
) -> ManagerDecision:
    if goal_companion:
        if active_turn_id is None:
            return ManagerDecision(ManagerAction.RESUME_GOAL)
        return ManagerDecision(
            ManagerAction.FAIL_CLOSED,
            reason_code="unexpected_active_turn",
        )
    if active_turn_id is not None:
        return ManagerDecision(
            ManagerAction.FAIL_CLOSED,
            reason_code="unexpected_active_turn",
        )
    return ManagerDecision(ManagerAction.START)
