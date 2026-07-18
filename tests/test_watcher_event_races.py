from datetime import UTC, datetime

from scripts.event_source import EventKind, ObservedEvent
from scripts.silence import initial_state
from scripts.watcher_events import TargetEventContext, apply_target_events
from scripts.watcher_models import MonitorTarget


def test_late_terminal_from_prior_parent_turn_does_not_finish_replacement() -> None:
    target = MonitorTarget(
        target_id=None,
        generation=2,
        terminal=False,
        open_tool_count=0,
        waiting_for_approval=False,
        waiting_for_user=False,
        progress_epoch=0,
        started_at=None,
    )
    event = ObservedEvent(
        kind=EventKind.TURN_ABORTED,
        occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
        turn_id="turn-1",
        terminal=True,
    )

    _state, terminal = apply_target_events(
        set(),
        target,
        initial_state(0.0),
        TargetEventContext((event,), 1.0, "turn-2"),
    )

    assert terminal is False


def test_late_terminal_from_prior_child_generation_is_ignored_by_start_time() -> None:
    started_at = datetime(2026, 7, 18, 0, 0, 2, tzinfo=UTC)
    target = MonitorTarget(
        target_id="child-1",
        generation=2,
        terminal=False,
        open_tool_count=0,
        waiting_for_approval=False,
        waiting_for_user=False,
        progress_epoch=0,
        started_at=started_at,
    )
    event = ObservedEvent(
        kind=EventKind.TURN_ABORTED,
        occurred_at=datetime(2026, 7, 18, 0, 0, 1, tzinfo=UTC),
        child_id="child-1",
        terminal=True,
    )

    _state, terminal = apply_target_events(
        set(),
        target,
        initial_state(0.0),
        TargetEventContext((event,), 1.0, "turn-2"),
    )

    assert terminal is False
