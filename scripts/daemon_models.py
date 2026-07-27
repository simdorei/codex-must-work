"""Typed control models shared by the CMW daemon and MCP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NewType, final, override

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.durations import Milliseconds
    from scripts.setup import MessagePreset

SessionId = NewType("SessionId", str)


@dataclass(frozen=True, slots=True)
class StartRequest:
    """One parsed explicit activation request from the MCP boundary."""

    session_id: SessionId
    transcript_path: Path
    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds
    message_preset: MessagePreset
    auto_restart: bool
    goal_companion: bool
    observe_only: bool
    permission_mode: str | None


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """One parsed session-only control request from the MCP boundary."""

    session_id: SessionId


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Privacy-safe result returned by every daemon control operation."""

    session_id: SessionId
    status: str
    enabled: bool | None = None
    managed: bool | None = None
    reused: bool | None = None
    manager_ready: bool | None = None
    managed_turn_id: str | None = None
    shutdown_requested: bool | None = None
    manager_error: str | None = None
    daemon_error: str | None = None


@final
class DaemonServiceError(RuntimeError):
    """Expose one stable public-safe daemon failure reason."""

    def __init__(self, reason_code: str) -> None:
        """Retain one stable reason while preserving Python exception mutability."""
        super().__init__(reason_code)
        self.reason_code = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code
