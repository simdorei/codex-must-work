"""Observation clocks shared across one watcher tick."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.silence import latest_progress_at

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from scripts.silence import SilenceState

type DetectorKey = tuple[str, str | None, int]


@dataclass(frozen=True, slots=True)
class TickContext:
    """Bind one monotonic detector time to its diagnostic wall clock."""

    now: float
    wall_time: datetime


def restart_eligible_target_count(
    states: Iterable[SilenceState],
    now: float,
    restart_after: float,
) -> int:
    """Allow a whole-turn interrupt only when its sole live target is stale."""
    snapshots = tuple(states)
    if any(now - latest_progress_at(state) < restart_after for state in snapshots):
        return 0
    return sum(not state.waits.terminal for state in snapshots)
