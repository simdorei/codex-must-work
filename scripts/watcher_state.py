"""Load the strict state subset required by the watcher."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.silence import Thresholds
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    JsonValue,
    runtime_path,
)
from scripts.watcher_models import MonitorTarget, RuntimeTarget
from scripts.watcher_source import resolve_rollout_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def discover_runtime_files(root: Path) -> tuple[Path, ...]:
    """List runtime candidates without reading outside their write locks."""
    directory = root / "runtime"
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob("*.json")))


def runtime_target_from_values(
    root: Path,
    path: Path,
    values: Mapping[str, JsonValue],
) -> RuntimeTarget:
    """Validate fresh locked runtime values for one target."""
    session_id = _string(values, "session_id", path)
    if runtime_path(root, session_id) != path:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    transcript = _string(values, "transcript_path", path)
    parent_turn = values.get("parent_turn_id")
    if parent_turn is not None and not isinstance(parent_turn, str):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    warning_ms = _integer(values, "warning_after_ms", path, minimum=1)
    restart_ms = _integer(values, "restart_after_ms", path, minimum=1)
    try:
        thresholds = Thresholds(warning=warning_ms / 1000, restart=restart_ms / 1000)
    except ValueError as error:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from error
    return RuntimeTarget(
        session_id=session_id,
        runtime_file=path,
        rollout_file=resolve_rollout_path(root, transcript),
        parent_turn_id=parent_turn,
        parent_complete=_boolean(values, "parent_complete", path),
        observe_only=_boolean(values, "observe_only", path),
        managed_mode=_optional_boolean(values, "managed_mode", path, default=False),
        manager_ready=_optional_boolean(values, "manager_ready", path, default=False),
        managed_turn_id=_optional_string(values, "managed_turn_id", path),
        thresholds=thresholds,
        auto_restart_requested=_boolean(values, "auto_restart_requested_by_user", path),
        turn_activity_epoch=_optional_integer(
            values,
            "turn_activity_epoch",
            path,
            default=0,
        ),
        revision=_optional_integer(values, "revision", path, default=0),
        parent=_target_parent(values, path),
        children=_target_children(values, path),
    )


def claim_completion(
    values: dict[str, JsonValue],
    event_id: str,
    path: Path,
) -> bool:
    """Persist the first completion claim for one parent-turn event."""
    recorded = values.get("completion_event_id")
    if recorded is not None and not isinstance(recorded, str):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    if recorded == event_id:
        return False
    values["completion_event_id"] = event_id
    return True


def mark_target_terminal(
    values: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> bool:
    """Persist one rollout-confirmed monitor terminal transition."""
    raw_children = values.get("children")
    raw_target = values.get("parent") if target_id is None else None
    if target_id is not None:
        if not isinstance(raw_children, dict):
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        raw_target = raw_children.get(target_id)
    if not isinstance(raw_target, dict):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    status = raw_target.get("status")
    if status == "terminal":
        return False
    if status != "running":
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    raw_target["status"] = "terminal"
    if target_id is None:
        values["parent"] = raw_target
    else:
        if not isinstance(raw_children, dict):
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        raw_children[target_id] = raw_target
        values["children"] = raw_children
    return True


def advance_target_progress_epoch(
    values: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> None:
    """Persist one relevant rollout progress event for a monitor generation."""
    raw_children = values.get("children")
    raw_target = values.get("parent") if target_id is None else None
    if target_id is not None and isinstance(raw_children, dict):
        raw_target = raw_children.get(target_id)
    if not isinstance(raw_target, dict):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    epoch = raw_target.get("progress_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    raw_target["progress_epoch"] = epoch + 1
    if target_id is None:
        values["parent"] = raw_target
    elif isinstance(raw_children, dict):
        raw_children[target_id] = raw_target
        values["children"] = raw_children


def mark_parent_complete(values: dict[str, JsonValue], path: Path) -> bool:
    """Persist one rollout-confirmed parent terminal transition."""
    completed = values.get("parent_complete")
    if type(completed) is not bool:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    if completed:
        return False
    values["parent_complete"] = True
    return True


def _target_children(
    values: Mapping[str, JsonValue],
    path: Path,
) -> tuple[MonitorTarget, ...]:
    raw_children = values.get("children")
    if not isinstance(raw_children, dict):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    children: list[MonitorTarget] = []
    for child_id, raw_child in raw_children.items():
        if not child_id or not isinstance(raw_child, dict):
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        children.append(_target_monitor(raw_child, child_id, path))
    return tuple(children)


def _target_parent(values: Mapping[str, JsonValue], path: Path) -> MonitorTarget | None:
    raw_parent = values.get("parent")
    if raw_parent is None:
        return None
    if not isinstance(raw_parent, dict):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return _target_monitor(raw_parent, None, path)


def _target_monitor(
    values: Mapping[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> MonitorTarget:
    status = _string(values, "status", path)
    if status not in {"running", "terminal"}:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return MonitorTarget(
        target_id=target_id,
        generation=_integer(values, "generation", path, minimum=1),
        terminal=status == "terminal",
        open_tool_count=_integer(values, "open_tool_count", path, minimum=0),
        waiting_for_approval=_boolean(values, "waiting_for_approval", path),
        waiting_for_user=_boolean(values, "waiting_for_user", path),
        progress_epoch=_optional_integer(values, "progress_epoch", path, default=0),
        started_at=_optional_datetime(values, "silence_started_at", path),
    )


def _optional_datetime(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
) -> datetime | None:
    raw = values.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError as error:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from error


def _string(values: Mapping[str, JsonValue], key: str, path: Path) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def _boolean(values: Mapping[str, JsonValue], key: str, path: Path) -> bool:
    value = values.get(key)
    if type(value) is not bool:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def _optional_boolean(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
    *,
    default: bool,
) -> bool:
    value = values.get(key, default)
    if type(value) is not bool:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def _optional_string(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
) -> str | None:
    value = values.get(key)
    if value is not None and not isinstance(value, str):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def _optional_integer(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
    *,
    default: int,
) -> int:
    value = values.get(key, default)
    if type(value) is not int or value < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def _integer(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
    *,
    minimum: int,
) -> int:
    value = values.get(key)
    if type(value) is not int or value < minimum:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value
