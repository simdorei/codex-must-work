from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, final, override

from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_restart_guard import restart_request_is_fresh
from scripts.state import load_state
from scripts.watcher_engine import WatcherEngine
from tests.test_manager_engine_goal import (
    FakeGoalAppServer,
    accept_fake_goal_turn,
    runtime_fixture,
)
from tests.watcher_fixture import append_progress

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from scripts.manager_runtime import ManagerRuntime
    from scripts.state_io import JsonValue


class CallbackGoalServer(FakeGoalAppServer):
    def __init__(self) -> None:
        super().__init__()
        self.after_interrupt: Callable[[], None] | None = None

    @override
    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        result = super().request(method, params, timeout_seconds=timeout_seconds)
        if method == "turn/interrupt" and self.after_interrupt is not None:
            self.after_interrupt()
        return result


@final
class CompletesDuringFenceServer(CallbackGoalServer):
    def __init__(self) -> None:
        super().__init__()
        self.raced = False

    @override
    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        if (
            method == "thread/goal/get"
            and self.active == "turn-goal-1"
            and self.goal_status == "active"
            and not self.raced
        ):
            self.raced = True
            assert self.finish_active_turn() == "turn-goal-1"
        return super().request(method, params, timeout_seconds=timeout_seconds)


def _adopt_goal_turn(
    tmp_path: Path,
    client: FakeGoalAppServer,
) -> tuple[Path, Path, ManagerEngine, list[str]]:
    root, path = runtime_fixture(tmp_path)
    launches: list[str] = []
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: launches.append("watcher"),
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()
    assert engine.tick() is True
    return root, path, engine, launches


def test_detector_claim_interrupt_and_goal_replacement_form_one_recovery_path(
    tmp_path: Path,
) -> None:
    client = CallbackGoalServer()
    root, path, engine, launches = _adopt_goal_turn(tmp_path, client)
    watcher = WatcherEngine(root)
    wall_time = datetime(2026, 7, 18, tzinfo=UTC)
    assert watcher.tick(0.0, wall_time) is True
    assert watcher.tick(91.0, wall_time) is True
    assert watcher.tick(301.0, wall_time) is True
    queued = load_state(root, path).values
    assert queued["restart_request"] is not None
    assert queued["restart_claimed"] is False

    rollout = tmp_path / "sessions" / "rollout.jsonl"

    def progress_during_interrupt() -> None:
        append_progress(rollout, None)
        _ = watcher.tick(302.0, wall_time)

    client.after_interrupt = progress_during_interrupt
    assert engine.tick() is True
    interrupted = load_state(root, path).values
    assert interrupted["restart_count"] == 1
    assert interrupted["managed_turn_id"] is None

    assert engine.tick() is True
    restarted = load_state(root, path).values
    assert restarted["managed_turn_id"] == "turn-goal-2"
    assert launches == ["watcher", "watcher"]


def test_progress_arriving_after_claim_cancels_interrupt_and_keeps_goal_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CallbackGoalServer()
    root, path, engine, _launches = _adopt_goal_turn(tmp_path, client)
    watcher = WatcherEngine(root)
    wall_time = datetime(2026, 7, 18, tzinfo=UTC)
    _ = watcher.tick(0.0, wall_time)
    _ = watcher.tick(91.0, wall_time)
    _ = watcher.tick(301.0, wall_time)
    rollout = tmp_path / "sessions" / "rollout.jsonl"

    def progress_then_check(check_root: Path, runtime: ManagerRuntime) -> bool:
        append_progress(rollout, None)
        _ = watcher.tick(302.0, wall_time)
        return restart_request_is_fresh(check_root, runtime)

    monkeypatch.setattr("scripts.manager_interrupt.restart_request_is_fresh", progress_then_check)

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["restart_claimed"] is False
    assert runtime["managed_turn_id"] == "turn-goal-1"
    assert client.active == "turn-goal-1"
    assert client.goal_status == "paused"
    assert "turn/interrupt" not in client.calls


def test_completion_during_cancel_keeps_goal_paused_until_owned_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CallbackGoalServer()
    root, path, engine, _launches = _adopt_goal_turn(tmp_path, client)
    watcher = WatcherEngine(root)
    wall_time = datetime(2026, 7, 18, tzinfo=UTC)
    _ = watcher.tick(0.0, wall_time)
    _ = watcher.tick(91.0, wall_time)
    _ = watcher.tick(301.0, wall_time)
    rollout = tmp_path / "sessions" / "rollout.jsonl"

    def finish_then_check(check_root: Path, runtime: ManagerRuntime) -> bool:
        append_progress(rollout, None)
        _ = watcher.tick(302.0, wall_time)
        assert client.active is not None
        client.completed.add(client.active)
        client.active = None
        return restart_request_is_fresh(check_root, runtime)

    monkeypatch.setattr("scripts.manager_interrupt.restart_request_is_fresh", finish_then_check)
    assert engine.tick() is True
    cancelled = load_state(root, path).values
    assert cancelled["managed_turn_id"] is None
    assert cancelled["handoff_requested"] is True
    assert client.goal_status == "paused"

    assert engine.tick() is True
    resumed = load_state(root, path).values
    assert resumed["managed_turn_id"] == "turn-goal-2"
    assert resumed["manager_error"] is None


def test_paused_goal_cannot_auto_start_unowned_replacement(tmp_path: Path) -> None:
    client = FakeGoalAppServer()
    root, path, engine, launches = _adopt_goal_turn(tmp_path, client)
    assert client.goal_status == "paused"
    assert client.finish_active_turn() == "turn-goal-1"
    assert client.active is None

    assert engine.tick() is True
    between = load_state(root, path).values
    assert between["managed_turn_id"] is None
    assert client.active is None

    assert engine.tick() is True

    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] == "turn-goal-2"
    assert runtime["manager_error"] is None
    assert launches == ["watcher", "watcher"]


def test_goal_completion_during_fence_adopts_latest_goal_turn(tmp_path: Path) -> None:
    client = CompletesDuringFenceServer()
    root, path, _engine, launches = _adopt_goal_turn(tmp_path, client)

    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] == "turn-goal-2"
    assert runtime["manager_error"] is None
    assert client.active == "turn-goal-2"
    assert client.goal_status == "paused"
    assert launches == ["watcher"]


def test_goal_is_fenced_before_watcher_launch_can_finish_turn(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()

    def finish_during_launch() -> None:
        if client.active == "turn-goal-1":
            assert client.finish_active_turn() == "turn-goal-1"

    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=finish_during_launch,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )
    engine.initialize()

    assert engine.tick() is True
    between = load_state(root, path).values
    assert between["managed_turn_id"] == "turn-goal-1"
    assert between["handoff_requested"] is False
    assert client.active is None
    assert client.goal_status == "paused"

    assert engine.tick() is True
    settled = load_state(root, path).values
    assert settled["managed_turn_id"] is None
    assert settled["handoff_requested"] is True

    assert engine.tick() is True
    resumed = load_state(root, path).values
    assert resumed["managed_turn_id"] == "turn-goal-2"
    assert resumed["manager_error"] is None


def test_external_active_turn_after_normal_completion_fails_closed(tmp_path: Path) -> None:
    client = FakeGoalAppServer()
    root, path, engine, launches = _adopt_goal_turn(tmp_path, client)
    assert client.active == "turn-goal-1"
    client.completed.add("turn-goal-1")
    client.active = None

    assert engine.tick() is True
    client.active = "turn-external"
    assert engine.tick() is False

    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] is None
    assert runtime["manager_error"] == "unexpected_active_turn"
    assert launches == ["watcher"]
