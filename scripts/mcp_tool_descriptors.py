"""Describe CMW controls and optional local notification setup to MCP clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.state_io import JsonValue


def control_tool_descriptors(*, include_notification_setup: bool = False) -> list[JsonValue]:
    """Expose control schemas and the optional secret-free setup launcher."""
    session: dict[str, JsonValue] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 65_536,
    }
    capability: dict[str, JsonValue] = {
        "type": "string",
        "minLength": 43,
        "maxLength": 43,
    }
    control: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "session_id": session,
            "control_capability": capability,
        },
        "required": ["session_id", "control_capability"],
        "additionalProperties": False,
    }
    start: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "session_id": session,
            "control_capability": capability,
            "transcript_path": session,
            "permission_mode": {"type": ["string", "null"]},
            "auto_restart": {"type": "boolean", "default": True},
            "goal_companion": {"type": "boolean", "default": False},
            "observe_only": {"type": "boolean", "default": False},
            "warning_after_ms": {"type": "integer", "minimum": 1},
            "restart_after_ms": {"type": "integer", "minimum": 1},
            "message_preset": {"type": "string"},
        },
        "required": ["session_id", "control_capability", "transcript_path"],
        "additionalProperties": False,
    }
    definitions = (
        ("cmw.start", "Enable managed CMW for one explicit task.", start),
        ("cmw.stop", "Manually stop CMW and its owned turn.", control),
        ("cmw.status", "Read the current CMW task status.", control),
        ("cmw.complete", "Request verified-completion shutdown.", control),
    )
    descriptors: list[JsonValue] = [
        {"name": name, "description": description, "inputSchema": schema}
        for name, description, schema in definitions
    ]
    if include_notification_setup:
        descriptors.append(
            {
                "name": "cmw.notifications.setup",
                "description": (
                    "Open a short-lived local page for Discord webhook setup. "
                    "Never pass a webhook URL as a tool argument."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            }
        )
    return descriptors
