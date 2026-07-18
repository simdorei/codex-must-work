from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.event_source import (
    EventKind,
    JsonRecord,
    JsonValue,
    PauseTransition,
    parse_rollout_event,
)

_AT = datetime(2026, 7, 17, 3, 4, 5, tzinfo=UTC)


def rollout(record_type: str, payload: dict[str, JsonValue]) -> JsonRecord:
    return {
        "timestamp": "2026-07-17T03:04:05Z",
        "type": record_type,
        "payload": payload,
    }


def test_only_allowlisted_records_are_progress() -> None:
    assert parse_rollout_event(rollout("unknown", {"type": "reasoning"})) is None
    assert parse_rollout_event(rollout("event_msg", {"type": "unknown"})) is None
    assert (
        parse_rollout_event(
            rollout(
                "response_item",
                {"type": "message", "role": "user", "phase": "commentary"},
            )
        )
        is None
    )
    assert (
        parse_rollout_event(
            {
                "timestamp": "not-a-timestamp",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        )
        is None
    )


def test_private_bodies_never_enter_observed_event() -> None:
    private_body = "do-not-retain-this-private-body"
    event = parse_rollout_event(
        rollout(
            "response_item",
            {
                "type": "reasoning",
                "id": "item-7",
                "turn_id": "turn-3",
                "content": private_body,
                "arguments": private_body,
                "output": private_body,
                "message": private_body,
                "text": private_body,
            },
        )
    )

    assert event is not None
    assert event.kind is EventKind.ITEM
    assert event.occurred_at == _AT
    assert event.item_id == "item-7"
    assert event.turn_id == "turn-3"
    assert private_body not in repr(event)


@pytest.mark.parametrize(
    ("event_type", "kind", "terminal"),
    [
        ("task_started", EventKind.TURN_STARTED, False),
        ("task_complete", EventKind.TURN_COMPLETED, True),
        ("turn_aborted", EventKind.TURN_ABORTED, True),
    ],
)
def test_turn_lifecycle(event_type: str, kind: EventKind, terminal: bool) -> None:
    event = parse_rollout_event(rollout("event_msg", {"type": event_type, "turn_id": "turn-9"}))

    assert event is not None
    assert event.kind is kind
    assert event.turn_id == "turn-9"
    assert event.terminal is terminal


@pytest.mark.parametrize(
    ("payload", "kind", "pause", "fallback"),
    [
        (
            {"type": "function_call", "call_id": "call-1"},
            EventKind.TOOL_STARTED,
            PauseTransition.START,
            False,
        ),
        (
            {"type": "custom_tool_call_output", "call_id": "call-1"},
            EventKind.TOOL_RESULT,
            PauseTransition.END,
            False,
        ),
        (
            {"type": "web_search_call", "id": "search-1", "status": "completed"},
            EventKind.TOOL_RESULT,
            PauseTransition.END,
            False,
        ),
    ],
)
def test_response_tool_progress(
    payload: dict[str, JsonValue],
    kind: EventKind,
    pause: PauseTransition,
    fallback: bool,
) -> None:
    event = parse_rollout_event(rollout("response_item", payload))

    assert event is not None
    assert event.kind is kind
    assert event.pause is pause
    assert event.fallback_tool_result is fallback


def test_fallback_tool_end_is_marked() -> None:
    event = parse_rollout_event(
        rollout("event_msg", {"type": "mcp_tool_call_end", "call_id": "call-8"})
    )

    assert event is not None
    assert event.kind is EventKind.TOOL_RESULT
    assert event.pause is PauseTransition.END
    assert event.fallback_tool_result is True


@pytest.mark.parametrize(
    ("payload", "expected_child"),
    [
        (
            {
                "type": "sub_agent_activity",
                "agent_thread_id": "child-thread",
                "agent_id": "child-agent",
            },
            "child-thread",
        ),
        ({"type": "sub_agent_activity", "agent_id": "child-agent"}, "child-agent"),
    ],
)
def test_child_mapping(payload: dict[str, JsonValue], expected_child: str) -> None:
    event = parse_rollout_event(rollout("event_msg", payload))

    assert event is not None
    assert event.kind is EventKind.DELTA
    assert event.child_id == expected_child


def test_session_metadata_maps_parent_without_content() -> None:
    event = parse_rollout_event(
        rollout(
            "session_meta",
            {"id": "session-1", "parent_thread_id": "parent-1", "content": "ignored"},
        )
    )

    assert event is not None
    assert event.kind is EventKind.SESSION_METADATA
    assert event.session_id == "session-1"
    assert event.parent_thread_id == "parent-1"
