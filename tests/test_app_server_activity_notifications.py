from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.app_server_activity import (
    INITIAL_ACTIVITY_SEQUENCE,
    AppServerActivity,
    AppServerActivityKind,
    AppServerActivityStream,
)

if TYPE_CHECKING:
    from scripts.app_server_protocol import JsonObject


def test_progress_observation_excludes_notification_content() -> None:
    # Given
    stream = AppServerActivityStream()
    message: JsonObject = {
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "delta": "private model output",
            "item": {"arguments": "private tool input"},
        },
    }

    # When
    stream.record(message)

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity == AppServerActivity(
        AppServerActivityKind.TURN_PROGRESS,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert "private" not in repr(observation)


def test_response_does_not_emit_progress() -> None:
    # Given
    stream = AppServerActivityStream()

    # When
    stream.record({"id": "request-1", "result": {"private": "value"}})

    # Then
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None


def test_unrelated_notification_does_not_emit_progress() -> None:
    # Given
    stream = AppServerActivityStream()

    # When
    stream.record({"method": "account/updated", "params": {"private": "value"}})

    # Then
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None


@pytest.mark.parametrize(
    "method",
    [
        "thread/goal/updated",
        "thread/goal/cleared",
        "thread/status/changed",
    ],
)
def test_native_goal_notifications_do_not_emit_turn_progress(method: str) -> None:
    # Given
    stream = AppServerActivityStream()
    message: JsonObject = {
        "method": method,
        "params": {
            "threadId": "thread-1",
            "status": "active",
            "tokens_used": 123,
            "time_used_seconds": 4.5,
        },
    }

    # When
    stream.record(message)

    # Then
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None
