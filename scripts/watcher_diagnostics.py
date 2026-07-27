"""Map watcher targets to the fixed sanitized diagnostic schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.diagnostics import (
    DiagnosticCode,
    DiagnosticEvent,
    MonitorState,
    append_diagnostic_once,
    diagnostic_event_exists,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.watcher_models import MonitorTarget, RuntimeTarget


@dataclass(frozen=True, slots=True)
class TargetDiagnostic:
    """One safe diagnostic request tied to an allowlisted target."""

    occurred_at: datetime
    code: DiagnosticCode
    target: MonitorTarget | None = None
    state: MonitorState = MonitorState.ACTIVE
    elapsed_ms: int | None = None
    event_id: str | None = None


def append_target_diagnostic(
    root: Path,
    target: RuntimeTarget,
    diagnostic: TargetDiagnostic,
) -> None:
    """Hash opaque identifiers before appending a diagnostic."""
    _ = append_target_diagnostic_once(root, target, diagnostic)


def append_target_diagnostic_once(
    root: Path,
    target: RuntimeTarget,
    diagnostic: TargetDiagnostic,
) -> bool:
    """Hash identifiers, append a unique event, and report whether it was new."""
    return append_diagnostic_once(
        root,
        DiagnosticEvent(
            occurred_at=diagnostic.occurred_at,
            code=diagnostic.code,
            state=diagnostic.state,
            session_hash=_hash(target.session_id),
            child_hash=(
                None
                if diagnostic.target is None or diagnostic.target.target_id is None
                else _hash(diagnostic.target.target_id)
            ),
            elapsed_ms=diagnostic.elapsed_ms,
            event_id=diagnostic.event_id,
        ),
    )


def completion_event_id(target: RuntimeTarget) -> str:
    """Return the stable privacy-safe identity for one parent-turn completion."""
    turn_id = "" if target.parent_turn_id is None else target.parent_turn_id
    return _hash(f"watcher_completed\0{target.session_id}\0{turn_id}")


def lifecycle_event_id(
    code: DiagnosticCode,
    target: RuntimeTarget,
    monitor: MonitorTarget,
) -> str:
    """Return one stable identity for a target's current progress epoch."""
    turn_id = "" if target.parent_turn_id is None else target.parent_turn_id
    target_id = "" if monitor.target_id is None else monitor.target_id
    identity = (
        f"{code.value}\0{target.session_id}\0{turn_id}\0{target_id}\0"
        f"{monitor.generation}\0{monitor.progress_epoch}"
    )
    return _hash(identity)


def lifecycle_event_exists(root: Path, event_id: str) -> bool:
    """Check one lifecycle identity without exposing raw target identifiers."""
    return diagnostic_event_exists(root, event_id)


def notification_failure_event_id(event_id: str) -> str:
    """Derive one stable diagnostic identity for a failed remote delivery."""
    return _hash(f"discord_notification_failed\0{event_id}")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
