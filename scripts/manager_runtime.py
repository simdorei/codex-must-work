"""Persist the resident manager's exact turn ownership handshake."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.diagnostics import DiagnosticCode, DiagnosticEvent, MonitorState, append_diagnostic
from scripts.hook_state import start_managed_parent
from scripts.manager_decision import ManagerView
from scripts.manager_failure import validate_manager_failure
from scripts.manager_runtime_values import (
    bool_value,
    bump_revision,
    fail,
    int_value,
    optional_string,
    require_managed,
    runtime_file,
    string_value,
)
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    load_state,
    mutate_existing_state,
    runtime_path,
)
from scripts.watcher_source import resolve_rollout_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from scripts.state_io import JsonValue


@dataclass(frozen=True, slots=True)
class RestartRequest:
    """Exact watcher evidence authorizing one managed-turn restart."""

    request_id: str
    turn_id: str
    target_id: str | None
    target_generation: int
    progress_epoch: int
    turn_activity_epoch: int


@dataclass(frozen=True, slots=True)
class ManagerRuntime:
    """The validated state consumed by one manager loop iteration."""

    runtime_file: Path
    rollout_file: Path
    session_id: str
    message_preset: str
    executable_sha256: str
    restart_request: RestartRequest | None
    restart_claimed: bool
    view: ManagerView
    manager_ready: bool
    shutdown_requested: bool
    shutdown_interrupt: bool
    manager_error: str | None
    parent_turn_id: str | None


def load_manager_runtime(root: Path, runtime_name: str) -> ManagerRuntime | None:
    """Load one runtime selected by its hashed filename, never a raw thread id."""
    path = runtime_file(root, runtime_name)
    if not path.is_file():
        return None
    values = load_state(root, path).values
    session_id = string_value(values, "session_id", path)
    if runtime_path(root, session_id) != path:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    preset = string_value(values, "message_preset", path)
    if preset not in {"continue", "cleanup"}:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    request = restart_request_from_values(values, path)
    managed = bool_value(values, "managed_mode", path)
    rollout_file = resolve_rollout_path(root, string_value(values, "transcript_path", path))
    goal_companion = values.get("goal_companion", False)
    if type(goal_companion) is not bool:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return ManagerRuntime(
        runtime_file=path,
        rollout_file=rollout_file,
        session_id=session_id,
        message_preset=preset,
        executable_sha256=string_value(values, "executable_sha256", path),
        restart_request=request,
        restart_claimed=bool_value(values, "restart_claimed", path),
        view=ManagerView(
            enabled=bool_value(values, "enabled", path) and managed,
            handoff_requested=bool_value(values, "handoff_requested", path),
            managed_turn_id=optional_string(values, "managed_turn_id", path),
            restart_request_turn_id=None if request is None else request.turn_id,
            goal_companion=goal_companion,
        ),
        manager_ready=bool_value(values, "manager_ready", path),
        shutdown_requested=bool_value(values, "shutdown_requested", path),
        shutdown_interrupt=bool_value(values, "shutdown_interrupt", path),
        manager_error=optional_string(values, "manager_error", path),
        parent_turn_id=optional_string(values, "parent_turn_id", path),
    )


def restart_request_from_values(
    values: Mapping[str, JsonValue],
    path: Path,
) -> RestartRequest | None:
    """Parse the complete restart authorization from persisted state."""
    request = values.get("restart_request")
    if request is None:
        return None
    if not isinstance(request, dict) or "target_id" not in request:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    raw_turn_activity_epoch = request.get("turn_activity_epoch", 0)
    if type(raw_turn_activity_epoch) is not int or raw_turn_activity_epoch < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return RestartRequest(
        request_id=string_value(request, "request_id", path),
        turn_id=string_value(request, "turn_id", path),
        target_id=optional_string(request, "target_id", path),
        target_generation=int_value(request, "target_generation", path, minimum=1),
        progress_epoch=int_value(request, "progress_epoch", path, minimum=0),
        turn_activity_epoch=raw_turn_activity_epoch,
    )


def mark_manager_ready(root: Path, path: Path, pid: int) -> None:
    """Publish readiness only after the resident connection is initialized."""
    if pid < 1:
        fail("manager_pid_invalid")

    def update(values: dict[str, JsonValue]) -> None:
        require_managed(values, path)
        values["manager_ready"] = True
        values["manager_pid"] = pid
        values["manager_error"] = None
        bump_revision(values, path)

    _ = mutate_existing_state(root, path, update)


def mark_manager_stopped(root: Path, path: Path) -> None:
    """Clear the readiness lease when a manager exits."""

    def update(values: dict[str, JsonValue]) -> None:
        values["manager_ready"] = False
        values["manager_pid"] = None
        bump_revision(values, path)

    _ = mutate_existing_state(root, path, update)


def request_manager_startup_cancel(root: Path, path: Path) -> None:
    """Ask a not-yet-ready manager to finish initialization and clean up itself."""

    def update(values: dict[str, JsonValue]) -> None:
        require_managed(values, path)
        if values.get("manager_ready") is True:
            return
        values["shutdown_requested"] = True
        values["shutdown_interrupt"] = False
        values["handoff_requested"] = False
        bump_revision(values, path)

    _ = mutate_existing_state(root, path, update)


def record_turn_started(root: Path, path: Path, turn_id: str) -> None:
    """Commit ownership only after this connection observes turn start."""
    if not turn_id:
        fail("managed_turn_id_missing")

    def update(values: dict[str, JsonValue]) -> None:
        require_managed(values, path)
        if values.get("handoff_requested") is not True or values.get("managed_turn_id") is not None:
            fail("handoff_state_changed")
        values["managed_turn_id"] = turn_id
        values["handoff_requested"] = False
        values["restart_request"] = None
        values["restart_claimed"] = False
        start_managed_parent(values, turn_id, datetime.now(UTC).isoformat(), path)
        bump_revision(values, path)

    _ = mutate_existing_state(root, path, update)


def record_turn_finished(root: Path, path: Path, turn_id: str) -> None:
    """Release an exactly owned completed turn and request the next handoff."""

    def update(values: dict[str, JsonValue]) -> None:
        if values.get("managed_turn_id") != turn_id:
            fail("completed_turn_not_owned")
        _finish_turn(values, path)

    _ = mutate_existing_state(root, path, update)


def record_restart_performed(root: Path, path: Path, turn_id: str) -> None:
    """Acknowledge one exact interrupt and queue its replacement turn."""

    def update(values: dict[str, JsonValue]) -> str:
        request = values.get("restart_request")
        if not isinstance(request, dict) or request.get("turn_id") != turn_id:
            fail("restart_request_changed")
        if values.get("managed_turn_id") != turn_id:
            fail("restart_turn_not_owned")
        if values.get("restart_claimed") is not True:
            fail("restart_request_not_claimed")
        count = values.get("restart_count")
        if type(count) is not int or count < 0:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        values["restart_count"] = count + 1
        session_id = string_value(values, "session_id", path)
        _finish_turn(values, path)
        return session_id

    session_id = mutate_existing_state(root, path, update)
    if session_id is not None:
        append_diagnostic(
            root,
            DiagnosticEvent(
                occurred_at=datetime.now(UTC),
                code=DiagnosticCode.RESTART_PERFORMED,
                state=MonitorState.ACTIVE,
                session_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            ),
        )


def record_manager_failure(root: Path, path: Path, reason_code: str) -> None:
    """Fail closed with a fixed public-safe reason code."""
    validate_manager_failure(reason_code)

    def update(values: dict[str, JsonValue]) -> str:
        values["manager_ready"] = False
        values["manager_error"] = reason_code
        session_id = string_value(values, "session_id", path)
        bump_revision(values, path)
        return session_id

    session_id = mutate_existing_state(root, path, update)
    if session_id is not None:
        append_diagnostic(
            root,
            DiagnosticEvent(
                occurred_at=datetime.now(UTC),
                code=DiagnosticCode.MANAGER_FAILED,
                state=MonitorState.FAILED_CLOSED,
                session_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            ),
        )


def _finish_turn(values: dict[str, JsonValue], path: Path) -> None:
    values["managed_turn_id"] = None
    values["restart_request"] = None
    values["restart_claimed"] = False
    values["handoff_requested"] = True
    bump_revision(values, path)
