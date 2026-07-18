"""Bind an activation request to the rollout's canonical thread identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, override
from uuid import UUID

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue

_MAX_METADATA_BYTES = 65_536
_IDENTITY_UNREADABLE = "rollout_identity_unreadable"
_IDENTITY_MISSING = "rollout_identity_missing"
_IDENTITY_INVALID = "rollout_identity_invalid"
_SESSION_MISMATCH = "rollout_session_mismatch"
_SESSION_INVALID = "session_id_invalid"


class _JsonLoader(Protocol):
    def __call__(self, value: str) -> JsonValue: ...


_LOAD_JSON = cast("_JsonLoader", json.loads)


@dataclass(frozen=True, slots=True)
class SessionIdentityError(ValueError):
    """Report an invalid or mismatched rollout identity."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


def require_bound_rollout(transcript: Path, requested_session_id: str) -> str:
    """Return the canonical UUID after matching the first session_meta record."""
    canonical = _canonical_uuid(requested_session_id)
    try:
        with transcript.open("r", encoding="utf-8", newline="") as handle:
            raw = handle.readline(_MAX_METADATA_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise SessionIdentityError(_IDENTITY_UNREADABLE) from error
    if not raw or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise SessionIdentityError(_IDENTITY_MISSING)
    try:
        record = _LOAD_JSON(raw)
    except json.JSONDecodeError as error:
        raise SessionIdentityError(_IDENTITY_INVALID) from error
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise SessionIdentityError(_IDENTITY_MISSING)
    payload = record.get("payload")
    rollout_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(rollout_id, str) or _canonical_uuid(rollout_id) != canonical:
        raise SessionIdentityError(_SESSION_MISMATCH)
    return canonical


def _canonical_uuid(raw: str) -> str:
    try:
        parsed = UUID(raw)
    except (AttributeError, ValueError) as error:
        raise SessionIdentityError(_SESSION_INVALID) from error
    canonical = str(parsed)
    if raw != canonical:
        raise SessionIdentityError(_SESSION_INVALID)
    return canonical
