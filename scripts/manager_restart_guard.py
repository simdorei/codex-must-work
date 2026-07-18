"""Revalidate watcher evidence immediately before an exact-turn interrupt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.manager_runtime import (
    ManagerRuntime,
    RestartRequest,
    restart_request_from_values,
)
from scripts.manager_runtime_values import bump_revision
from scripts.state import mutate_existing_state
from scripts.watcher_batch import read_target_batch
from scripts.watcher_events import event_is_turn_activity
from scripts.watcher_state import runtime_target_from_values

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue
    from scripts.watcher_models import MonitorTarget, RuntimeTarget


def claim_restart_request(root: Path, runtime: ManagerRuntime) -> bool:
    """Atomically claim fresh evidence so the watcher cannot clear it mid-interrupt."""
    if runtime.restart_request is None or runtime.restart_claimed:
        return False

    def claim(values: dict[str, JsonValue]) -> bool:
        if values.get("restart_claimed") is not False:
            return False
        if not _request_is_fresh(root, runtime, values):
            return False
        values["restart_claimed"] = True
        bump_revision(values, runtime.runtime_file)
        return True

    return mutate_existing_state(root, runtime.runtime_file, claim) is True


def restart_request_is_fresh(root: Path, runtime: ManagerRuntime) -> bool:
    """Accept only unchanged ownership, generation, and rollout-cursor evidence."""
    request = runtime.restart_request
    if request is None:
        return False

    def validate(values: dict[str, JsonValue]) -> bool:
        return _request_is_fresh(root, runtime, values)

    return mutate_existing_state(root, runtime.runtime_file, validate) is True


def clear_restart_request(root: Path, runtime: ManagerRuntime) -> None:
    """Clear only the exact request previously loaded by this manager tick."""
    request = runtime.restart_request
    if request is None:
        return

    def clear(values: dict[str, JsonValue]) -> None:
        current = restart_request_from_values(values, runtime.runtime_file)
        if current == request:
            _clear_request(values, runtime.runtime_file)

    _ = mutate_existing_state(root, runtime.runtime_file, clear)


def _ownership_matches(target: RuntimeTarget, request: RestartRequest) -> bool:
    if (
        not target.managed_mode
        or not target.manager_ready
        or target.managed_turn_id != request.turn_id
        or target.parent_turn_id != request.turn_id
        or target.turn_activity_epoch != request.turn_activity_epoch
    ):
        return False
    return any(
        monitor.target_id == request.target_id
        and monitor.generation == request.target_generation
        and monitor.progress_epoch == request.progress_epoch
        and not monitor.terminal
        for monitor in target.targets
    )


def _clear_request(values: dict[str, JsonValue], path: Path) -> None:
    values["restart_request"] = None
    values["restart_claimed"] = False
    bump_revision(values, path)


def _request_is_fresh(
    root: Path,
    runtime: ManagerRuntime,
    values: dict[str, JsonValue],
) -> bool:
    request = runtime.restart_request
    if request is None:
        return False
    target = _validated_target(root, runtime, values, request)
    if target is None:
        return False
    batch = read_target_batch(root, target)
    if batch.previous_cursor is None:
        _clear_request(values, runtime.runtime_file)
        return False
    if _requested_monitor(target, request) is None:
        _clear_request(values, runtime.runtime_file)
        return False
    if any(event_is_turn_activity(event, target) for event in batch.events):
        _clear_request(values, runtime.runtime_file)
        return False
    return True


def _validated_target(
    root: Path,
    runtime: ManagerRuntime,
    values: dict[str, JsonValue],
    request: RestartRequest,
) -> RuntimeTarget | None:
    current = restart_request_from_values(values, runtime.runtime_file)
    if current != request:
        return None
    target = runtime_target_from_values(root, runtime.runtime_file, values)
    if _ownership_matches(target, request):
        return target
    _clear_request(values, runtime.runtime_file)
    return None


def _requested_monitor(
    target: RuntimeTarget,
    request: RestartRequest,
) -> MonitorTarget | None:
    return next(
        (
            candidate
            for candidate in target.targets
            if candidate.target_id == request.target_id
            and candidate.generation == request.target_generation
        ),
        None,
    )
