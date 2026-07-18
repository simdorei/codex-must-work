from pathlib import Path

import pytest

from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.state import StateDocument, load_state, save_state
from tests.test_manager_engine_goal import (
    FakeGoalAppServer,
    accept_fake_goal_turn,
    runtime_fixture,
)

_READINESS_FAILED = "readiness failed"


def test_initial_pause_is_restored_when_manager_readiness_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )

    def fail_ready(_root: Path, _path: Path, _pid: int) -> None:
        raise OSError(_READINESS_FAILED)

    monkeypatch.setattr("scripts.manager_engine.mark_manager_ready", fail_ready)

    with pytest.raises(OSError, match="readiness failed"):
        engine.initialize()

    assert client.goal_status == "active"


def test_work_off_before_goal_handoff_restores_goal_and_removes_runtime(
    tmp_path: Path,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()
    values = dict(load_state(root, path).values)
    values["shutdown_requested"] = True
    save_state(root, path, StateDocument(values=values))

    assert engine.tick() is False

    assert client.goal_status == "active"
    assert not path.exists()


def test_restricted_goal_is_not_overwritten_before_interrupt(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()
    assert engine.tick() is True
    values = dict(load_state(root, path).values)
    values["restart_request"] = {
        "request_id": "request-1",
        "turn_id": "turn-goal-1",
        "target_id": None,
        "target_generation": 1,
        "progress_epoch": 0,
    }
    values["restart_claimed"] = False
    save_state(root, path, StateDocument(values=values))
    client.goal_status = "usageLimited"

    assert engine.tick() is False

    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "goal_not_resumable"
    assert client.goal_status == "usageLimited"
    assert "turn/interrupt" not in client.calls


def test_replacement_goal_is_not_mutated_or_interrupted(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()
    assert engine.tick() is True
    values = dict(load_state(root, path).values)
    values["restart_request"] = {
        "request_id": "request-1",
        "turn_id": "turn-goal-1",
        "target_id": None,
        "target_generation": 1,
        "progress_epoch": 0,
    }
    values["restart_claimed"] = False
    save_state(root, path, StateDocument(values=values))
    client.goal_created_at = 11

    assert engine.tick() is False

    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "goal_identity_changed"
    assert "turn/interrupt" not in client.calls


def test_fast_completed_goal_turn_is_adopted_without_resume_timeout(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer(complete_on_resume=True)
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] is None
    assert runtime["handoff_requested"] is True
    assert runtime["manager_error"] is None


def test_manager_failure_keeps_owned_goal_scheduling_paused(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()
    assert engine.tick() is True
    assert client.goal_status == "paused"
    client.pending_server_request = "item/tool/requestUserInput"

    assert engine.tick() is False
    engine.close()

    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "server_request_unhandled"
    assert client.goal_status == "paused"
    assert client.finish_active_turn() == "turn-goal-1"
    assert client.active is None
