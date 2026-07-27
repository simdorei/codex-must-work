"""Adapt typed daemon activation input to existing persisted state models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.control import CapabilityReport
from scripts.daemon_models import DaemonServiceError
from scripts.manager_lease import manager_lease_owner
from scripts.manager_runtime import load_manager_runtime
from scripts.setup import ActivationRequest, Settings
from scripts.state import load_state

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.daemon_models import StartRequest


def activation_request(request: StartRequest) -> ActivationRequest:
    """Build the validated activation model consumed by existing setup code."""
    return ActivationRequest(
        session_id=request.session_id,
        transcript_path=request.transcript_path,
        settings=Settings(
            warning_after_ms=request.warning_after_ms,
            restart_after_ms=request.restart_after_ms,
            message_preset=request.message_preset,
            auto_restart_requested_by_user=request.auto_restart,
        ),
        observe_only=request.observe_only,
        permission_mode=request.permission_mode,
        now=datetime.now(UTC),
        goal_companion=request.goal_companion,
    )


def daemon_capabilities(fingerprint: str, *, managed: bool) -> CapabilityReport:
    """Authorize managed restart only for the in-process daemon owner."""
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=managed,
        reason_code="ready",
        evidence_fingerprint=fingerprint,
        stop_continuation_ready=False,
    )


def validate_daemon_start(request: StartRequest) -> None:
    """Reject daemon modes that cannot preserve exact-turn ownership."""
    managed = request.auto_restart and not request.observe_only
    if not managed and not request.observe_only:
        reason = "daemon_requires_auto_restart_or_observe_only"
        raise DaemonServiceError(reason)
    if managed and request.permission_mode not in {"dontAsk", "bypassPermissions"}:
        reason = "managed_mode_requires_approval_free_permission"
        raise DaemonServiceError(reason)


def require_recoverable_activation(
    root: Path,
    path: Path,
    request: ActivationRequest,
) -> None:
    """Require matching persisted settings and no competing live manager."""
    require_matching_activation(root, path, request)
    if manager_lease_owner(root, path.name) is not None:
        reason = "session_manager_still_running"
        raise DaemonServiceError(reason)
    runtime = load_manager_runtime(root, path.name)
    if runtime is not None and runtime.manager_error is not None:
        raise DaemonServiceError(runtime.manager_error)


def require_matching_activation(
    root: Path,
    path: Path,
    request: ActivationRequest,
) -> None:
    """Reject reconfiguration of one already enabled persisted task."""
    values = load_state(root, path).values
    settings = request.settings
    runtime = load_manager_runtime(root, path.name)
    rollout_matches = runtime is None or runtime.rollout_file == request.transcript_path.resolve()
    matches = (
        rollout_matches
        and values.get("session_id") == request.session_id
        and values.get("warning_after_ms") == int(settings.warning_after_ms)
        and values.get("restart_after_ms") == int(settings.restart_after_ms)
        and values.get("message_preset") == settings.message_preset.value
        and values.get("auto_restart_requested_by_user") is settings.auto_restart_requested_by_user
        and values.get("observe_only") is request.observe_only
        and values.get("permission_mode") == request.permission_mode
        and values.get("goal_companion") is request.goal_companion
    )
    if not matches:
        reason = "managed_reconfiguration_requires_work_off"
        raise DaemonServiceError(reason)
