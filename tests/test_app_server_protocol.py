from scripts.app_server_protocol import AppServerEventState, TurnOutcome


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

    state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.INTERRUPTED


def test_turn_completion_notification_preserves_failed_outcome() -> None:
    state = AppServerEventState()

    state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "failed"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.FAILED


def test_turn_completion_notification_preserves_impossible_in_progress_status() -> None:
    state = AppServerEventState()

    state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "inProgress"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.IN_PROGRESS


def test_turn_completion_notification_marks_missing_status_invalid() -> None:
    state = AppServerEventState()

    state.record(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1"}},
        }
    )

    assert state.turn_outcome("turn-1") is TurnOutcome.INVALID


def test_duplicate_turn_completion_keeps_first_observed_status() -> None:
    state = AppServerEventState()
    for status in ("completed", "failed"):
        state.record(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": status}},
            }
        )

    assert state.turn_outcome("turn-1") is TurnOutcome.COMPLETED


def test_turn_notifications_without_thread_id_bind_to_request_owner() -> None:
    state = AppServerEventState()
    state.record(
        {
            "method": "turn/started",
            "params": {"turn": {"id": "turn-1", "status": "inProgress"}},
        }
    )

    assert state.was_started("turn-1") is True
    assert state.active_turn("thread-1") is None
    assert state.bind_started_turn("thread-1", "turn-1") is True
    assert state.active_turn("thread-1") == "turn-1"

    state.record(
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

    state.record(
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }
    )

    assert state.pending_server_request == "item/commandExecution/requestApproval"
