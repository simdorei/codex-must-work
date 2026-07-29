"""Persist notification-only monitoring sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from scripts.monitor_models import DaemonServiceError, SessionId, StartRequest
from scripts.private_root import ensure_private_root
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    StateDocument,
    cursor_path,
    load_state,
    mutate_existing_state,
    runtime_path,
    save_state,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from scripts.state_io import JsonValue

_ENABLED: Final = "enabled"
_RECONFIGURATION_REQUIRED: Final = "monitoring_reconfiguration_requires_work_off"
_TRANSCRIPT_INVALID: Final = "transcript_path_invalid"


def start_notification_session(
    root: Path,
    request: StartRequest,
    *,
    ensure_root: bool = True,
) -> bool:
    """Create one passive monitor and report whether an exact session was reused."""
    if ensure_root:
        ensure_private_root(root)
    relative_transcript = _relative_transcript(root, request.transcript_path)
    path = runtime_path(root, request.session_id)
    if path.is_file():
        existing_values = load_state(root, path).values
        if _matches(existing_values, request, relative_transcript):
            return True
        raise DaemonServiceError(_RECONFIGURATION_REQUIRED)

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values: dict[str, JsonValue] = {
        "session_id": request.session_id,
        _ENABLED: True,
        "warning_after_ms": int(request.warning_after_ms),
        "critical_after_ms": int(request.critical_after_ms),
        "transcript_path": relative_transcript,
        "parent_turn_id": None,
        "parent_complete": False,
        "parent": _new_monitor(now),
        "children": {},
        "turn_activity_epoch": 0,
        "revision": 0,
        "completion_event_id": None,
    }
    save_state(root, path, StateDocument(values=values))
    return False


def notification_session_active(root: Path, session_id: SessionId) -> bool:
    """Return whether the exact hashed runtime is currently enabled."""
    path = runtime_path(root, session_id)
    return path.is_file() and load_state(root, path).values.get(_ENABLED) is True


def mark_notification_session_complete(root: Path, session_id: SessionId) -> bool:
    """Mark an active monitor complete so the watcher emits its final transition."""
    path = runtime_path(root, session_id)
    if not path.is_file():
        return False

    def complete(values: dict[str, JsonValue]) -> bool:
        if values.get(_ENABLED) is not True:
            return False
        revision = values.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        values["parent_complete"] = True
        values["revision"] = revision + 1
        return True

    return mutate_existing_state(root, path, complete) is True


def remove_notification_session(root: Path, session_id: SessionId) -> None:
    """Remove only one monitor's runtime and rollout cursor."""
    runtime = runtime_path(root, session_id)
    cursor = cursor_path(root, session_id)
    runtime.unlink(missing_ok=True)
    cursor.unlink(missing_ok=True)


def _relative_transcript(root: Path, transcript: Path) -> str:
    codex_home = root.parent.resolve()
    resolved = transcript.resolve()
    if not resolved.is_file() or resolved == codex_home or not resolved.is_relative_to(codex_home):
        raise DaemonServiceError(_TRANSCRIPT_INVALID)
    for candidate in (codex_home, *resolved.parents):
        if candidate == codex_home.parent:
            break
        if candidate.is_symlink() or candidate.is_junction():
            raise DaemonServiceError(_TRANSCRIPT_INVALID)
        if candidate == codex_home:
            break
    return resolved.relative_to(codex_home).as_posix()


def _matches(
    values: Mapping[str, JsonValue],
    request: StartRequest,
    relative_transcript: str,
) -> bool:
    critical = values.get("critical_after_ms")
    if critical is None:
        critical = values.get("restart_after_ms")
    return (
        values.get(_ENABLED) is True
        and values.get("session_id") == request.session_id
        and values.get("transcript_path") == relative_transcript
        and values.get("warning_after_ms") == int(request.warning_after_ms)
        and critical == int(request.critical_after_ms)
    )


def _new_monitor(now: str) -> dict[str, JsonValue]:
    return {
        "status": "running",
        "generation": 1,
        "silence_started_at": now,
        "open_tool_count": 0,
        "waiting_for_approval": False,
        "waiting_for_user": False,
        "progress_epoch": 0,
    }
