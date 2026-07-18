from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.state import JsonValue, StateDocument, config_path, runtime_path, save_state

if TYPE_CHECKING:
    from pathlib import Path


def enabled_runtime(
    root: Path,
    *,
    child: bool = False,
    approval_wait: bool = False,
) -> Path:
    save_state(
        root,
        config_path(root),
        StateDocument(values={"message_preset": "continue"}),
    )
    path = runtime_path(root, "session-1")
    children: dict[str, JsonValue] = (
        {
            "child-1": {
                "status": "running",
                "generation": 1,
                "open_tool_count": 0,
                "waiting_for_approval": approval_wait,
                "waiting_for_user": True,
            }
        }
        if child
        else {}
    )
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": "session-1",
                "enabled": True,
                "message_preset": "continue",
                "parent_turn_id": "turn-old",
                "parent_complete": True,
                "parent": None,
                "children": children,
                "unknown_future_key": "preserve-me",
            }
        ),
    )
    return path


def hook_event(name: str, **fields: str) -> str:
    return json.dumps(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "hook_event_name": name,
            **fields,
        }
    )
