from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_runtime import record_pending_turn
from scripts.state import load_state, mutate_existing_state
from tests.manager_fixture import FakeAppServer, manager_runtime_fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.state_io import JsonValue


def test_start_timeout_keeps_exact_pending_turn_for_reconciliation_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()

    def time_out_start(_thread_id: str, _turn_id: str, _timeout_seconds: float = 12.0) -> bool:
        return False

    monkeypatch.setattr(client, "wait_turn_started", time_out_start)
    now = [100.0]
    monkeypatch.setattr("scripts.manager_engine.time.monotonic", lambda: now[0])
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()

    assert engine.tick() is True

    pending = load_state(root, path).values
    assert pending["pending_turn_id"] == "turn-1"
    assert pending["pending_turn_timed_out_at"] == 100.0
    assert pending["managed_turn_id"] is None
    client.active = None
    now[0] = 101.0
    client.active = "turn-1"
    assert engine.tick() is True
    owned = load_state(root, path).values
    assert owned["pending_turn_id"] is None
    assert owned["managed_turn_id"] == "turn-1"


def test_accepted_turn_is_persisted_before_waiting_for_started_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()

    def inspect_pending(_thread_id: str, turn_id: str, _timeout_seconds: float = 12.0) -> bool:
        pending = load_state(root, path).values
        assert pending["pending_turn_id"] == turn_id
        assert pending["managed_turn_id"] is None
        return True

    monkeypatch.setattr(client, "wait_turn_started", inspect_pending)
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()

    assert engine.tick() is True


def test_grace_expiry_interrupts_exact_pending_turn_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()

    def time_out_start(_thread_id: str, _turn_id: str, _timeout_seconds: float = 12.0) -> bool:
        return False

    monkeypatch.setattr(client, "wait_turn_started", time_out_start)
    now = [100.0]
    monkeypatch.setattr("scripts.manager_engine.time.monotonic", lambda: now[0])
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()
    assert engine.tick() is True
    client.active = None

    now[0] = 161.0
    assert engine.tick() is False

    runtime = load_state(root, path).values
    assert runtime["pending_turn_id"] is None
    assert client.calls.count("turn/interrupt") == 1
    assert runtime["manager_error"] == "start_timeout"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("cancel_before_persist"),
        KeyboardInterrupt(),
        SystemExit(73),
    ],
    ids=["os-error", "keyboard-interrupt", "system-exit"],
)
def test_accepted_turn_is_interrupted_when_base_exception_precedes_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    # Given
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    engine = _engine(root, path.name, client)

    def cancel_before(_root: Path, _path: Path, _turn_id: str) -> None:
        raise failure

    monkeypatch.setattr("scripts.manager_engine.record_pending_turn", cancel_before)

    # When / Then
    with pytest.raises(type(failure)) as raised:
        _ = engine.tick()
    assert raised.value is failure
    runtime = load_state(root, path).values
    assert runtime.get("pending_turn_id") is None
    assert runtime["managed_turn_id"] is None
    assert client.calls.count("turn/interrupt") == 1
    assert client.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )


def test_accepted_turn_stays_pending_when_mutation_commits_then_keyboard_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    engine = _engine(root, path.name, client)

    def mutate_then_cancel[Result](
        target_root: Path,
        target_path: Path,
        mutator: Callable[[dict[str, JsonValue]], Result],
        *,
        after_commit: Callable[[], None] | None = None,
    ) -> Result | None:
        _ = mutate_existing_state(
            target_root,
            target_path,
            mutator,
            after_commit=after_commit,
        )
        raise KeyboardInterrupt

    monkeypatch.setattr("scripts.manager_runtime.mutate_existing_state", mutate_then_cancel)

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        _ = engine.tick()
    runtime = load_state(root, path).values
    assert runtime["pending_turn_id"] == "turn-1"
    assert runtime["managed_turn_id"] is None
    assert client.calls.count("turn/interrupt") == 0


def test_accepted_turn_stays_pending_when_system_exit_arrives_after_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    engine = _engine(root, path.name, client)

    def record_then_cancel(target_root: Path, target_path: Path, turn_id: str) -> None:
        record_pending_turn(target_root, target_path, turn_id)
        raise SystemExit(73)

    monkeypatch.setattr("scripts.manager_engine.record_pending_turn", record_then_cancel)

    # When / Then
    with pytest.raises(SystemExit) as raised:
        _ = engine.tick()
    assert raised.value.code == 73
    runtime = load_state(root, path).values
    assert runtime["pending_turn_id"] == "turn-1"
    assert runtime["managed_turn_id"] is None
    assert client.calls.count("turn/interrupt") == 0


def test_matching_active_turn_promotes_at_exact_grace_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, path = manager_runtime_fixture(tmp_path)
    client, engine, now = _timed_out_engine(root, path, monkeypatch)

    # When
    now[0] = 160.0
    client.active = "turn-1"
    assert engine.tick() is True

    # Then
    runtime = load_state(root, path).values
    assert runtime["managed_turn_id"] == "turn-1"
    assert client.calls.count("turn/interrupt") == 0


def test_matching_active_turn_is_interrupted_after_grace_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, path = manager_runtime_fixture(tmp_path)
    client, engine, now = _timed_out_engine(root, path, monkeypatch)

    # When
    now[0] = 160.001
    client.active = "turn-1"
    assert engine.tick() is False

    # Then
    runtime = load_state(root, path).values
    assert runtime["pending_turn_id"] is None
    assert runtime["managed_turn_id"] is None
    assert client.calls.count("turn/interrupt") == 1
    assert client.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )


def _engine(root: Path, runtime_name: str, client: FakeAppServer) -> ManagerEngine:
    engine = ManagerEngine(
        root,
        runtime_name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()
    return engine


def _timed_out_engine(
    root: Path,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeAppServer, ManagerEngine, list[float]]:
    client = FakeAppServer()

    def time_out_start(_thread_id: str, _turn_id: str, _timeout_seconds: float = 12.0) -> bool:
        return False

    monkeypatch.setattr(client, "wait_turn_started", time_out_start)
    now = [100.0]
    monkeypatch.setattr("scripts.manager_engine.time.monotonic", lambda: now[0])
    engine = _engine(root, path.name, client)
    assert engine.tick() is True
    client.active = None
    return client, engine, now
