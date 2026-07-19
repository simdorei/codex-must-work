from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.app_server_protocol import TurnOutcome, decode_object
from scripts.diagnostics import DiagnosticCode
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_restart_guard import claim_restart_request
from scripts.manager_runtime import load_manager_runtime
from scripts.state import StateDocument, load_state, save_state
from tests.manager_fixture import arm_restart
from tests.test_manager_engine_goal import (
    FakeGoalAppServer,
    accept_fake_goal_turn,
    runtime_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path


def _engine(root: Path, path: Path, client: FakeGoalAppServer) -> ManagerEngine:
    return ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )


def _completion_count(root: Path) -> int:
    diagnostic = root / "logs" / "diagnostic.jsonl"
    if not diagnostic.exists():
        return 0
    count = 0
    for line in diagnostic.read_text(encoding="utf-8").splitlines():
        event = decode_object(line)
        if event is not None and event.get("code") == DiagnosticCode.WATCHER_COMPLETED.value:
            count += 1
    return count


def test_completed_owned_turn_with_complete_goal_records_one_verified_shutdown(
    tmp_path: Path,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    client.goal_status = "complete"
    _ = client.finish_active_turn()

    assert engine.tick() is False
    assert not path.exists()
    assert _completion_count(root) == 1
    assert engine.tick() is False
    assert _completion_count(root) == 1


def test_external_interruption_of_owned_goal_turn_fails_closed(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    _ = client.finish_active_turn(TurnOutcome.INTERRUPTED)

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "turn_interrupted_external"
    assert runtime["handoff_requested"] is False
    assert _completion_count(root) == 0


def test_failed_owned_goal_turn_fails_closed_without_retry(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    _ = client.finish_active_turn(TurnOutcome.FAILED)

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "turn_failed"
    assert runtime["handoff_requested"] is False
    assert _completion_count(root) == 0


@pytest.mark.parametrize("outcome", [TurnOutcome.IN_PROGRESS, TurnOutcome.INVALID])
def test_impossible_terminal_status_fails_closed(
    tmp_path: Path,
    outcome: TurnOutcome,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    _ = client.finish_active_turn(outcome)

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "turn_status_invalid"
    assert runtime["handoff_requested"] is False


def test_active_goal_is_paused_and_continues_after_successful_turn(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    client.start_on_resume = False
    client.goal_status = "active"
    _ = client.finish_active_turn()

    assert engine.tick() is True
    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] is None
    assert runtime["handoff_requested"] is True
    assert client.goal_status == "paused"


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [
        ("blocked", "goal_blocked"),
        ("usageLimited", "goal_usage_limited"),
        ("budgetLimited", "goal_budget_limited"),
    ],
)
def test_non_resumable_goal_status_fails_closed_with_exact_reason(
    tmp_path: Path,
    status: str,
    reason_code: str,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    client.goal_status = status
    _ = client.finish_active_turn()

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == reason_code
    assert runtime["managed_turn_id"] == "turn-goal-1"


def test_goal_identity_change_after_owned_turn_fails_closed(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    _ = client.finish_active_turn()
    client.goal_created_at += 1

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "goal_identity_changed"
    assert runtime["managed_turn_id"] == "turn-goal-1"


def test_recent_matching_interrupt_claim_recovers_one_replacement(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    arm_restart(root, path, "turn-goal-1")
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None
    assert claim_restart_request(root, runtime) is True
    _ = client.finish_active_turn(TurnOutcome.INTERRUPTED)

    assert engine.tick() is True
    recovered = load_state(root, path).values
    assert recovered["restart_count"] == 1
    assert recovered["managed_turn_id"] is None
    assert recovered["handoff_requested"] is True


def test_expired_interrupt_claim_never_authorizes_replacement(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = _engine(root, path, client)
    engine.initialize()
    assert engine.tick() is True
    arm_restart(root, path, "turn-goal-1")
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None
    assert claim_restart_request(root, runtime) is True
    values = dict(load_state(root, path).values)
    values["restart_claimed_at"] = "2000-01-01T00:00:00+00:00"
    save_state(root, path, StateDocument(values=values))
    _ = client.finish_active_turn(TurnOutcome.INTERRUPTED)

    assert engine.tick() is False
    expired = load_state(root, path).values
    assert expired["manager_error"] == "interrupt_claim_expired"
    assert expired["restart_count"] == 0
    assert expired["managed_turn_id"] == "turn-goal-1"
