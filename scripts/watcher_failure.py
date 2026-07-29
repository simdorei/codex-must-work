"""Commit rollout failures only while their runtime snapshot is current."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.monitor_diagnostics import DiagnosticCode, MonitorState
from scripts.monitor_state import runtime_target_from_values
from scripts.state import mutate_existing_state
from scripts.watcher_diagnostics import TargetDiagnostic, append_target_diagnostic

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.monitor_target import RuntimeTarget
    from scripts.state_io import JsonValue


def record_rollout_failure(
    root: Path,
    snapshot: RuntimeTarget,
    code: DiagnosticCode,
    wall_time: datetime,
    failed_sessions: set[str],
) -> None:
    """Record one failure if no newer hook update replaced the snapshot."""

    def fail(values: dict[str, JsonValue]) -> None:
        if values.get("enabled") is not True:
            return
        target = runtime_target_from_values(root, snapshot.runtime_file, values)
        if target.revision != snapshot.revision:
            return
        append_target_diagnostic(
            root,
            target,
            TargetDiagnostic(wall_time, code, state=MonitorState.FAILED_CLOSED),
        )
        failed_sessions.add(target.session_id)

    _ = mutate_existing_state(root, snapshot.runtime_file, fail)
