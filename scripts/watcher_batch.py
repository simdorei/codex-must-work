"""Read rollout batches without holding a session state lock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.event_source import ObservedEvent, parse_rollout_event
from scripts.watcher_source import (
    RolloutCursor,
    initial_cursor,
    load_cursor,
    read_new_records,
    save_cursor,
)

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.watcher_models import RuntimeTarget


@dataclass(frozen=True, slots=True)
class TargetBatch:
    """One uncommitted cursor transition and its sanitized events."""

    previous_cursor: RolloutCursor | None
    next_cursor: RolloutCursor
    events: tuple[ObservedEvent, ...]


def read_target_batch(root: Path, target: RuntimeTarget) -> TargetBatch:
    """Read one bounded batch without changing persisted state."""
    cursor = load_cursor(root, target.session_id)
    if cursor is None:
        return TargetBatch(None, initial_cursor(target.rollout_file), ())
    batch = read_new_records(target.rollout_file, cursor)
    events = tuple(
        event for record in batch.records if (event := parse_rollout_event(record)) is not None
    )
    return TargetBatch(cursor, batch.cursor, events)


def commit_target_cursor(root: Path, target: RuntimeTarget, batch: TargetBatch) -> None:
    """Persist a cursor only after the matching runtime snapshot is revalidated."""
    if batch.previous_cursor != batch.next_cursor:
        save_cursor(root, target.session_id, batch.next_cursor)
