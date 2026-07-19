"""Validate safe reuse of one already-running resident manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.manager_launch import active_manager
from scripts.private_root import ensure_private_root
from scripts.setup import ActivationError, validate_activation_request
from scripts.state import mutate_existing_state

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.manager_runtime import ManagerRuntime
    from scripts.setup import ActivationRequest
    from scripts.state_io import JsonValue


def reuse_ready_manager(root: Path, runtime_file: Path, request: ActivationRequest) -> bool:
    """Reuse only a healthy PID-bound manager with identical effective settings."""
    ensure_private_root(root)
    relative_transcript = validate_activation_request(root, request)
    expected_rollout = (root.parent / relative_transcript).resolve()

    def inspect(values: dict[str, JsonValue]) -> bool:
        manager = active_manager(root, runtime_file)
        if manager is None or not _lifecycle_matches(values, manager):
            return False
        if not _configuration_matches(values, manager, request, expected_rollout):
            raise ActivationError(reason_code="managed_reconfiguration_requires_work_off")
        return True

    return mutate_existing_state(root, runtime_file, inspect) is True


def _lifecycle_matches(values: dict[str, JsonValue], manager: ManagerRuntime) -> bool:
    return (
        values.get("enabled") is True
        and values.get("managed_mode") is True
        and values.get("manager_ready") is True
        and values.get("manager_pid") == manager.manager_pid
        and values.get("shutdown_requested") is False
        and values.get("manager_error") is None
    )


def _configuration_matches(
    values: dict[str, JsonValue],
    manager: ManagerRuntime,
    request: ActivationRequest,
    expected_rollout: Path,
) -> bool:
    settings = request.settings
    return (
        manager.rollout_file == expected_rollout
        and values.get("warning_after_ms") == int(settings.warning_after_ms)
        and values.get("restart_after_ms") == int(settings.restart_after_ms)
        and values.get("message_preset") == settings.message_preset.value
        and values.get("auto_restart_requested_by_user") is settings.auto_restart_requested_by_user
        and values.get("observe_only") is request.observe_only
        and values.get("permission_mode") == request.permission_mode
        and values.get("goal_companion") is request.goal_companion
    )
