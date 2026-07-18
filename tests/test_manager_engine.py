from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.state import StateDocument, load_state, save_state
from tests.manager_fixture import FakeAppServer, arm_restart, manager_runtime_fixture

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


def test_manager_owns_handoff_then_interrupts_and_restarts_exact_turn(tmp_path: Path) -> None:
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
    first = load_state(root, path).values
    assert first["managed_turn_id"] == "turn-1"
    arm_restart(root, path, "turn-1")

    assert engine.tick() is True
    interrupted = load_state(root, path).values
    assert interrupted["managed_turn_id"] is None
    assert interrupted["restart_count"] == 1
    assert engine.tick() is True
    restarted = load_state(root, path).values
    assert restarted["managed_turn_id"] == "turn-2"
    assert client.calls == [
        "initialize",
        "thread/resume",
        "turn/start",
        "turn/interrupt",
        "thread/backgroundTerminals/clean",
        "turn/start",
    ]


def test_manager_cancels_restart_when_rollout_progress_arrives(tmp_path: Path) -> None:
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
    arm_restart(root, path, "turn-1")
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    record = {
        "timestamp": "2026-07-18T23:59:59Z",
        "type": "response_item",
        "payload": {"type": "reasoning", "id": "item-1", "turn_id": "turn-1"},
    }
    with rollout.open("a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps(record) + "\n")

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["managed_turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls


def test_manager_cancels_restart_when_target_generation_changes(tmp_path: Path) -> None:
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
    arm_restart(root, path, "turn-1")
    values: dict[str, JsonValue] = dict(load_state(root, path).values)
    parent = values["parent"]
    assert isinstance(parent, dict)
    values["parent"] = {**parent, "generation": 2}
    save_state(root, path, StateDocument(values=values))

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["managed_turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls


def test_manager_waits_for_final_turn_before_removing_completed_runtime(tmp_path: Path) -> None:
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
    document = load_state(root, path)
    values: dict[str, JsonValue] = dict(document.values)
    values["shutdown_requested"] = True
    save_state(root, path, StateDocument(values=values))

    assert engine.tick() is True
    assert path.exists()
    client.completed.add("turn-1")
    client.active = None

    assert engine.tick() is False
    assert not path.exists()


def test_shutdown_before_first_handoff_removes_runtime_without_starting_turn(
    tmp_path: Path,
) -> None:
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
    values: dict[str, JsonValue] = dict(load_state(root, path).values)
    values["shutdown_requested"] = True
    save_state(root, path, StateDocument(values=values))

    assert engine.tick() is False

    assert not path.exists()
    assert "turn/start" not in client.calls


def test_manual_shutdown_interrupts_exact_owned_turn_before_removal(tmp_path: Path) -> None:
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
    document = load_state(root, path)
    values: dict[str, JsonValue] = dict(document.values)
    values["shutdown_requested"] = True
    values["shutdown_interrupt"] = True
    save_state(root, path, StateDocument(values=values))

    assert engine.tick() is False
    assert "turn/interrupt" in client.calls
    assert not path.exists()
