"""Parse authenticated MCP tool arguments into daemon request models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scripts.state_io import JsonValue

from scripts.durations import (
    MAX_THRESHOLD_MS,
    Milliseconds,
    ThresholdOrderError,
    validate_thresholds,
)
from scripts.mcp_protocol import JsonRpcError, JsonRpcId
from scripts.monitor_models import SessionId, SessionRequest, StartRequest
from scripts.threshold_settings import DEFAULT_CRITICAL_MS, DEFAULT_WARNING_MS

_LEGACY_RESTART_KEYS = frozenset(
    {
        "auto_restart",
        "goal_companion",
        "message_preset",
        "observe_only",
        "permission_mode",
        "restart_after_ms",
    }
)
_START_KEYS: Final = frozenset(
    {
        "session_id",
        "transcript_path",
        "activation_turn_id",
        "warning_after_ms",
        "critical_after_ms",
    }
)
_SESSION_KEYS: Final = frozenset({"session_id"})
MAX_TOOL_TEXT_CHARS: Final = 65_536


@dataclass(frozen=True, slots=True)
class ParsedStartRequest:
    """Keep the daemon request and its one-time activation turn together."""

    request: StartRequest
    activation_turn_id: str


def parse_start_request(
    values: Mapping[str, JsonValue], request_id: JsonRpcId | None
) -> ParsedStartRequest:
    """Parse untrusted MCP activation arguments into the daemon model."""
    if _LEGACY_RESTART_KEYS.intersection(values):
        raise JsonRpcError(
            -32602,
            "Invalid params",
            request_id=request_id,
            data="legacy_restart_option_unsupported",
        )
    _reject_unknown(values, _START_KEYS, request_id)
    session_id = SessionId(_required_text(values, "session_id", request_id))
    transcript = Path(_required_text(values, "transcript_path", request_id))
    warning = Milliseconds(
        _positive_int(values, "warning_after_ms", int(DEFAULT_WARNING_MS), request_id)
    )
    critical = Milliseconds(
        _positive_int(values, "critical_after_ms", int(DEFAULT_CRITICAL_MS), request_id)
    )
    try:
        _ = validate_thresholds(warning, critical)
    except ThresholdOrderError as error:
        raise JsonRpcError(
            -32602,
            "Invalid params",
            request_id=request_id,
            data=str(error),
        ) from error
    return ParsedStartRequest(
        request=StartRequest(
            session_id=session_id,
            transcript_path=transcript,
            warning_after_ms=warning,
            critical_after_ms=critical,
        ),
        activation_turn_id=_required_text(values, "activation_turn_id", request_id),
    )


def parse_session_request(
    values: Mapping[str, JsonValue], request_id: JsonRpcId | None
) -> SessionRequest:
    """Parse untrusted MCP session arguments into the daemon model."""
    _reject_unknown(values, _SESSION_KEYS, request_id)
    return SessionRequest(SessionId(_required_text(values, "session_id", request_id)))


def validate_session_text(
    values: Mapping[str, JsonValue],
    request_id: JsonRpcId | None,
) -> None:
    """Enforce the advertised session-id text bound after authorization."""
    _ = _required_text(values, "session_id", request_id)


def _required_text(values: Mapping[str, JsonValue], key: str, request_id: JsonRpcId | None) -> str:
    value = values.get(key)
    if type(value) is str and 0 < len(value) <= MAX_TOOL_TEXT_CHARS:
        return value
    reason = f"{key}_missing" if not value else f"{key}_invalid"
    raise JsonRpcError(-32602, "Invalid params", request_id=request_id, data=reason)


def _reject_unknown(
    values: Mapping[str, JsonValue],
    allowed: frozenset[str],
    request_id: JsonRpcId | None,
) -> None:
    if not set(values).issubset(allowed):
        raise JsonRpcError(
            -32602,
            "Invalid params",
            request_id=request_id,
            data="unexpected_argument_unsupported",
        )


def _positive_int(
    values: Mapping[str, JsonValue], key: str, default: int, request_id: JsonRpcId | None
) -> int:
    value = values.get(key, default)
    if type(value) is int and 0 < value <= MAX_THRESHOLD_MS:
        return value
    raise JsonRpcError(-32602, "Invalid params", request_id=request_id, data=f"{key}_invalid")
