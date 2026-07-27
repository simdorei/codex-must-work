from __future__ import annotations

from typing import TYPE_CHECKING, final

import pytest

from scripts.app_server_protocol import TurnOutcome, decode_object
from scripts.diagnostics import DiagnosticCode
from scripts.goal_control import GoalControlError, GoalStatus
from scripts.manager_outcome import resolve_turn_outcome
from scripts.manager_restart_guard import claim_restart_request
from scripts.manager_runtime import ManagerRuntime, load_manager_runtime, record_turn_started
from scripts.state import StateDocument, load_state, save_state
from tests.manager_fixture import arm_restart
from tests.test_manager_engine_goal import runtime_fixture

if TYPE_CHECKING:
    from pathlib import Path


@final
class FakeGoalGuard:
    """Expose typed Goal outcomes without crossing the unsupported policy gate."""

    def __init__(
        self,
        status: GoalStatus,
        *,
        status_error: GoalControlError | None = None,
    ) -> None:
        self.status: GoalStatus = status
        self.status_error: GoalControlError | None = status_error
        self.paused: bool = False

    def status_after_turn(self) -> GoalStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.status

    def pause_for_interrupt(self) -> None:
        if self.status is GoalStatus.COMPLETE:
            reason = "goal_complete"
            raise GoalControlError(reason)
        self.paused = True


def _owned_goal_runtime(tmp_path: Path) -> tuple[Path, Path, ManagerRuntime]:
    root, path = runtime_fixture(tmp_path, goal_companion=True)
    record_turn_started(root, path, "turn-goal-1")
    document = load_state(root, path)
    values = dict(document.values)
    values["manager_ready"] = True
    values["manager_pid"] = 123
    save_state(root, path, StateDocument(values=values))
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None
    return root, path, runtime


def _completion_count(root: Path) -> int:
    diagnostic = root / "logs" / "diagnostic.jsonl"
    if not diagnostic.exists():
        return 0
    count = 0
    for line in diagnostic.read_text(encoding="utf-8").splitlines():
        event = decode_object(line)
        if event is not None and event.get("code") == DiagnosticCode.WATCHER_COMPLETED.value:
            count += 1
    return count


def test_completed_owned_turn_with_complete_goal_records_one_verified_shutdown(
    tmp_path: Path,
) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)
    guard = FakeGoalGuard(GoalStatus.COMPLETE)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        guard,
        "turn-goal-1",
        TurnOutcome.COMPLETED,
    )

    assert resolution.keep_running is False
    assert resolution.failure_reason is None
    assert not path.exists()
    assert _completion_count(root) == 1


def test_external_interruption_of_owned_goal_turn_fails_closed(tmp_path: Path) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        FakeGoalGuard(GoalStatus.ACTIVE),
        "turn-goal-1",
        TurnOutcome.INTERRUPTED,
    )

    persisted = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == "turn_interrupted_external"
    assert persisted["handoff_requested"] is False
    assert persisted["managed_turn_id"] == "turn-goal-1"
    assert _completion_count(root) == 0


def test_failed_owned_goal_turn_fails_closed_without_retry(tmp_path: Path) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        FakeGoalGuard(GoalStatus.ACTIVE),
        "turn-goal-1",
        TurnOutcome.FAILED,
    )

    persisted = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == "turn_failed"
    assert persisted["handoff_requested"] is False
    assert persisted["managed_turn_id"] == "turn-goal-1"
    assert _completion_count(root) == 0


@pytest.mark.parametrize("outcome", [TurnOutcome.IN_PROGRESS, TurnOutcome.INVALID])
def test_impossible_terminal_status_fails_closed(
    tmp_path: Path,
    outcome: TurnOutcome,
) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        FakeGoalGuard(GoalStatus.ACTIVE),
        "turn-goal-1",
        outcome,
    )

    persisted = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == "turn_status_invalid"
    assert persisted["handoff_requested"] is False
    assert persisted["managed_turn_id"] == "turn-goal-1"


def test_active_goal_is_paused_and_continues_after_successful_turn(tmp_path: Path) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)
    guard = FakeGoalGuard(GoalStatus.ACTIVE)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        guard,
        "turn-goal-1",
        TurnOutcome.COMPLETED,
    )

    persisted = load_state(root, path).values
    assert resolution.keep_running is True
    assert resolution.failure_reason is None
    assert persisted["managed_turn_id"] is None
    assert persisted["handoff_requested"] is True
    assert guard.paused is True


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [
        ("blocked", "goal_blocked"),
        ("usageLimited", "goal_usage_limited"),
        ("budgetLimited", "goal_budget_limited"),
    ],
)
def test_non_resumable_goal_status_fails_closed_with_exact_reason(
    tmp_path: Path,
    status: str,
    reason_code: str,
) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)

    resolution = resolve_turn_outcome(
        root,
        runtime,
        FakeGoalGuard(GoalStatus(status)),
        "turn-goal-1",
        TurnOutcome.COMPLETED,
    )

    persisted = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == reason_code
    assert persisted["managed_turn_id"] == "turn-goal-1"
    assert persisted["handoff_requested"] is False


def test_goal_identity_change_after_owned_turn_fails_closed(tmp_path: Path) -> None:
    root, path, runtime = _owned_goal_runtime(tmp_path)
    guard = FakeGoalGuard(
        GoalStatus.ACTIVE,
        status_error=GoalControlError("goal_identity_changed"),
    )

    with pytest.raises(GoalControlError, match="goal_identity_changed"):
        _ = resolve_turn_outcome(
            root,
            runtime,
            guard,
            "turn-goal-1",
            TurnOutcome.COMPLETED,
        )

    persisted = load_state(root, path).values
    assert persisted["managed_turn_id"] == "turn-goal-1"
    assert persisted["handoff_requested"] is False


def test_recent_matching_interrupt_claim_recovers_one_replacement(tmp_path: Path) -> None:
    root, path, _runtime = _owned_goal_runtime(tmp_path)
    arm_restart(root, path, "turn-goal-1")
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None
    assert claim_restart_request(root, runtime) is True
    claimed = load_manager_runtime(root, path.name)
    assert claimed is not None

    resolution = resolve_turn_outcome(
        root,
        claimed,
        FakeGoalGuard(GoalStatus.ACTIVE),
        "turn-goal-1",
        TurnOutcome.INTERRUPTED,
    )

    recovered = load_state(root, path).values
    assert resolution.keep_running is True
    assert resolution.restart_prompt_pending is True
    assert recovered["restart_count"] == 1
    assert recovered["managed_turn_id"] is None
    assert recovered["handoff_requested"] is True


def test_expired_interrupt_claim_never_authorizes_replacement(tmp_path: Path) -> None:
    root, path, _runtime = _owned_goal_runtime(tmp_path)
    arm_restart(root, path, "turn-goal-1")
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None
    assert claim_restart_request(root, runtime) is True
    values = dict(load_state(root, path).values)
    values["restart_claimed_at"] = "2000-01-01T00:00:00+00:00"
    save_state(root, path, StateDocument(values=values))
    expired_runtime = load_manager_runtime(root, path.name)
    assert expired_runtime is not None

    resolution = resolve_turn_outcome(
        root,
        expired_runtime,
        FakeGoalGuard(GoalStatus.ACTIVE),
        "turn-goal-1",
        TurnOutcome.INTERRUPTED,
    )

    expired = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == "interrupt_claim_expired"
    assert expired["restart_count"] == 0
    assert expired["managed_turn_id"] == "turn-goal-1"
