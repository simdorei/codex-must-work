from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts.event_source import parse_rollout_event
from scripts.monitor_diagnostics import DiagnosticCode
from scripts.watcher_engine import WatcherEngine
from tests.watcher_fixture import WALL_TIME, diagnostic_codes, state

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state import JsonValue


@pytest.mark.parametrize(
    "event_type",
    [
        "thread_goal_updated",
    ],
)
def test_native_goal_updates_are_not_observable_events(event_type: str) -> None:
    event = parse_rollout_event(
        {
            "timestamp": "2026-07-17T03:04:05Z",
            "type": "event_msg",
            "payload": {
                "type": event_type,
                "thread_id": "thread-1",
                "status": "active",
                "tokens_used": 123,
                "time_used_seconds": 4.5,
            },
        }
    )

    assert event is None


def test_native_goal_update_does_not_rearm_silence_detector(tmp_path: Path) -> None:
    root, rollout, _ = state(tmp_path, children=0, parent=True)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, WALL_TIME) is True

    record = _goal_record("thread_goal_updated")
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")

    assert engine.tick(1.0, WALL_TIME) is True
    assert engine.tick(90.0, WALL_TIME) is True
    assert DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value in diagnostic_codes(root)


def _goal_record(event_type: str) -> dict[str, JsonValue]:
    return {
        "timestamp": "2026-07-17T03:04:05Z",
        "type": "event_msg",
        "payload": {
            "type": event_type,
            "turn_id": "turn-parent",
            "status": "active",
            "tokens_used": 123,
            "time_used_seconds": 4.5,
        },
    }
