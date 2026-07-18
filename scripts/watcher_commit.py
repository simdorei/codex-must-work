"""Commit one revalidated rollout batch without event-loss races."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from scripts.state import mutate_existing_state
from scripts.watcher_batch import TargetBatch, commit_target_cursor
from scripts.watcher_models import RuntimeTarget
from scripts.watcher_state import runtime_target_from_values

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.event_source import ObservedEvent
    from scripts.state_io import JsonValue

type TargetProcessor = Callable[
    [RuntimeTarget, dict[str, JsonValue], tuple[ObservedEvent, ...]],
    bool,
]


def commit_runtime_snapshot(
    root: Path,
    snapshot: RuntimeTarget,
    batch: TargetBatch | None,
    processor: TargetProcessor,
) -> bool:
    """Commit runtime changes before its rollout cursor under one state lock."""
    accepted = False

    def process(values: dict[str, JsonValue]) -> bool:
        nonlocal accepted
        if values.get("enabled") is not True:
            return False
        target = runtime_target_from_values(root, snapshot.runtime_file, values)
        if target.revision != snapshot.revision:
            return target.parent_complete or any(not monitor.terminal for monitor in target.targets)
        before = deepcopy(values)
        accepted = True
        active = processor(target, values, () if batch is None else batch.events)
        if values != before:
            values["revision"] = target.revision + 1
        return active

    def commit_batch() -> None:
        if accepted and batch is not None:
            commit_target_cursor(root, snapshot, batch)

    return (
        mutate_existing_state(
            root,
            snapshot.runtime_file,
            process,
            after_commit=commit_batch,
        )
        is True
    )
