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
    control: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "session_id": session,
        },
        "required": ["session_id"],
        "additionalProperties": False,
    }
    start: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "session_id": session,
            "transcript_path": session,
            "activation_turn_id": session,
            "warning_after_ms": {"type": "integer", "minimum": 1, "default": 300_000},
            "critical_after_ms": {"type": "integer", "minimum": 1, "default": 600_000},
        },
        "required": [
            "session_id",
            "transcript_path",
            "activation_turn_id",
        ],
        "additionalProperties": False,
    }
    definitions = (
        ("cmw.work_on", "Monitor one explicit task for lifecycle notifications.", start),
        ("cmw.stop", "Stop monitoring one explicit task.", control),
        ("cmw.status", "Read the current CMW task status.", control),
        ("cmw.complete", "Record completion and stop monitoring.", control),
    )
    descriptors: list[JsonValue] = [
        {"name": name, "description": description, "inputSchema": schema}
        for name, description, schema in definitions
    ]
    descriptors.append(
        {
            "name": "cmw.settings",
            "description": (
                "Show or select default, recommended, or custom 병목 의심 and 심각 정체 thresholds."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": session,
                    "action": {
                        "type": "string",
                        "enum": ["show", "default", "recommended", "custom"],
                        "default": "show",
                    },
                    "warning_after_ms": {"type": "integer", "minimum": 1},
                    "critical_after_ms": {"type": "integer", "minimum": 1},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        }
    )
    if include_notification_setup:
        descriptors.append(
            {
                "name": "cmw.notifications.setup",
                "description": (
                    "Open a short-lived local page for Discord webhook setup. "
                    "Never pass a webhook URL as a tool argument."
                ),
                "inputSchema": control,
            }
        )
    return descriptors
