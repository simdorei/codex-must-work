"""Apply observed rollout progress to one monitored runtime target."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from scripts.monitor_diagnostics import DiagnosticCode
from scripts.monitor_state import (
    advance_target_progress_epoch,
    mark_parent_complete,
    mark_target_terminal,
)
from scripts.stall_detector import SilenceState, WaitState, set_wait_state
from scripts.watcher_diagnostics import TargetDiagnostic, append_target_diagnostic
from scripts.watcher_events import TargetEventContext, apply_target_events, parent_completed
from scripts.watcher_recovery import recovered_detector_state, restore_suspected_transition

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.event_source import ObservedEvent
    from scripts.monitor_target import MonitorTarget, RuntimeTarget
    from scripts.state_io import JsonValue
    from scripts.watcher_context import DetectorKey, TickContext
    from scripts.watcher_notifications import WatcherNotificationRecorder


@dataclass(frozen=True, slots=True)
class TargetProgress:
    """Prepared detector states and target-level activity signals."""

    states: dict[DetectorKey, SilenceState]
    activity_observed: bool
    parent_terminal: bool


@dataclass(frozen=True, slots=True)
class _MonitorProgress:
    state: SilenceState
    activity_observed: bool


@dataclass(frozen=True, slots=True)
class _PreparationContext:
    target: RuntimeTarget
    values: dict[str, JsonValue]
    events: tuple[ObservedEvent, ...]
    tick: TickContext
    parent_terminal: bool


@final
class TargetProgressPreparer:
    """Apply rollout events using watcher-owned process-local detector state."""

    def __init__(
        self,
        root: Path,
        detectors: dict[DetectorKey, SilenceState],
        open_calls: dict[DetectorKey, set[str]],
        started: set[DetectorKey],
        recorder: WatcherNotificationRecorder,
    ) -> None:
        """Bind shared detector collections to one state root."""
        self._root = root
        self._detectors = detectors
        self._open_calls = open_calls
        self._started = started
        self._recorder = recorder

    def prepare(
        self,
        target: RuntimeTarget,
        values: dict[str, JsonValue],
        events: tuple[ObservedEvent, ...],
        tick: TickContext,
    ) -> TargetProgress:
        """Prepare every child detector before threshold evaluation."""
        terminal = parent_completed(target, events)
        context = _PreparationContext(target, values, events, tick, terminal)
        if terminal:
            _ = mark_parent_complete(values, target.runtime_file)
        states: dict[DetectorKey, SilenceState] = {}
        activity_observed = False
        for monitor in target.targets:
            key = (target.session_id, monitor.target_id, monitor.generation)
            progress = self._prepare_monitor(
                context,
                monitor,
                self._detectors.get(key),
                self._open_calls.setdefault(key, set()),
            )
            states[key] = progress.state
            activity_observed = progress.activity_observed or activity_observed
        return TargetProgress(states, activity_observed, terminal)

    def _prepare_monitor(
        self,
        context: _PreparationContext,
        monitor: MonitorTarget,
        existing: SilenceState | None,
        open_calls: set[str],
    ) -> _MonitorProgress:
        target = context.target
        tick = context.tick
        key = (target.session_id, monitor.target_id, monitor.generation)
        state = existing or recovered_detector_state(tick, monitor)
        sequence_before_events = state.silence_sequence
        warned = self._recorder.warning_was_emitted(target, monitor, state)
        if warned and not state.warning_emitted:
            state = restore_suspected_transition(state)
        self._record_started(context, monitor, key)
        state, event_terminal = apply_target_events(
            open_calls,
            monitor,
            state,
            TargetEventContext(context.events, tick.now, target.parent_turn_id),
        )
        progressed = state.silence_sequence > sequence_before_events
        if progressed:
            advance_target_progress_epoch(
                context.values,
                monitor.target_id,
                target.runtime_file,
                tick.wall_time,
            )
            if warned:
                self._recorder.record_recovery(target, monitor, tick.wall_time)
        if event_terminal:
            _ = mark_target_terminal(
                context.values,
                monitor.target_id,
                target.runtime_file,
            )
        waits = WaitState(
            open_tool_count=max(monitor.open_tool_count, len(open_calls)),
            waiting_for_approval=monitor.waiting_for_approval,
            waiting_for_user=monitor.waiting_for_user,
            child_terminal=monitor.terminal or event_terminal,
            parent_complete=context.parent_terminal,
        )
        return _MonitorProgress(
            set_wait_state(state, waits, tick.now, resume_confirmed=not waits.paused),
            progressed or event_terminal,
        )

    def _record_started(
        self,
        context: _PreparationContext,
        monitor: MonitorTarget,
        key: DetectorKey,
    ) -> None:
        if key in self._started or monitor.terminal:
            return
        append_target_diagnostic(
            self._root,
            context.target,
            TargetDiagnostic(
                context.tick.wall_time,
                DiagnosticCode.WATCHER_STARTED,
                monitor,
            ),
        )
        self._started.add(key)
