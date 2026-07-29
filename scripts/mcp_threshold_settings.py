"""Validate and dispatch threshold-setting MCP requests."""

from __future__ import annotations

from typing import assert_never

from scripts.durations import MAX_THRESHOLD_MS, Milliseconds
from scripts.mcp_arguments import validate_session_text
from scripts.mcp_protocol import JsonObject, JsonRpcError
from scripts.mcp_server_tools import threshold_settings_success, tool_error
from scripts.state import StateError
from scripts.threshold_settings import (
    ThresholdSettingsAction,
    ThresholdSettingsError,
    ThresholdSettingsSnapshot,
    ThresholdSettingsStore,
)

_STATE_UNAVAILABLE = "monitoring_state_unavailable"


def call_threshold_settings(
    settings: ThresholdSettingsStore,
    arguments: JsonObject,
    request_id: int | str | None,
) -> JsonObject:
    """Apply one authenticated threshold request."""
    allowed = {
        "session_id",
        "action",
        "warning_after_ms",
        "critical_after_ms",
    }
    if not set(arguments).issubset(allowed):
        raise JsonRpcError(-32602, "Invalid params", request_id=request_id)
    validate_session_text(arguments, request_id)
    action = _threshold_action(arguments, request_id)
    has_thresholds = "warning_after_ms" in arguments or "critical_after_ms" in arguments
    if action is not ThresholdSettingsAction.CUSTOM and has_thresholds:
        raise JsonRpcError(-32602, "Invalid params", request_id=request_id)
    try:
        result = _apply_threshold_action(settings, action, arguments, request_id)
    except ThresholdSettingsError as error:
        return tool_error(error.reason_code)
    except StateError:
        return tool_error(_STATE_UNAVAILABLE)
    return threshold_settings_success(result)


def _threshold_action(
    arguments: JsonObject,
    request_id: int | str | None,
) -> ThresholdSettingsAction:
    action_value = arguments.get("action", ThresholdSettingsAction.SHOW.value)
    if type(action_value) is not str:
        raise JsonRpcError(-32602, "Invalid params", request_id=request_id)
    try:
        return ThresholdSettingsAction(action_value)
    except ValueError as error:
        raise JsonRpcError(-32602, "Invalid params", request_id=request_id) from error


def _apply_threshold_action(
    settings: ThresholdSettingsStore,
    action: ThresholdSettingsAction,
    arguments: JsonObject,
    request_id: int | str | None,
) -> ThresholdSettingsSnapshot:
    match action:
        case ThresholdSettingsAction.SHOW:
            return settings.load()
        case ThresholdSettingsAction.DEFAULT:
            return settings.set_default()
        case ThresholdSettingsAction.RECOMMENDED:
            return settings.set_recommended()
        case ThresholdSettingsAction.CUSTOM:
            warning = _settings_integer(arguments, "warning_after_ms", request_id)
            critical = _settings_integer(arguments, "critical_after_ms", request_id)
            return settings.set_custom(warning, critical)
        case _:
            assert_never(action)


def _settings_integer(
    arguments: JsonObject,
    key: str,
    request_id: int | str | None,
) -> Milliseconds:
    """Read one explicit positive settings duration from untrusted input."""
    value = arguments.get(key)
    if type(value) is int and 0 < value <= MAX_THRESHOLD_MS:
        return Milliseconds(value)
    raise JsonRpcError(
        -32602,
        "Invalid params",
        request_id=request_id,
        data=f"{key}_invalid",
    )
