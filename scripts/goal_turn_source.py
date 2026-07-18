"""Prove that one observed turn came from Codex's native Goal continuation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, final

from scripts.goal_control import GoalControlError
from scripts.watcher_source import read_new_records

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.state_io import JsonValue
    from scripts.watcher_source import RolloutCursor

_GOAL_CONTEXT_START: Final = '<codex_internal_context source="goal">'
_GOAL_CONTEXT_END: Final = "</codex_internal_context>"
_SOURCE_UNVERIFIED: Final = "goal_turn_source_unverified"


def wait_for_native_goal_turn(
    rollout: Path,
    cursor: RolloutCursor,
    turn_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    """Accept only a canonical Goal-source user fragment for the exact turn."""
    deadline = time.monotonic() + timeout_seconds
    current = cursor
    evidence = _GoalTurnEvidence(turn_id)
    while True:
        batch = read_new_records(rollout, current)
        current = batch.cursor
        for record in batch.records:
            verdict = evidence.observe(record)
            if verdict is not None:
                return verdict
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not batch.records:
            time.sleep(min(remaining, 0.05))


def require_native_goal_turn(
    verifier: Callable[[Path, RolloutCursor, str], bool],
    rollout: Path,
    cursor: RolloutCursor,
    turn_id: str,
) -> None:
    """Fail closed when canonical Goal provenance cannot be proved."""
    try:
        verified = verifier(rollout, cursor, turn_id)
    except (OSError, ValueError) as error:
        raise GoalControlError(_SOURCE_UNVERIFIED) from error
    if not verified:
        raise GoalControlError(_SOURCE_UNVERIFIED)


@final
class _GoalTurnEvidence:
    """Track one exact turn until visible input or native model execution proves its source."""

    __slots__ = ("in_target", "marker_seen", "turn_id")

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.in_target = False
        self.marker_seen = False

    def observe(self, record: dict[str, JsonValue]) -> bool | None:
        """Reject visible user input; accept only an internal marker followed by execution."""
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if record.get("type") == "event_msg":
            return self._event_verdict(payload)
        if not self.in_target:
            return None
        if _is_goal_marker(payload, self.turn_id):
            self.marker_seen = True
            return None
        if self.marker_seen and _is_model_execution(payload, self.turn_id):
            return True
        return None

    def _event_verdict(self, payload: dict[str, JsonValue]) -> bool | None:
        event_type = payload.get("type")
        if event_type == "task_started":
            started_turn = payload.get("turn_id")
            if started_turn == self.turn_id:
                self.in_target = True
                self.marker_seen = False
            return None if started_turn == self.turn_id else (False if self.in_target else None)
        if not self.in_target:
            return None
        if event_type == "user_message":
            return False
        if (
            event_type in {"task_complete", "turn_aborted"}
            and payload.get("turn_id") == self.turn_id
        ):
            return False
        if event_type == "agent_message" and self.marker_seen:
            return True
        return None


def _is_goal_marker(payload: dict[str, JsonValue], turn_id: str) -> bool:
    if payload.get("type") != "message":
        return False
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if (
        not isinstance(metadata, dict)
        or metadata.get("turn_id") != turn_id
        or payload.get("role") != "user"
    ):
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    texts = (
        item.get("text")
        for item in content
        if isinstance(item, dict) and item.get("type") == "input_text"
    )
    return any(
        isinstance(text, str)
        and text.lstrip().startswith(_GOAL_CONTEXT_START)
        and text.rstrip().endswith(_GOAL_CONTEXT_END)
        for text in texts
    )


def _is_model_execution(payload: dict[str, JsonValue], turn_id: str) -> bool:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict) or metadata.get("turn_id") != turn_id:
        return False
    payload_type = payload.get("type")
    if payload_type == "message":
        return payload.get("role") == "assistant"
    return payload_type in {
        "reasoning",
        "custom_tool_call",
        "function_call",
        "local_shell_call",
    }
