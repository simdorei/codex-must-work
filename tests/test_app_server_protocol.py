import pytest

from scripts.app_server_protocol import (
    AppServerEventState,
    AppServerProtocolError,
    TurnOutcome,
)


def test_resident_notifications_track_exact_owned_turn_lifecycle() -> None:
    state = AppServerEventState()

    state.record(
        {
            "method": "turn/started",
            "params": {
                "thread": {"id": "thread-1"},
                "turn": {"id": "turn-1", "threadId": "thread-1"},
            },
        }
    )

    assert state.active_turn("thread-1") == "turn-1"
    assert state.was_started("turn-1") is True
    state.record(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )
    assert state.active_turn("thread-1") is None
    assert state.was_completed("turn-1") is True
    assert state.latest_started_turn("thread-1") == "turn-1"


def test_turn_completion_notification_preserves_interrupted_outcome() -> None:
    state = AppServerEventState()

    _ = state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.INTERRUPTED


def test_turn_completion_notification_preserves_failed_outcome() -> None:
    state = AppServerEventState()

    _ = state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "failed"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.FAILED


def test_turn_completion_notification_preserves_impossible_in_progress_status() -> None:
    state = AppServerEventState()

    _ = state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "inProgress"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.IN_PROGRESS


def test_turn_completion_notification_marks_missing_status_invalid() -> None:
    state = AppServerEventState()

    _ = state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.INVALID


def test_duplicate_turn_completion_keeps_first_observed_status() -> None:
    state = AppServerEventState()
    for status in ("completed", "failed"):
        _ = state.record(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": status}},
            }
        )

    assert state.turn_outcome("turn-1") is TurnOutcome.COMPLETED


def test_turn_notifications_without_thread_id_bind_to_request_owner() -> None:
    state = AppServerEventState()
    _ = state.record(
        {
            "method": "turn/started",
            "params": {"turn": {"id": "turn-1", "status": "inProgress"}},
        }
    )

    assert state.was_started("turn-1") is True
    assert state.active_turn("thread-1") is None
    assert state.bind_started_turn("thread-1", "turn-1") is True
    assert state.active_turn("thread-1") == "turn-1"

    _ = state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed"}},
        }
    )

    assert state.was_completed("turn-1") is True
    assert state.active_turn("thread-1") is None
    assert state.latest_started_turn("thread-1") == "turn-1"


def test_server_request_is_exposed_instead_of_silently_ignored() -> None:
    state = AppServerEventState()

    _ = state.record(
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }
    )

    assert state.pending_server_request == "item/commandExecution/requestApproval"


def test_completed_turn_outcomes_are_bounded() -> None:
    # Given
    state = AppServerEventState()

    # When
    for index in range(513):
        state.record(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": f"turn-{index}", "status": "completed"},
                },
            }
        )

    # Then
    assert state.turn_outcome("turn-0") is None
    assert state.turn_outcome("turn-1") is TurnOutcome.COMPLETED
    assert state.turn_outcome("turn-512") is TurnOutcome.COMPLETED


def test_2049th_live_turn_correlation_is_rejected_without_evicting_live_owner() -> None:
    # Given
    state = AppServerEventState()
    for index in range(2048):
        state.correlate_turn(f"thread-{index}", f"turn-{index}")

    # When / Then
    with pytest.raises(AppServerProtocolError, match="app_server_correlation_capacity"):
        state.correlate_turn("thread-overflow", "turn-overflow")
    assert state.thread_for_turn("turn-0") == "thread-0"


def test_terminal_correlation_expires_after_ten_minutes() -> None:
    # Given
    now = [0.0]
    state = AppServerEventState(clock=lambda: now[0])
    state.correlate_turn("thread-old", "turn-old")
    state.record(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-old",
                "turn": {"id": "turn-old", "status": "completed"},
            },
        }
    )

    # When
    now[0] = 601.0
    state.correlate_turn("thread-new", "turn-new")

    # Then
    assert state.thread_for_turn("turn-old") is None
    assert state.thread_for_turn("turn-new") == "thread-new"
