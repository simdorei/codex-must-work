"""Typed tool-result and authorization helpers for the MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.control_capability import ControlKeyError, derive_control_capability
from scripts.mcp_protocol import JsonObject, JsonRpcError

if TYPE_CHECKING:
    from scripts.monitor_models import ToolResult
    from scripts.notification_setup import NotificationSetupLaunch
    from scripts.threshold_settings import ThresholdSettingsSnapshot


def tool_success(result: ToolResult) -> JsonObject:
    """Serialize a daemon result without exposing private state."""
    payload: JsonObject = {"session_id": result.session_id, "status": result.status}
    for key, value in (
        ("enabled", result.enabled),
        ("reused", result.reused),
        ("daemon_error", result.daemon_error),
    ):
        if value is not None:
            payload[key] = value
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def tool_error(message: str) -> JsonObject:
    """Serialize one public daemon failure for MCP clients."""
    payload: JsonObject = {"error": message}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }


def notification_setup_success(launch: NotificationSetupLaunch) -> JsonObject:
    """Serialize only the temporary loopback URL and its lifetime."""
    payload: JsonObject = {
        "status": "ready",
        "setup_url": launch.setup_url,
        "expires_in_seconds": launch.expires_in_seconds,
        "restart_recommended_after_save": True,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def threshold_settings_success(result: ThresholdSettingsSnapshot) -> JsonObject:
    """Serialize one secret-free threshold selection."""
    payload: JsonObject = {
        "status": "ready",
        "mode": result.mode.value,
        "warning_after_ms": result.warning_after_ms,
        "critical_after_ms": result.critical_after_ms,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def require_authorized(
    key: bytes,
    arguments: JsonObject,
    request_id: int | str | None,
) -> AuthorizedSession:
    """Derive the private capability inside the trusted MCP process."""
    session_id = arguments.get("session_id")
    if type(session_id) is not str or not session_id:
        raise JsonRpcError(
            -32001,
            "Unauthorized",
            request_id=request_id,
            data={"code": "cmw_unauthorized"},
        )
    try:
        capability = derive_control_capability(key, session_id)
    except ControlKeyError as error:
        raise JsonRpcError(
            -32001,
            "Unauthorized",
            request_id=request_id,
            data={"code": "cmw_unauthorized"},
        ) from error
    return AuthorizedSession(session_id, capability)


@dataclass(frozen=True, slots=True)
class AuthorizedSession:
    """Retain the verified identity needed by authorization-bound controls."""

    session_id: str
    capability: str
