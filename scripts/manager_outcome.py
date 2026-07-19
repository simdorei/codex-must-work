"""Resolve terminal app-server outcomes without collapsing their meaning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, assert_never

from scripts.app_server_protocol import TurnOutcome
from scripts.goal_control import GoalControlError, GoalStatus
from scripts.manager_restart_guard import InterruptClaimState, classify_interrupt_claim
from scripts.manager_runtime import ManagerRuntime, record_restart_performed, record_turn_finished
from scripts.setup import complete_session, disable_session

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.manager_goal import GoalGuard


@dataclass(frozen=True, slots=True)
class TurnResolution:
    """Return the next manager-loop state after one terminal outcome."""

    keep_running: bool
    restart_prompt_pending: bool = False
    failure_reason: str | None = None


def resolve_turn_outcome(
    root: Path,
    runtime: ManagerRuntime,
    goal_guard: GoalGuard | None,
    turn_id: str,
    outcome: TurnOutcome,
) -> TurnResolution:
    """Apply the exact completion, interruption, or failure contract."""
    if runtime.shutdown_requested:
        disable_session(root, runtime.session_id)
        return TurnResolution(keep_running=False)
    match outcome:
        case TurnOutcome.COMPLETED:
            return _resolve_completed(root, runtime, goal_guard, turn_id)
        case TurnOutcome.INTERRUPTED:
            return _resolve_interrupted(root, runtime, turn_id)
        case TurnOutcome.FAILED:
            return TurnResolution(keep_running=False, failure_reason="turn_failed")
        case TurnOutcome.IN_PROGRESS | TurnOutcome.INVALID:
            return TurnResolution(keep_running=False, failure_reason="turn_status_invalid")
        case _:
            assert_never(outcome)


def _resolve_completed(
    root: Path,
    runtime: ManagerRuntime,
    goal_guard: GoalGuard | None,
    turn_id: str,
) -> TurnResolution:
    if not runtime.view.goal_companion:
        record_turn_finished(root, runtime.runtime_file, turn_id)
        return TurnResolution(keep_running=True)
    if goal_guard is None:
        return TurnResolution(keep_running=False, failure_reason="goal_identity_missing")
    status = goal_guard.status_after_turn()
    if status is GoalStatus.ACTIVE:
        return _continue_active_goal(root, runtime, goal_guard, turn_id)
    if status is GoalStatus.PAUSED:
        record_turn_finished(root, runtime.runtime_file, turn_id)
        return TurnResolution(keep_running=True)
    if status is GoalStatus.COMPLETE:
        return _complete_goal(root, runtime, turn_id)
    if status is GoalStatus.BLOCKED:
        failure_reason = "goal_blocked"
    elif status is GoalStatus.USAGE_LIMITED:
        failure_reason = "goal_usage_limited"
    elif status is GoalStatus.BUDGET_LIMITED:
        failure_reason = "goal_budget_limited"
    else:
        assert_never(status)
    return TurnResolution(keep_running=False, failure_reason=failure_reason)


def _complete_goal(root: Path, runtime: ManagerRuntime, turn_id: str) -> TurnResolution:
    record_turn_finished(root, runtime.runtime_file, turn_id)
    complete_session(root, runtime.session_id, datetime.now(UTC))
    return TurnResolution(keep_running=False)


def _continue_active_goal(
    root: Path,
    runtime: ManagerRuntime,
    goal_guard: GoalGuard,
    turn_id: str,
) -> TurnResolution:
    try:
        goal_guard.pause_for_interrupt()
    except GoalControlError as error:
        if error.reason_code != "goal_complete":
            raise
        return _complete_goal(root, runtime, turn_id)
    record_turn_finished(root, runtime.runtime_file, turn_id)
    return TurnResolution(keep_running=True)


def _resolve_interrupted(
    root: Path,
    runtime: ManagerRuntime,
    turn_id: str,
) -> TurnResolution:
    observed_at = datetime.now(UTC)
    claim = classify_interrupt_claim(runtime, turn_id, now=observed_at)
    match claim:
        case InterruptClaimState.MATCHED:
            record_restart_performed(
                root,
                runtime.runtime_file,
                turn_id,
                now=observed_at,
            )
            return TurnResolution(keep_running=True, restart_prompt_pending=True)
        case InterruptClaimState.UNCLAIMED:
            if not runtime.view.goal_companion:
                disable_session(root, runtime.session_id)
                return TurnResolution(keep_running=False)
            return TurnResolution(
                keep_running=False,
                failure_reason="turn_interrupted_external",
            )
        case InterruptClaimState.EXPIRED:
            return TurnResolution(keep_running=False, failure_reason="interrupt_claim_expired")
        case InterruptClaimState.MISMATCH:
            return TurnResolution(keep_running=False, failure_reason="interrupt_claim_mismatch")
        case _:
            assert_never(claim)
