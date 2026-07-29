"""Validated immutable targets for notification-only monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.stall_detector import Thresholds


@dataclass(frozen=True, slots=True)
class MonitorTarget:
    """Allowlisted state for one parent or child generation."""

    target_id: str | None
    generation: int
    terminal: bool
    open_tool_count: int
    waiting_for_approval: bool
    waiting_for_user: bool
    progress_epoch: int
    started_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """One explicitly enabled passive monitoring session."""

    session_id: str
    runtime_file: Path
    rollout_file: Path
    parent_turn_id: str | None
    parent_complete: bool
    thresholds: Thresholds
    turn_activity_epoch: int
    revision: int
    parent: MonitorTarget | None
    children: tuple[MonitorTarget, ...]

    @property
    def targets(self) -> tuple[MonitorTarget, ...]:
        """Return the parent first, followed by its children."""
        return self.children if self.parent is None else (self.parent, *self.children)
