"""Validate public-safe resident manager failure reason codes."""

from __future__ import annotations

from typing import Final

from scripts.manager_runtime_values import fail

_ALLOWED: Final = frozenset(
    {
        "active_turn_mismatch",
        "app_server_failed",
        "goal_not_resumable",
        "goal_identity_changed",
        "goal_identity_invalid",
        "goal_identity_missing",
        "goal_handoff_changed",
        "goal_missing",
        "goal_resume_timeout",
        "goal_status_invalid",
        "goal_status_mismatch",
        "goal_turn_source_unverified",
        "interrupt_timeout",
        "restart_turn_not_owned",
        "server_request_unhandled",
        "start_timeout",
        "completed_turn_not_owned",
        "handoff_state_changed",
        "managed_mode_not_enabled",
        "manager_pid_invalid",
        "managed_turn_id_missing",
        "restart_request_changed",
        "restart_request_not_claimed",
        "runtime_name_invalid",
        "trusted_codex_executable_changed",
        "trusted_codex_executable_missing",
        "trusted_codex_home_invalid",
        "unexpected_active_turn",
    }
)


def validate_manager_failure(reason_code: str) -> None:
    """Reject arbitrary error text before it reaches persisted diagnostics."""
    if reason_code not in _ALLOWED:
        fail("manager_failure_reason_invalid")
