"""Maintain one whole-turn activity generation for interrupt freshness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.state import CorruptReason, CorruptStateError

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


def turn_activity_epoch(values: dict[str, JsonValue], path: Path) -> int:
    """Return the validated whole-turn activity generation."""
    epoch = values.get("turn_activity_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return epoch


def advance_turn_activity_epoch(values: dict[str, JsonValue], path: Path) -> None:
    """Invalidate restart evidence after any owned turn-tree activity."""
    values["turn_activity_epoch"] = turn_activity_epoch(values, path) + 1


def persist_turn_activity(
    values: dict[str, JsonValue],
    path: Path,
    observed: bool,
) -> bool:
    """Persist one activity generation and preserve its observed flag."""
    if observed:
        advance_turn_activity_epoch(values, path)
    return observed


def advance_monitor_progress_epoch(monitor: dict[str, JsonValue], path: Path) -> None:
    """Advance one monitor generation after hook-observed progress."""
    epoch = monitor.get("progress_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    monitor["progress_epoch"] = epoch + 1
