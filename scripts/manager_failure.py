"""Validate public-safe resident manager failure reason codes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from scripts.diagnostics import DiagnosticCode, DiagnosticEvent, MonitorState, append_diagnostic
from scripts.manager_runtime_values import bump_revision, fail, string_value
from scripts.state import mutate_existing_state

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue

_ALLOWED: Final = frozenset(
    {
        "activation_turn_aborted",
        "activation_turn_superseded",
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
        "interrupt_claim_expired",
        "interrupt_claim_mismatch",
        "turn_failed",
        "turn_status_invalid",
        "turn_interrupted_external",
        "goal_blocked",
        "goal_usage_limited",
        "goal_budget_limited",
        "goal_companion_atomic_update_unavailable",
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


def record_manager_failure(root: Path, path: Path, reason_code: str) -> None:
    """Persist a fixed public-safe failure and clear resident manager readiness."""
    validate_manager_failure(reason_code)

    def update(values: dict[str, JsonValue]) -> str:
        values["manager_ready"] = False
        values["manager_pid"] = None
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
