"""Run privacy-safe silence detection over incremental rollout events."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from scripts.activity_epoch import persist_turn_activity
from scripts.notifications import NotificationSink, NullNotificationSink
from scripts.stall_detector import SilenceState, evaluate
from scripts.watcher_actions import (
    diagnostic_for_action,
)
from scripts.watcher_completion import CompletionClock, complete_target, finish_if_terminal
from scripts.watcher_context import DetectorKey, TickContext
from scripts.watcher_heartbeat import record_heartbeat
from scripts.watcher_notifications import WatcherNotificationRecorder
from scripts.watcher_progress import TargetProgressPreparer
from scripts.watcher_runtime import RuntimeFileMonitor

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.event_source import ObservedEvent
    from scripts.monitor_target import RuntimeTarget
    from scripts.state_io import JsonValue


@final
class WatcherEngine:
    """Maintain detector state while a user-level watcher process is alive."""

    def __init__(
        self,
        root: Path,
        *,
        notification_sink: NotificationSink | None = None,
    ) -> None:
        """Initialize empty in-memory detector state for one state root."""
        self._root = root
        sink = notification_sink or NullNotificationSink()
        self._notification_recorder = WatcherNotificationRecorder(root, sink)
        self._detectors: dict[DetectorKey, SilenceState] = {}
        self._open_calls: dict[DetectorKey, set[str]] = {}
        self._started: set[DetectorKey] = set()
        self._heartbeat_at: dict[str, float] = {}
        self._progress = TargetProgressPreparer(
            root,
            self._detectors,
            self._open_calls,
            self._started,
            self._notification_recorder,
        )
        self._runtime = RuntimeFileMonitor(root)

    def tick(self, now: float, wall_time: datetime) -> bool:
        """Process one bounded batch and report whether monitoring should continue."""
        context = TickContext(now, wall_time)
        active = False
        try:
            active = self._runtime.tick(
                context,
                lambda target, values, events: self._tick_target(
                    target,
                    values,
                    events,
                    context,
                ),
            )
        finally:
            self._notification_recorder.flush()
        return active

    def _tick_target(
        self,
        target: RuntimeTarget,
        values: dict[str, JsonValue],
        events: tuple[ObservedEvent, ...],
        context: TickContext,
    ) -> bool:
        if (
            finished := finish_if_terminal(
                target,
                values,
                self._detectors,
                CompletionClock(context.now, context.wall_time),
                self._notification_recorder.record,
            )
        ) is not None:
            return finished
        return self._evaluate_target(target, values, events, context)

    def _evaluate_target(
        self,
        target: RuntimeTarget,
        values: dict[str, JsonValue],
        events: tuple[ObservedEvent, ...],
        context: TickContext,
    ) -> bool:
        now = context.now
        progress = self._progress.prepare(
            target,
            values,
            events,
            context,
        )
        _ = persist_turn_activity(
            values,
            target.runtime_file,
            progress.activity_observed or progress.parent_terminal,
        )
        if progress.parent_terminal:
            complete_target(
                target,
                values,
                self._detectors,
                CompletionClock(context.now, context.wall_time),
                self._notification_recorder.record,
            )
            return False

        active = actionable = False
        for monitor in target.targets:
            key = (target.session_id, monitor.target_id, monitor.generation)
            state = progress.states[key]
            result = evaluate(
                state,
                now,
                target.thresholds,
            )
            self._detectors[key] = result.state
            diagnostic = diagnostic_for_action(
                result.action,
                target,
                monitor,
                result.state,
                context,
            )
            if diagnostic is not None:
                self._notification_recorder.record(target, diagnostic)
                actionable = True
            active = active or not result.state.waits.terminal
        record_heartbeat(
            (
                self._root,
                self._heartbeat_at,
                context.now,
                context.wall_time,
            ),
            target,
            progress.states,
            actionable=actionable,
        )
        return active
