"""Apply allowlisted rollout events to one in-memory child detector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from scripts.event_source import EventKind
from scripts.silence import ProgressKind, record_progress

if TYPE_CHECKING:
    from scripts.event_source import ObservedEvent
    from scripts.silence import SilenceState
    from scripts.watcher_models import MonitorTarget, RuntimeTarget


@dataclass(frozen=True, slots=True)
class TargetEventContext:
    """Bind one rollout batch to its observation clock and parent turn."""

    events: tuple[ObservedEvent, ...]
    now: float
    parent_turn_id: str | None


def apply_target_events(
    open_calls: set[str],
    target: MonitorTarget,
    state: SilenceState,
    context: TargetEventContext,
) -> tuple[SilenceState, bool]:
    """Apply fresh progress and return whether the target became terminal."""
    terminal = False
    for event in context.events:
        if not _event_matches_target(event, target, context.parent_turn_id):
            continue
        match event.kind:
            case EventKind.ITEM | EventKind.TURN_STARTED:
                state = record_progress(state, ProgressKind.ITEM, context.now)
            case EventKind.DELTA:
                state = record_progress(state, ProgressKind.DELTA, context.now)
            case EventKind.TOOL_STARTED:
                state = _tool_started(open_calls, state, event, context.now)
            case EventKind.TOOL_RESULT:
                state = _tool_result(open_calls, state, event, context.now)
            case EventKind.TURN_COMPLETED | EventKind.TURN_ABORTED:
                state = record_progress(state, ProgressKind.ITEM, context.now)
            case EventKind.SESSION_METADATA:
                pass
            case _:
                assert_never(event.kind)
        terminal = terminal or _terminal_matches(event, target, context.parent_turn_id)
    return state, terminal


def _terminal_matches(
    event: ObservedEvent,
    target: MonitorTarget,
    parent_turn_id: str | None,
) -> bool:
    if not event.terminal:
        return False
    return target.target_id is not None or (
        parent_turn_id is not None and event.turn_id == parent_turn_id
    )


def event_is_target_progress(
    event: ObservedEvent,
    target: MonitorTarget,
    parent_turn_id: str | None,
) -> bool:
    """Return whether one event advances this exact monitor generation."""
    if not _event_matches_target(event, target, parent_turn_id):
        return False
    return event.kind in {
        EventKind.ITEM,
        EventKind.TURN_STARTED,
        EventKind.DELTA,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_RESULT,
    }


def event_is_turn_activity(event: ObservedEvent, target: RuntimeTarget) -> bool:
    """Return whether an unread event changes the owned turn tree."""
    return any(
        _event_matches_target(event, monitor, target.parent_turn_id)
        and (
            event_is_target_progress(event, monitor, target.parent_turn_id)
            or _terminal_matches(event, monitor, target.parent_turn_id)
        )
        for monitor in target.targets
    )


def _event_matches_target(
    event: ObservedEvent,
    target: MonitorTarget,
    parent_turn_id: str | None,
) -> bool:
    if event.child_id != target.target_id:
        return False
    if target.started_at is not None and event.occurred_at < target.started_at:
        return False
    return not (
        target.target_id is None
        and event.turn_id is not None
        and parent_turn_id is not None
        and event.turn_id != parent_turn_id
    )


def _tool_started(
    open_calls: set[str],
    state: SilenceState,
    event: ObservedEvent,
    now: float,
) -> SilenceState:
    if call_id := event.call_id or event.item_id:
        open_calls.add(call_id)
    return record_progress(state, ProgressKind.ITEM, now)


def _tool_result(
    open_calls: set[str],
    state: SilenceState,
    event: ObservedEvent,
    now: float,
) -> SilenceState:
    call_id = event.call_id or event.item_id
    if call_id is not None:
        open_calls.discard(call_id)
    elif event.fallback_tool_result and len(open_calls) == 1:
        _ = open_calls.pop()
    return record_progress(state, ProgressKind.TOOL_RESULT, now)


def parent_completed(target: RuntimeTarget, events: tuple[ObservedEvent, ...]) -> bool:
    """Recognize successful completion only for the locked parent turn."""
    return any(
        event.kind is EventKind.TURN_COMPLETED
        and event.child_id is None
        and target.parent_turn_id is not None
        and event.turn_id == target.parent_turn_id
        for event in events
    )
