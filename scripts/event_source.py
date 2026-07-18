"""Classify rollout records without retaining conversation content."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonRecord = Mapping[str, JsonValue]


@unique
class EventKind(StrEnum):
    """Privacy-safe event categories consumed by the watcher."""

    SESSION_METADATA = "session_metadata"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_ABORTED = "turn_aborted"
    ITEM = "item"
    DELTA = "delta"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"


@unique
class PauseTransition(StrEnum):
    """Tool-wait transition represented by an observed event."""

    NONE = "none"
    START = "start"
    END = "end"


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    """Allowed event metadata; raw bodies never enter this value."""

    kind: EventKind
    occurred_at: datetime
    session_id: str | None = None
    parent_thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    child_id: str | None = None
    call_id: str | None = None
    terminal: bool = False
    pause: PauseTransition = PauseTransition.NONE
    fallback_tool_result: bool = False


_ITEM_PROGRESS = frozenset(
    {
        "reasoning",
        "agent_message",
    }
)
_TOOL_CALLS = frozenset(
    {
        "function_call",
        "custom_tool_call",
        "tool_search_call",
    }
)
_TOOL_OUTPUTS = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    }
)
_DELTAS = frozenset(
    {
        "agent_message_delta",
        "agent_reasoning_delta",
        "reasoning_delta",
        "sub_agent_activity",
    }
)
_OTHER_PROGRESS = frozenset(
    {
        "context_compacted",
        "thread_goal_updated",
        "thread_rolled_back",
    }
)
_FALLBACK_TOOL_ENDS = frozenset(
    {
        "mcp_tool_call_end",
        "patch_apply_end",
        "web_search_end",
        "image_generation_end",
    }
)


def _text(record: JsonRecord, key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


def _payload(raw: JsonRecord) -> JsonRecord | None:
    value = raw.get("payload")
    return value if isinstance(value, dict) else None


def _timestamp(raw: JsonRecord) -> datetime | None:
    value = _text(raw, "timestamp")
    if value is None or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(f"{value[:-1]}+00:00").astimezone(UTC)
    except ValueError:
        return None


def _is_completed(payload: JsonRecord) -> bool:
    return _text(payload, "status") == "completed" or payload.get("completed") is True


def _response_item_kind(payload: JsonRecord) -> EventKind | None:
    item_type = _text(payload, "type")
    kind: EventKind | None = None
    if item_type in _ITEM_PROGRESS:
        kind = EventKind.ITEM
    elif item_type == "message":
        phase = _text(payload, "phase")
        if _text(payload, "role") == "assistant" and phase in {"commentary", "final_answer"}:
            kind = EventKind.ITEM
    elif item_type in _TOOL_CALLS:
        kind = EventKind.TOOL_STARTED
    elif item_type in _TOOL_OUTPUTS:
        kind = EventKind.TOOL_RESULT
    elif item_type in {"web_search_call", "image_generation_call"}:
        kind = EventKind.TOOL_RESULT if _is_completed(payload) else EventKind.TOOL_STARTED
    return kind


def _event_message_kind(payload: JsonRecord) -> tuple[EventKind, bool] | None:
    event_type = _text(payload, "type")
    classified: tuple[EventKind, bool] | None = None
    if event_type == "task_started":
        classified = EventKind.TURN_STARTED, False
    elif event_type == "task_complete":
        classified = EventKind.TURN_COMPLETED, False
    elif event_type == "turn_aborted":
        classified = EventKind.TURN_ABORTED, False
    elif event_type in _DELTAS:
        classified = EventKind.DELTA, False
    elif event_type in _OTHER_PROGRESS:
        classified = EventKind.ITEM, False
    elif event_type in _FALLBACK_TOOL_ENDS:
        classified = EventKind.TOOL_RESULT, True
    return classified


def _pause_for(kind: EventKind) -> PauseTransition:
    if kind is EventKind.TOOL_STARTED:
        return PauseTransition.START
    if kind is EventKind.TOOL_RESULT:
        return PauseTransition.END
    return PauseTransition.NONE


def _observed(
    kind: EventKind,
    occurred_at: datetime,
    payload: JsonRecord,
    *,
    fallback_tool_result: bool = False,
) -> ObservedEvent:
    return ObservedEvent(
        kind=kind,
        occurred_at=occurred_at,
        session_id=_text(payload, "session_id"),
        parent_thread_id=_text(payload, "parent_thread_id"),
        turn_id=_text(payload, "turn_id"),
        item_id=_text(payload, "item_id") or _text(payload, "id"),
        child_id=_text(payload, "agent_thread_id") or _text(payload, "agent_id"),
        call_id=_text(payload, "call_id"),
        terminal=kind in {EventKind.TURN_COMPLETED, EventKind.TURN_ABORTED},
        pause=_pause_for(kind),
        fallback_tool_result=fallback_tool_result,
    )


def parse_rollout_event(raw: JsonRecord) -> ObservedEvent | None:
    """Return only allowlisted metadata from one decoded rollout record."""
    payload = _payload(raw)
    occurred_at = _timestamp(raw)
    record_type = _text(raw, "type")
    if payload is None or occurred_at is None or record_type is None:
        return None
    if record_type == "session_meta":
        return ObservedEvent(
            kind=EventKind.SESSION_METADATA,
            occurred_at=occurred_at,
            session_id=_text(payload, "id"),
            parent_thread_id=_text(payload, "parent_thread_id"),
        )
    if record_type == "response_item":
        kind = _response_item_kind(payload)
        return None if kind is None else _observed(kind, occurred_at, payload)
    if record_type == "event_msg":
        classified = _event_message_kind(payload)
        if classified is None:
            return None
        kind, fallback = classified
        return _observed(kind, occurred_at, payload, fallback_tool_result=fallback)
    return None
