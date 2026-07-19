"""Map detector actions to sanitized diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from scripts.activity_epoch import turn_activity_epoch
from scripts.diagnostics import DiagnosticCode
from scripts.silence import Action, latest_progress_at, rearm_cancelled_restart
from scripts.watcher_diagnostics import TargetDiagnostic

if TYPE_CHECKING:
    from datetime import datetime

    from scripts.silence import SilenceState
    from scripts.state_io import JsonValue
    from scripts.watcher_models import MonitorTarget, RuntimeTarget


def rearm_if_restart_cancelled(
    state: SilenceState,
    values: dict[str, JsonValue],
    now: float,
) -> SilenceState:
    """Rearm a detector only after its persisted restart request disappears."""
    return rearm_cancelled_restart(state, now) if values.get("restart_request") is None else state


def diagnostic_for_action(
    action: Action,
    target: MonitorTarget,
    state: SilenceState,
    now: float,
    wall_time: datetime,
) -> TargetDiagnostic | None:
    """Create the fixed diagnostic for one actionable detector transition."""
    code = {
        Action.WARNING: DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE,
        Action.AUTO_RESTART: DiagnosticCode.RESTART_REQUESTED,
        Action.RESTART_LIMITED: DiagnosticCode.RESTART_UNAVAILABLE,
    }.get(action)
    if code is None:
        return None
    return TargetDiagnostic(
        occurred_at=wall_time,
        code=code,
        target=target,
        elapsed_ms=max(0, int((now - latest_progress_at(state)) * 1000)),
    )


def queue_restart_request(
    action: Action,
    runtime: RuntimeTarget,
    target: MonitorTarget,
    values: dict[str, JsonValue],
) -> None:
    """Queue one exact-turn request only for the manager-owned generation."""
    if action is not Action.AUTO_RESTART or values.get("restart_request") is not None:
        return
    turn_id = runtime.managed_turn_id
    if turn_id is None:
        return
    values["restart_request"] = {
        "request_id": uuid4().hex,
        "turn_id": turn_id,
        "target_id": target.target_id,
        "target_generation": target.generation,
        "progress_epoch": target.progress_epoch,
        "turn_activity_epoch": turn_activity_epoch(values, runtime.runtime_file),
    }
    values["restart_claimed"] = False
    values["restart_claimed_at"] = None


def cancel_unclaimed_restart_for_activity(
    activity_observed: bool,
    values: dict[str, JsonValue],
) -> None:
    """Cancel an unclaimed whole-turn interrupt after any tree activity."""
    if not activity_observed or values.get("restart_claimed") is True:
        return
    request = values.get("restart_request")
    if not isinstance(request, dict):
        return
    values["restart_request"] = None
    values["restart_claimed"] = False
    values["restart_claimed_at"] = None
