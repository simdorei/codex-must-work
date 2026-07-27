"""Persist a privacy-safe Goal identity before any Goal status mutation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scripts.goal_control import GoalIdentityFingerprint
from scripts.manager_runtime_values import bump_revision, fail, require_managed
from scripts.state import CorruptReason, CorruptStateError, mutate_existing_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from scripts.state_io import JsonValue

_KEY: Final = "goal_identity_fingerprint"
_SHA256_LENGTH: Final = 64


def parse_goal_identity_fingerprint(
    values: Mapping[str, JsonValue],
    path: Path,
) -> GoalIdentityFingerprint | None:
    """Validate the optional Goal identity captured by a prior daemon instance."""
    value = values.get(_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    thread_id = value.get("thread_id")
    created_at = value.get("created_at")
    objective_sha256 = value.get("objective_sha256")
    token_budget = value.get("token_budget")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or type(created_at) is not int
        or created_at < 0
    ):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    if (
        not isinstance(objective_sha256, str)
        or len(objective_sha256) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in objective_sha256)
    ):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    if token_budget is not None and (type(token_budget) is not int or token_budget < 0):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return GoalIdentityFingerprint(
        thread_id=thread_id,
        created_at=created_at,
        objective_sha256=objective_sha256,
        token_budget=token_budget,
    )


def record_goal_identity_fingerprint(
    root: Path,
    path: Path,
    fingerprint: GoalIdentityFingerprint,
) -> None:
    """Commit one immutable Goal fingerprint before the manager pauses that Goal."""

    def update(values: dict[str, JsonValue]) -> None:
        require_managed(values, path)
        captured = parse_goal_identity_fingerprint(values, path)
        if captured is not None and captured != fingerprint:
            fail("goal_identity_changed")
        values[_KEY] = {
            "thread_id": fingerprint.thread_id,
            "created_at": fingerprint.created_at,
            "objective_sha256": fingerprint.objective_sha256,
            "token_budget": fingerprint.token_budget,
        }
        bump_revision(values, path)

    _ = mutate_existing_state(root, path, update)
