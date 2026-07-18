from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.state import JsonValue, StateDocument, runtime_path, save_state

if TYPE_CHECKING:
    from pathlib import Path

WALL_TIME = datetime(2026, 7, 17, tzinfo=UTC)


def state(
    tmp_path: Path,
    *,
    children: int = 1,
    parent: bool = False,
) -> tuple[Path, Path, Path]:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    rollout = codex_home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.touch()
    save_state(
        root,
        root / "config.json",
        StateDocument(
            values={
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
            }
        ),
    )
    path = runtime_path(root, "session-secret")
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": "session-secret",
                "enabled": True,
                "observe_only": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
                "parent_turn_id": "turn-parent",
                "parent_complete": False,
                "transcript_path": "sessions/rollout.jsonl",
                "parent": child() if parent else None,
                "children": {f"child-{index}": child() for index in range(1, children + 1)},
            }
        ),
    )
    return root, rollout, path


def child() -> dict[str, JsonValue]:
    return {
        "status": "running",
        "generation": 1,
        "open_tool_count": 0,
        "waiting_for_approval": False,
        "waiting_for_user": False,
    }


def append_progress(rollout: Path, child_id: str | None, secret: str = "") -> None:
    payload: dict[str, JsonValue] = {
        "type": "agent_message_delta" if child_id is None else "sub_agent_activity",
        "message": secret,
    }
    if child_id is not None:
        payload["agent_id"] = child_id
    record = {
        "timestamp": "2026-07-18T23:59:59Z",
        "type": "event_msg",
        "payload": payload,
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def append_terminal(rollout: Path, child_id: str) -> None:
    record = {
        "timestamp": "2026-07-18T23:59:59Z",
        "type": "event_msg",
        "payload": {"type": "task_complete", "agent_id": child_id},
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def diagnostic_codes(root: Path) -> list[str]:
    path = root / "logs" / "diagnostic.jsonl"
    return [json.loads(line)["code"] for line in path.read_text(encoding="utf-8").splitlines()]
