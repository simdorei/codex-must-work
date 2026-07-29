"""Observation clocks shared across one watcher tick."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

type DetectorKey = tuple[str, str | None, int]


@dataclass(frozen=True, slots=True)
class TickContext:
    """Bind one monotonic detector time to its diagnostic wall clock."""

    now: float
    wall_time: datetime
