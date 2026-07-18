"""Map watcher targets to the fixed sanitized diagnostic schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.diagnostics import (
    DiagnosticCode,
    DiagnosticEvent,
    MonitorState,
    append_diagnostic,
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
    append_diagnostic(
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


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
