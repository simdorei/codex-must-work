"""Rehydrate one passive detector from persisted wall-clock evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from scripts.stall_detector import MonitorPhase, SilenceState, initial_state

if TYPE_CHECKING:
    from scripts.monitor_target import MonitorTarget
    from scripts.watcher_context import TickContext


def recovered_detector_state(
    context: TickContext,
    monitor: MonitorTarget,
) -> SilenceState:
    """Anchor a fresh process to the persisted start of observable silence."""
    if monitor.started_at is None:
        return initial_state(context.now)
    elapsed = max(0.0, (context.wall_time - monitor.started_at).total_seconds())
    return initial_state(context.now - elapsed)


def restore_suspected_transition(state: SilenceState) -> SilenceState:
    """Restore a persisted first-stage transition without emitting it again."""
    return replace(
        state,
        phase=MonitorPhase.SILENT_WARNED,
        warning_emitted=True,
    )
