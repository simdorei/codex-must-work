"""Non-mutating validation for session activation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from scripts.activation_error import ActivationError
from scripts.durations import validate_thresholds
from scripts.path_identity import UnsupportedLocalPathError, resolve_local_path
from scripts.session_identity import SessionIdentityError, require_bound_rollout
from scripts.state_io import ensure_existing_components_are_direct

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.durations import Milliseconds


class _SettingsView(Protocol):
    @property
    def warning_after_ms(self) -> Milliseconds: ...

    @property
    def restart_after_ms(self) -> Milliseconds: ...


class _ActivationRequestView(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def transcript_path(self) -> Path: ...

    @property
    def settings(self) -> _SettingsView: ...


def validate_activation_request(root: Path, request: _ActivationRequestView) -> str:
    """Validate activation without mutating configuration or runtime state."""
    _ = validate_thresholds(
        request.settings.warning_after_ms,
        request.settings.restart_after_ms,
    )
    try:
        transcript = resolve_local_path(request.transcript_path)
    except (OSError, RuntimeError, UnsupportedLocalPathError) as error:
        raise ActivationError(reason_code="transcript_path_invalid") from error
    relative = _relative_transcript(root, transcript)
    try:
        _ = require_bound_rollout(transcript, request.session_id)
    except SessionIdentityError as error:
        raise ActivationError(reason_code=error.reason_code) from error
    return relative


def _relative_transcript(root: Path, transcript: Path) -> str:
    if not transcript.is_absolute() or not transcript.is_file():
        raise ActivationError(reason_code="transcript_path_invalid")
    try:
        codex_home = root.parent.resolve()
        resolved = transcript.resolve()
        relative = resolved.relative_to(codex_home)
        ensure_existing_components_are_direct(codex_home, resolved)
    except (OSError, RuntimeError, ValueError) as error:
        raise ActivationError(reason_code="transcript_path_outside_codex_home") from error
    return relative.as_posix()
