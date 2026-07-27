"""Parse authenticated MCP tool arguments into daemon request models."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scripts.state_io import JsonValue

from scripts.daemon_models import SessionId, SessionRequest, StartRequest
from scripts.durations import (
    MAX_THRESHOLD_MS,
    Milliseconds,
    ThresholdOrderError,
    validate_thresholds,
)
from scripts.mcp_protocol import JsonRpcError, JsonRpcId
from scripts.setup import MessagePreset


def parse_start_request(
    values: Mapping[str, JsonValue], request_id: JsonRpcId | None
) -> StartRequest:
    """Parse untrusted MCP activation arguments into the daemon model."""
    session_id = SessionId(_required_text(values, "session_id", request_id))
    transcript = Path(_required_text(values, "transcript_path", request_id))
    warning = Milliseconds(_positive_int(values, "warning_after_ms", 600_000, request_id))
    restart = Milliseconds(_positive_int(values, "restart_after_ms", 1_200_000, request_id))
    try:
        _ = validate_thresholds(warning, restart)
    except ThresholdOrderError as error:
        raise JsonRpcError(
            -32602,
            "Invalid params",
            request_id=request_id,
            data=str(error),
        ) from error
    return StartRequest(
        session_id=session_id,
        transcript_path=transcript,
        warning_after_ms=warning,
        restart_after_ms=restart,
        message_preset=_message_preset(values, request_id),
        auto_restart=_boolean(values, "auto_restart", request_id, default=True),
        goal_companion=_boolean(values, "goal_companion", request_id, default=False),
        observe_only=_boolean(values, "observe_only", request_id, default=False),
        permission_mode=_permission_mode(values, request_id),
    )


def parse_session_request(
    values: Mapping[str, JsonValue], request_id: JsonRpcId | None
) -> SessionRequest:
    """Parse untrusted MCP session arguments into the daemon model."""
    return SessionRequest(SessionId(_required_text(values, "session_id", request_id)))


def _message_preset(values: Mapping[str, JsonValue], request_id: JsonRpcId | None) -> MessagePreset:
    preset_value = values.get("message_preset", MessagePreset.CLEANUP.value)
    if type(preset_value) is not str:
        raise JsonRpcError(
            -32602, "Invalid params", request_id=request_id, data="message_preset_invalid"
        ) from None
    try:
        return MessagePreset(preset_value)
    except ValueError as error:
        raise JsonRpcError(
            -32602, "Invalid params", request_id=request_id, data="message_preset_invalid"
        ) from error


def _required_text(values: Mapping[str, JsonValue], key: str, request_id: JsonRpcId | None) -> str:
    value = values.get(key)
    if type(value) is str and value:
        return value
    raise JsonRpcError(-32602, "Invalid params", request_id=request_id, data=f"{key}_missing")


def _positive_int(
    values: Mapping[str, JsonValue], key: str, default: int, request_id: JsonRpcId | None
) -> int:
    value = values.get(key, default)
    if type(value) is int and 0 < value <= MAX_THRESHOLD_MS:
        return value
    raise JsonRpcError(-32602, "Invalid params", request_id=request_id, data=f"{key}_invalid")


def _boolean(
    values: Mapping[str, JsonValue], key: str, request_id: JsonRpcId | None, *, default: bool
) -> bool:
    value = values.get(key, default)
    if type(value) is bool:
        return value
    raise JsonRpcError(-32602, "Invalid params", request_id=request_id, data=f"{key}_invalid")


def _permission_mode(values: Mapping[str, JsonValue], request_id: JsonRpcId | None) -> str | None:
    value = values.get("permission_mode")
    if value is None:
        return None
    if type(value) is str and value in {
        "default",
        "acceptEdits",
        "plan",
        "dontAsk",
        "bypassPermissions",
    }:
        return value
    raise JsonRpcError(
        -32602, "Invalid params", request_id=request_id, data="permission_mode_invalid"
    )
