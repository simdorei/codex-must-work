"""Typed tool-result and authorization helpers for the MCP server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.control_capability import verify_control_capability
from scripts.goal_control import GoalControlError
from scripts.goal_policy import enforce_goal_companion_policy
from scripts.mcp_protocol import JsonObject, JsonRpcError

if TYPE_CHECKING:
    from scripts.daemon_models import ToolResult
    from scripts.notification_setup import NotificationSetupLaunch


def tool_success(result: ToolResult) -> JsonObject:
    """Serialize a daemon result without exposing private state."""
    payload: JsonObject = {"session_id": result.session_id, "status": result.status}
    for key, value in (
        ("enabled", result.enabled),
        ("managed", result.managed),
        ("reused", result.reused),
        ("manager_ready", result.manager_ready),
        ("managed_turn_id", result.managed_turn_id),
        ("shutdown_requested", result.shutdown_requested),
        ("manager_error", result.manager_error),
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


def require_authorized(key: bytes, arguments: JsonObject, request_id: int | str | None) -> None:
    """Reject malformed, cross-session, or invalid bearer capabilities."""
    session_id = arguments.get("session_id")
    capability = arguments.get("control_capability")
    authorized = (
        type(session_id) is str
        and type(capability) is str
        and verify_control_capability(key, session_id, capability)
    )
    if not authorized:
        raise JsonRpcError(
            -32001,
            "Unauthorized",
            request_id=request_id,
            data={"code": "cmw_unauthorized"},
        )


def reject_goal_companion(arguments: JsonObject, request_id: int | str | None) -> None:
    """Reject Goal companion mutation before parsing remaining start arguments."""
    requested = arguments.get("goal_companion", False)
    if type(requested) is not bool:
        return
    try:
        enforce_goal_companion_policy(requested=requested)
    except GoalControlError as error:
        raise JsonRpcError(
            -32602,
            "Invalid params",
            request_id=request_id,
            data={"code": str(error)},
        ) from error
