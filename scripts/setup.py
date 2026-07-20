"""Fail-closed session activation for Codex Must Work."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import TYPE_CHECKING, Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.activation_error import ActivationError
from scripts.activation_validation import validate_activation_request
from scripts.diagnostics import (
    DiagnosticCode,
    DiagnosticEvent,
    MonitorState,
    append_diagnostic,
)
from scripts.manager_lease import manager_lease_owner
from scripts.private_root import ensure_private_root
from scripts.session_shutdown import defer_session_shutdown
from scripts.state import (
    StateDocument,
    StateError,
    config_path,
    cursor_path,
    runtime_path,
    save_state,
)

if TYPE_CHECKING:
    from scripts.control import CapabilityReport
    from scripts.durations import Milliseconds
    from scripts.state_io import JsonValue

from scripts.state_io import (
    ExclusiveWriteLock,
    ensure_direct_regular_file,
    prepare_parent_directories,
)

_OBSERVE_ONLY_REASONS: Final = frozenset(
    {"same_live_server_attach_unavailable", "auto_restart_controls_unavailable", "ready"}
)

__all__ = ("ActivationError", "validate_activation_request")


@unique
class MessagePreset(StrEnum):
    """Allowlisted user instruction variants."""

    CONTINUE = "continue"
    CLEANUP = "cleanup"


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated activation settings persisted for later sessions."""

    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds
    message_preset: MessagePreset
    auto_restart_requested_by_user: bool


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    """One explicit request to activate the current session."""

    session_id: str
    transcript_path: Path
    settings: Settings
    observe_only: bool
    permission_mode: str | None
    now: datetime
    goal_companion: bool = False


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """Effective actions after conservative capability gating."""

    warning_delivery_active: bool
    effective_auto_restart: bool
    capability_reason: str
    stop_continuation_active: bool


def enable_session(
    root: Path,
    request: ActivationRequest,
    capabilities: CapabilityReport,
) -> ActivationResult:
    """Enable one session only after all requested capabilities are proven."""
    relative_transcript = validate_activation_request(root, request)
    if request.observe_only:
        if capabilities.reason_code not in _OBSERVE_ONLY_REASONS:
            raise ActivationError(reason_code=capabilities.reason_code)
    elif not capabilities.stop_continuation_ready and not capabilities.auto_restart_ready:
        raise ActivationError(reason_code=capabilities.reason_code)

    timestamp = _utc_text(request.now)
    effective_auto_restart = (
        request.settings.auto_restart_requested_by_user
        and capabilities.auto_restart_ready
        and not request.observe_only
    )
    if request.goal_companion and not effective_auto_restart:
        raise ActivationError(reason_code="goal_companion_requires_managed_restart")
    capability_values: dict[str, JsonValue] = {
        "warning_delivery_ready": capabilities.warning_delivery_ready,
        "auto_restart_ready": capabilities.auto_restart_ready,
        "evidence_fingerprint": capabilities.evidence_fingerprint,
        "reason_code": capabilities.reason_code,
    }
    config_values: dict[str, JsonValue] = {
        "warning_after_ms": int(request.settings.warning_after_ms),
        "restart_after_ms": int(request.settings.restart_after_ms),
        "auto_restart_requested_by_user": (request.settings.auto_restart_requested_by_user),
        "message_preset": request.settings.message_preset.value,
        "stale_ttl_ms": 604_800_000,
        "capabilities": capability_values,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    runtime_values: dict[str, JsonValue] = {
        "session_id": request.session_id,
        "enabled": True,
        "observe_only": request.observe_only,
        "permission_mode": request.permission_mode,
        "managed_mode": effective_auto_restart,
        "goal_companion": request.goal_companion,
        "warning_after_ms": int(request.settings.warning_after_ms),
        "restart_after_ms": int(request.settings.restart_after_ms),
        "auto_restart_requested_by_user": request.settings.auto_restart_requested_by_user,
        "message_preset": request.settings.message_preset.value,
        "executable_sha256": capabilities.evidence_fingerprint,
        "manager_ready": False,
        "manager_pid": None,
        "manager_error": None,
        "handoff_requested": False,
        "managed_turn_id": None,
        "restart_request": None,
        "restart_claimed": False,
        "restart_claimed_at": None,
        "restart_count": 0,
        "turn_activity_epoch": 0,
        "shutdown_requested": False,
        "shutdown_interrupt": False,
        "parent_turn_id": None,
        "parent_complete": False,
        "parent": None,
        "revision": 0,
        "completion_event_id": None,
        "transcript_path": relative_transcript,
        "children": {},
    }
    ensure_private_root(root)
    operation = root / "operation"
    prepare_parent_directories(root, operation)
    runtime_file = runtime_path(root, request.session_id)
    with ExclusiveWriteLock(operation):
        ensure_direct_regular_file(root, runtime_file)
        if runtime_file.exists():
            raise ActivationError(reason_code="session_already_enabled")
        if manager_lease_owner(root, runtime_file.name) is not None:
            raise ActivationError(reason_code="session_manager_still_running")
        save_state(
            root,
            runtime_file,
            StateDocument(values=runtime_values),
        )
        try:
            save_state(root, config_path(root), StateDocument(values=config_values))
        except (OSError, StateError):
            _remove_uncommitted_runtime(root, runtime_file)
            raise
    return ActivationResult(
        warning_delivery_active=(capabilities.warning_delivery_ready and not request.observe_only),
        effective_auto_restart=effective_auto_restart,
        capability_reason=capabilities.reason_code,
        stop_continuation_active=(
            capabilities.stop_continuation_ready
            and not request.observe_only
            and not effective_auto_restart
        ),
    )


def disable_session(root: Path, session_id: str) -> None:
    """Remove only this session's runtime and cursor artifacts."""
    if not session_id.strip():
        raise ActivationError(reason_code="session_id_missing")
    if not root.exists():
        return
    ensure_private_root(root)
    operation = root / "operation"
    prepare_parent_directories(root, operation)
    with ExclusiveWriteLock(operation):
        runtime = runtime_path(root, session_id)
        cursor = cursor_path(root, session_id)
        if not runtime.exists():
            cursor.unlink(missing_ok=True)
            return
        with ExclusiveWriteLock(runtime):
            runtime.unlink(missing_ok=True)
            cursor.unlink(missing_ok=True)


def _remove_uncommitted_runtime(root: Path, runtime_file: Path) -> None:
    ensure_direct_regular_file(root, runtime_file)
    with ExclusiveWriteLock(runtime_file):
        ensure_direct_regular_file(root, runtime_file)
        runtime_file.unlink(missing_ok=True)


def complete_session(root: Path, session_id: str, now: datetime) -> None:
    """Record one terminal completion heartbeat and disable this session."""
    if not session_id.strip():
        raise ActivationError(reason_code="session_id_missing")
    runtime = runtime_path(root, session_id)
    if not runtime.is_file():
        return
    if now.utcoffset() is None:
        raise ActivationError(reason_code="timezone_missing")
    ensure_private_root(root)
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()
    event_id = hashlib.sha256(f"work_completed\0{session_id}".encode()).hexdigest()
    append_diagnostic(
        root,
        DiagnosticEvent(
            occurred_at=now,
            code=DiagnosticCode.WATCHER_COMPLETED,
            state=MonitorState.COMPLETED,
            session_hash=session_hash,
            event_id=event_id,
        ),
    )
    disable_session(root, session_id)


def request_verified_completion(root: Path, session_id: str, now: datetime) -> bool:
    """Defer managed completion until Final, or complete an unmanaged session now."""
    if defer_session_shutdown(root, session_id, interrupt_active=False):
        return True
    complete_session(root, session_id, now)
    return False


def request_session_shutdown(
    root: Path,
    session_id: str,
    *,
    interrupt_active: bool,
) -> None:
    """Let an active managed turn finish before removing its runtime."""
    if not defer_session_shutdown(
        root,
        session_id,
        interrupt_active=interrupt_active,
    ):
        disable_session(root, session_id)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ActivationError(reason_code="timezone_missing")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
