"""Defer managed runtime deletion until its owned turn completes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.state import (
    CorruptReason,
    CorruptStateError,
    mutate_existing_state,
    runtime_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


def defer_session_shutdown(
    root: Path,
    session_id: str,
    *,
    interrupt_active: bool,
) -> bool:
    """Return true after persisting a managed active-turn shutdown request."""
    path = runtime_path(root, session_id)
    if not path.is_file():
        return False

    def request(values: dict[str, JsonValue]) -> bool:
        managed_turn = values.get("managed_turn_id")
        if (
            values.get("managed_mode") is not True
            or values.get("manager_ready") is not True
            or not isinstance(managed_turn, str)
        ):
            return False
        values["shutdown_requested"] = True
        values["shutdown_interrupt"] = interrupt_active
        values["handoff_requested"] = False
        values["restart_request"] = None
        values["restart_claimed"] = False
        values["restart_claimed_at"] = None
        revision = values.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        values["revision"] = revision + 1
        return True

    return mutate_existing_state(root, path, request) is True
