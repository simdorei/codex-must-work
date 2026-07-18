"""Persist periodic heartbeat diagnostics for one runtime snapshot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.diagnostics import DiagnosticCode, MonitorState
from scripts.silence import latest_progress_at
from scripts.watcher_diagnostics import TargetDiagnostic, append_target_diagnostic

if TYPE_CHECKING:
    from scripts.silence import SilenceState
    from scripts.watcher_models import RuntimeTarget

type DetectorKey = tuple[str, str | None, int]
type HeartbeatContext = tuple[Path, dict[str, float], float, datetime]


def record_heartbeat(
    context: HeartbeatContext,
    target: RuntimeTarget,
    states: dict[DetectorKey, SilenceState],
    *,
    actionable: bool,
) -> None:
    """Emit one bounded heartbeat only after the runtime's warning interval."""
    root, heartbeat_at, now, wall_time = context
    previous = heartbeat_at.setdefault(target.session_id, now)
    if actionable:
        heartbeat_at[target.session_id] = now
        return
    if not states or now - previous < target.thresholds.warning:
        return
    latest = max(latest_progress_at(state) for state in states.values())
    monitor_state = (
        MonitorState.PAUSED
        if all(state.waits.paused for state in states.values())
        else MonitorState.ACTIVE
    )
    append_target_diagnostic(
        root,
        target,
        TargetDiagnostic(
            wall_time,
            DiagnosticCode.HEARTBEAT_ACTIVE,
            state=monitor_state,
            elapsed_ms=max(0, int((now - latest) * 1000)),
        ),
    )
    heartbeat_at[target.session_id] = now
