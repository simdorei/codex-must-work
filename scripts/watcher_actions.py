"""Map detector actions to sanitized diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.monitor_diagnostics import DiagnosticCode
from scripts.stall_detector import Action, latest_progress_at
from scripts.watcher_diagnostics import TargetDiagnostic, lifecycle_event_id

if TYPE_CHECKING:
    from scripts.monitor_target import MonitorTarget, RuntimeTarget
    from scripts.stall_detector import SilenceState
    from scripts.watcher_context import TickContext


def diagnostic_for_action(
    action: Action,
    runtime: RuntimeTarget,
    target: MonitorTarget,
    state: SilenceState,
    context: TickContext,
) -> TargetDiagnostic | None:
    """Create the fixed diagnostic for one actionable detector transition."""
    code = {
        Action.WARNING: DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE,
        Action.CRITICAL: DiagnosticCode.BOTTLENECK_CRITICAL,
    }.get(action)
    if code is None:
        return None
    return TargetDiagnostic(
        occurred_at=context.wall_time,
        code=code,
        target=target,
        elapsed_ms=max(0, int((context.now - latest_progress_at(state)) * 1000)),
        event_id=lifecycle_event_id(code, runtime, target),
    )
