"""Validate and compare one exact work-on activation identity."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Final

_MAX_TEXT_CHARS: Final = 65_536


@dataclass(frozen=True, slots=True)
class ActivationIdentity:
    """Bind an authorization to one exact Codex turn and transcript."""

    session_id: str
    turn_id: str
    transcript_path: str


def activation_identity_is_valid(identity: ActivationIdentity) -> bool:
    """Return whether every identity member is nonempty and bounded."""
    values = (identity.session_id, identity.turn_id, identity.transcript_path)
    return all(value and len(value) <= _MAX_TEXT_CHARS for value in values)


def same_activation_identity(
    left: ActivationIdentity,
    right: ActivationIdentity,
) -> bool:
    """Compare every private identity member without early byte disclosure."""
    return (
        hmac.compare_digest(left.session_id, right.session_id)
        and hmac.compare_digest(left.turn_id, right.turn_id)
        and hmac.compare_digest(left.transcript_path, right.transcript_path)
    )
