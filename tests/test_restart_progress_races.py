from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.hook_event import process_hook
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_restart_guard import claim_restart_request
from scripts.state import StateDocument, load_state, save_state
from scripts.watcher_engine import WatcherEngine
from tests.manager_fixture import FakeAppServer, arm_restart, manager_runtime_fixture
from tests.watcher_fixture import append_progress

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.manager_runtime import ManagerRuntime
    from scripts.state_io import JsonValue


def _managed_turn(tmp_path: Path) -> tuple[Path, Path, ManagerEngine, FakeAppServer]:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()
    assert engine.tick() is True
    return root, path, engine, client


def test_non_goal_interrupt_rechecks_progress_consumed_after_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path, engine, client = _managed_turn(tmp_path)
    watcher = WatcherEngine(root)
    wall_time = datetime(2026, 7, 18, tzinfo=UTC)
    _ = watcher.tick(0.0, wall_time)
    _ = watcher.tick(91.0, wall_time)
    _ = watcher.tick(301.0, wall_time)
    rollout = tmp_path / "sessions" / "rollout.jsonl"

    def claim_then_progress(claim_root: Path, runtime: ManagerRuntime) -> bool:
        claimed = claim_restart_request(claim_root, runtime)
        append_progress(rollout, None)
        _ = watcher.tick(302.0, wall_time)
        return claimed

    monkeypatch.setattr("scripts.manager_interrupt.claim_restart_request", claim_then_progress)

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["restart_claimed"] is False
    assert runtime["restart_count"] == 0
    assert runtime["managed_turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls


def test_irrelevant_rollout_event_does_not_delete_queued_restart(tmp_path: Path) -> None:
    root, path, _engine, _client = _managed_turn(tmp_path)
    watcher = WatcherEngine(root)
    wall_time = datetime(2026, 7, 18, tzinfo=UTC)
    _ = watcher.tick(0.0, wall_time)
    _ = watcher.tick(91.0, wall_time)
    _ = watcher.tick(301.0, wall_time)
    before = load_state(root, path).values["restart_request"]
    assert before is not None
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    record = {
        "timestamp": "2026-07-18T00:00:02Z",
        "type": "session_meta",
        "payload": {"id": "thread-1"},
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")

    _ = watcher.tick(302.0, wall_time)
    after = load_state(root, path).values["restart_request"]

    assert after == before


def test_manager_guard_ignores_unread_irrelevant_event(tmp_path: Path) -> None:
    root, path, engine, client = _managed_turn(tmp_path)
    arm_restart(root, path, "turn-1")
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    record = {
        "timestamp": "2026-07-18T23:59:59Z",
        "type": "session_meta",
        "payload": {"id": "thread-1"},
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_count"] == 1
    assert runtime["managed_turn_id"] is None
    assert "turn/interrupt" in client.calls


def test_unread_child_terminal_cancels_whole_turn_restart(tmp_path: Path) -> None:
    root, path, engine, client = _managed_turn(tmp_path)
    values: dict[str, JsonValue] = dict(load_state(root, path).values)
    values["children"] = {
        "child-1": {
            "status": "running",
            "generation": 1,
            "open_tool_count": 0,
            "waiting_for_approval": False,
            "waiting_for_user": False,
            "progress_epoch": 0,
            "silence_started_at": "2026-07-18T00:00:01+00:00",
        }
    }
    values["restart_request"] = {
        "request_id": "request-child",
        "turn_id": "turn-1",
        "target_id": "child-1",
        "target_generation": 1,
        "progress_epoch": 0,
    }
    values["restart_claimed"] = False
    save_state(root, path, StateDocument(values=values))
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    record = {
        "timestamp": "2026-07-18T23:59:59Z",
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "agent_id": "child-1"},
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["restart_count"] == 0
    assert runtime["managed_turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls


def test_subagent_start_invalidates_queued_whole_turn_restart(tmp_path: Path) -> None:
    root, path, engine, client = _managed_turn(tmp_path)
    arm_restart(root, path, "turn-1")

    _ = process_hook(
        json.dumps(
            {
                "session_id": "thread-1",
                "hook_event_name": "SubagentStart",
                "agent_id": "child-1",
            }
        ),
        root=root,
    )
    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["restart_count"] == 0
    assert runtime["managed_turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls
