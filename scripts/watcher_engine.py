"""Run privacy-safe silence detection over incremental rollout events."""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING, final

from scripts.activity_epoch import persist_turn_activity
from scripts.diagnostics import DiagnosticCode
from scripts.silence import (
    RestartGate,
    SilenceState,
    WaitState,
    evaluate,
    initial_state,
    set_wait_state,
)
from scripts.state import mutate_existing_state
from scripts.watcher_actions import (
    cancel_unclaimed_restart_for_activity,
    diagnostic_for_action,
    queue_restart_request,
    rearm_if_restart_cancelled,
)
from scripts.watcher_batch import TargetBatch, read_target_batch
from scripts.watcher_commit import commit_runtime_snapshot
from scripts.watcher_completion import CompletionClock, complete_target, finish_if_terminal
from scripts.watcher_context import DetectorKey, TickContext, restart_eligible_target_count
from scripts.watcher_diagnostics import TargetDiagnostic, append_target_diagnostic
from scripts.watcher_events import TargetEventContext, apply_target_events, parent_completed
from scripts.watcher_failure import record_rollout_failure
from scripts.watcher_heartbeat import record_heartbeat
from scripts.watcher_source import RolloutCorruptError, RolloutRotatedError
from scripts.watcher_state import (
    advance_target_progress_epoch,
    discover_runtime_files,
    mark_parent_complete,
    mark_target_terminal,
    runtime_target_from_values,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.event_source import ObservedEvent
    from scripts.state_io import JsonValue
    from scripts.watcher_models import RuntimeTarget


@final
class WatcherEngine:
    """Maintain detector state while a user-level watcher process is alive."""

    def __init__(self, root: Path) -> None:
        """Initialize empty in-memory detector state for one state root."""
        self._root = root
        self._detectors: dict[DetectorKey, SilenceState] = {}
        self._open_calls: dict[DetectorKey, set[str]] = {}
        self._started: set[DetectorKey] = set()
        self._failed_sessions: set[str] = set()
        self._heartbeat_at: dict[str, float] = {}

    def tick(self, now: float, wall_time: datetime) -> bool:
        """Process one bounded batch and report whether monitoring should continue."""
        context = TickContext(now, wall_time)
        active = False
        for runtime_file in discover_runtime_files(self._root):

            def snapshot(
                values: dict[str, JsonValue],
                runtime_file: Path = runtime_file,
            ) -> RuntimeTarget | None:
                if values.get("enabled") is not True:
                    return None
                return runtime_target_from_values(self._root, runtime_file, values)

            target = mutate_existing_state(self._root, runtime_file, snapshot)
            if target is None or target.session_id in self._failed_sessions:
                continue
            if target.parent_complete:
                active = self._commit_snapshot(target, None, context) or active
                continue
            if not any(not monitor.terminal for monitor in target.targets):
                continue
            try:
                batch = read_target_batch(self._root, target)
            except RolloutCorruptError:
                record_rollout_failure(
                    self._root,
                    target,
                    DiagnosticCode.ROLLOUT_CORRUPT,
                    context.wall_time,
                    self._failed_sessions,
                )
                continue
            except RolloutRotatedError:
                record_rollout_failure(
                    self._root,
                    target,
                    DiagnosticCode.ROLLOUT_ROTATED,
                    context.wall_time,
                    self._failed_sessions,
                )
                continue
            active = self._commit_snapshot(target, batch, context) or active
        return active

    def _commit_snapshot(
        self,
        snapshot: RuntimeTarget,
        batch: TargetBatch | None,
        context: TickContext,
    ) -> bool:
        return commit_runtime_snapshot(
            self._root,
            snapshot,
            batch,
            lambda target, values, events: self._tick_target(
                target,
                values,
                events,
                context,
            ),
        )

    def _tick_target(
        self,
        target: RuntimeTarget,
        values: dict[str, JsonValue],
        events: tuple[ObservedEvent, ...],
        context: TickContext,
    ) -> bool:
        if (
            finished := finish_if_terminal(
                self._root,
                target,
                values,
                self._detectors,
                CompletionClock(context.now, context.wall_time),
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
        states: dict[DetectorKey, SilenceState] = {}
        activity_observed = False
        parent_terminal = parent_completed(target, events)
        if parent_terminal:
            _ = mark_parent_complete(values, target.runtime_file)
        for monitor in target.targets:
            key = (target.session_id, monitor.target_id, monitor.generation)
            state = self._detectors.get(key, initial_state(now))
            state = rearm_if_restart_cancelled(state, values, now)
            sequence_before_events = state.silence_sequence
            if key not in self._started and not monitor.terminal:
                append_target_diagnostic(
                    self._root,
                    target,
                    TargetDiagnostic(
                        context.wall_time,
                        DiagnosticCode.WATCHER_STARTED,
                        monitor,
                    ),
                )
                self._started.add(key)
            open_calls = self._open_calls.setdefault(key, set())
            state, event_terminal = apply_target_events(
                open_calls,
                monitor,
                state,
                TargetEventContext(events, now, target.parent_turn_id),
            )
            if state.silence_sequence > sequence_before_events:
                advance_target_progress_epoch(values, monitor.target_id, target.runtime_file)
                activity_observed = True
            if event_terminal:
                _ = mark_target_terminal(values, monitor.target_id, target.runtime_file)
                activity_observed = True
            waits = WaitState(
                open_tool_count=(
                    0
                    if target.managed_mode and target.auto_restart_requested
                    else max(monitor.open_tool_count, len(self._open_calls.get(key, set())))
                ),
                waiting_for_approval=monitor.waiting_for_approval,
                waiting_for_user=monitor.waiting_for_user,
                child_terminal=monitor.terminal or event_terminal,
                parent_complete=parent_terminal,
            )
            states[key] = set_wait_state(state, waits, now, resume_confirmed=not waits.paused)

        activity_observed = persist_turn_activity(
            values, target.runtime_file, activity_observed or parent_terminal
        )
        cancel_unclaimed_restart_for_activity(activity_observed, values)

        if parent_terminal:
            complete_target(
                self._root,
                target,
                values,
                self._detectors,
                CompletionClock(context.now, context.wall_time),
            )
            return False

        eligible_siblings = restart_eligible_target_count(
            states.values(),
            now,
            target.thresholds.restart,
        )
        active = actionable = False
        for monitor in target.targets:
            key = (target.session_id, monitor.target_id, monitor.generation)
            state = states[key]
            result = evaluate(
                state,
                now,
                target.thresholds,
                RestartGate(
                    requested_by_user=target.auto_restart_requested,
                    capability_ready=(
                        target.managed_mode
                        and target.manager_ready
                        and not target.observe_only
                        and target.managed_turn_id == target.parent_turn_id
                    ),
                    latest_event_check_passed=True,
                    target_generation_verified=True,
                    clock_reliable=isfinite(now),
                    restart_eligible_siblings=eligible_siblings,
                ),
            )
            self._detectors[key] = result.state
            queue_restart_request(result.action, target, monitor, values)
            diagnostic = diagnostic_for_action(
                result.action,
                monitor,
                result.state,
                context.now,
                context.wall_time,
            )
            if diagnostic is not None:
                append_target_diagnostic(self._root, target, diagnostic)
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
            states,
            actionable=actionable,
        )
        return active
