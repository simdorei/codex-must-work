from scripts.manager_decision import (
    ManagerAction,
    ManagerView,
    decide_manager_action,
)


def test_stalled_owned_turn_is_interrupted_before_same_thread_restart() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=False,
        managed_turn_id="turn-owned",
        restart_request_turn_id="turn-owned",
    )

    decision = decide_manager_action(view, active_turn_id="turn-owned")

    assert decision.action is ManagerAction.INTERRUPT
    assert decision.turn_id == "turn-owned"


def test_restart_request_for_unowned_turn_fails_closed() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=False,
        managed_turn_id="turn-owned",
        restart_request_turn_id="turn-other",
    )

    decision = decide_manager_action(view, active_turn_id="turn-owned")

    assert decision.action is ManagerAction.FAIL_CLOSED
    assert decision.reason_code == "restart_turn_not_owned"


def test_completed_activation_turn_hands_next_turn_to_manager() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=True,
        managed_turn_id=None,
        restart_request_turn_id=None,
    )

    decision = decide_manager_action(view, active_turn_id=None)

    assert decision.action is ManagerAction.START


def test_manager_waits_until_exact_started_turn_is_active() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=False,
        managed_turn_id="turn-owned",
        restart_request_turn_id="turn-owned",
    )

    decision = decide_manager_action(view, active_turn_id=None)

    assert decision.action is ManagerAction.WAIT


def test_goal_companion_rejects_preexisting_active_turn() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=True,
        managed_turn_id=None,
        restart_request_turn_id=None,
        goal_companion=True,
    )

    decision = decide_manager_action(view, active_turn_id="turn-goal")

    assert decision.action is ManagerAction.FAIL_CLOSED
    assert decision.reason_code == "unexpected_active_turn"


def test_goal_companion_resumes_goal_when_no_turn_is_active() -> None:
    view = ManagerView(
        enabled=True,
        handoff_requested=True,
        managed_turn_id=None,
        restart_request_turn_id=None,
        goal_companion=True,
    )

    decision = decide_manager_action(view, active_turn_id=None)

    assert decision.action is ManagerAction.RESUME_GOAL
