"""Typed public models for notification-only CMW controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NewType, final, override

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.durations import Milliseconds

SessionId = NewType("SessionId", str)


@dataclass(frozen=True, slots=True)
class StartRequest:
    """One explicit passive-monitor activation request."""

    session_id: SessionId
    transcript_path: Path
    warning_after_ms: Milliseconds
    critical_after_ms: Milliseconds


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """One parsed session-only control request."""

    session_id: SessionId


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Privacy-safe result returned by a notification control."""

    session_id: SessionId
    status: str
    enabled: bool | None = None
    reused: bool | None = None
    daemon_error: str | None = None


@final
class DaemonServiceError(RuntimeError):
    """Expose one stable public-safe monitoring failure reason."""

    def __init__(self, reason_code: str) -> None:
        """Retain one stable reason while preserving exception mutability."""
        super().__init__(reason_code)
        self.reason_code = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code
