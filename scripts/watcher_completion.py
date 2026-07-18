"""Persist and emit one crash-safe parent-task completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.diagnostics import DiagnosticCode, MonitorState
from scripts.silence import SilenceState, WaitState, initial_state, set_wait_state
from scripts.watcher_diagnostics import (
    TargetDiagnostic,
    append_target_diagnostic,
    completion_event_id,
)
from scripts.watcher_state import claim_completion, mark_target_terminal

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.state_io import JsonValue
    from scripts.watcher_models import RuntimeTarget

type DetectorKey = tuple[str, str | None, int]


@dataclass(frozen=True, slots=True)
class CompletionClock:
    """Monotonic and wall-clock values for one completion commit."""

    now: float
    wall_time: datetime


def complete_target(
    root: Path,
    target: RuntimeTarget,
    values: dict[str, JsonValue],
    detectors: dict[DetectorKey, SilenceState],
    clock: CompletionClock,
) -> None:
    """Record one parent-turn completion and terminalize its child detectors."""
    for monitor in target.targets:
        _ = mark_target_terminal(values, monitor.target_id, target.runtime_file)
    event_id = completion_event_id(target)
    if claim_completion(values, event_id, target.runtime_file):
        append_target_diagnostic(
            root,
            target,
            TargetDiagnostic(
                clock.wall_time,
                DiagnosticCode.WATCHER_COMPLETED,
                state=MonitorState.COMPLETED,
                event_id=event_id,
            ),
        )
    for monitor in target.targets:
        key = (target.session_id, monitor.target_id, monitor.generation)
        detectors[key] = set_wait_state(
            detectors.get(key, initial_state(clock.now)),
            WaitState(child_terminal=True),
            clock.now,
            resume_confirmed=False,
        )


def finish_if_terminal(
    root: Path,
    target: RuntimeTarget,
    values: dict[str, JsonValue],
    detectors: dict[DetectorKey, SilenceState],
    clock: CompletionClock,
) -> bool | None:
    """Finish a completed target, stop an empty target, or request evaluation."""
    if target.parent_complete:
        complete_target(root, target, values, detectors, clock)
        return False
    if not any(not monitor.terminal for monitor in target.targets):
        return False
    return None
