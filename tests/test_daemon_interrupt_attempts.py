from __future__ import annotations

from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, final

import pytest

from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind, JsonObject
from scripts.daemon_registry import DaemonRegistry
from scripts.manager_runtime import record_turn_started
from scripts.state import StateDocument, load_state, runtime_path, save_state
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    FakeAppServer,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.daemon_task import DaemonTask


@final
class _ObservedAttemptEvent:
    """Expose when a duplicate caller waits on the active remote request."""

    def __init__(self) -> None:
        self.wait_entered = Event()
        self._released = Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_entered.set()
        return self._released.wait(timeout)

    def set(self) -> None:
        self._released.set()


def test_simultaneous_exact_activities_share_successful_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    release_request = Event()
    request_entered = Event()
    attempt_event = _ObservedAttemptEvent()
    attempts: list[JsonObject] = []
    results: list[tuple[DaemonTask, ...]] = []
    try:
        _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")

        def block_interrupt(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            _ = timeout_seconds
            if method == "turn/interrupt":
                attempts.append(params)
                request_entered.set()
                assert release_request.wait(1.0)
            return {}

        activity = _unowned_activity()
        monkeypatch.setattr(clients[0], "request", block_interrupt)
        monkeypatch.setattr("scripts.daemon_registry.Event", lambda: attempt_event)
        first = Thread(target=lambda: results.append(registry.selected(activity)))
        second = Thread(target=lambda: results.append(registry.selected(activity)))

        first.start()
        assert request_entered.wait(1.0)
        second.start()
        assert attempt_event.wait_entered.wait(1.0)
        assert len(attempts) == 1
        release_request.set()
        first.join(1.0)
        second.join(1.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert results == [(), ()]
        assert attempts == [{"threadId": "thread-unowned", "turnId": "turn-unowned"}]
    finally:
        release_request.set()
        registry.close()


def test_waiting_exact_activity_retries_after_leader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    release_failure = Event()
    request_entered = Event()
    attempt_event = _ObservedAttemptEvent()
    attempts: list[JsonObject] = []
    errors: list[OSError] = []
    results: list[tuple[DaemonTask, ...]] = []
    failure = OSError("interrupt_failed")
    try:
        _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")

        def fail_then_succeed(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            _ = timeout_seconds
            if method != "turn/interrupt":
                return {}
            attempts.append(params)
            if len(attempts) == 1:
                request_entered.set()
                assert release_failure.wait(1.0)
                raise failure
            return {}

        def select_first() -> None:
            try:
                _ = registry.selected(_unowned_activity())
            except OSError as error:
                errors.append(error)

        monkeypatch.setattr(clients[0], "request", fail_then_succeed)
        monkeypatch.setattr("scripts.daemon_registry.Event", lambda: attempt_event)
        first = Thread(target=select_first)
        second = Thread(target=lambda: results.append(registry.selected(_unowned_activity())))

        first.start()
        assert request_entered.wait(1.0)
        second.start()
        assert attempt_event.wait_entered.wait(1.0)
        assert len(attempts) == 1
        release_failure.set()
        first.join(1.0)
        second.join(1.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert [str(error) for error in errors] == ["interrupt_failed"]
        assert results == [()]
        assert len(attempts) == 2
    finally:
        release_failure.set()
        registry.close()


@pytest.mark.parametrize("succeeds", [True, False], ids=["success", "failure"])
def test_owner_promoted_while_interrupt_is_in_flight_prevents_later_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    succeeds: bool,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    release_request = Event()
    request_entered = Event()
    attempts: list[JsonObject] = []
    results: list[tuple[DaemonTask, ...]] = []
    errors: list[OSError] = []
    failure = OSError("interrupt_failed")
    try:
        task, _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")

        def finish_interrupt(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            _ = timeout_seconds
            if method == "turn/interrupt":
                attempts.append(params)
                request_entered.set()
                assert release_request.wait(1.0)
                if not succeeds:
                    raise failure
            return {}

        def select_activity() -> None:
            try:
                results.append(registry.selected(_unowned_activity()))
            except OSError as error:
                errors.append(error)

        monkeypatch.setattr(clients[0], "request", finish_interrupt)
        worker = Thread(target=select_activity)
        worker.start()
        assert request_entered.wait(1.0)
        _promote_exact_owner(root, "turn-unowned")
        release_request.set()
        worker.join(1.0)

        assert not worker.is_alive()
        if succeeds:
            assert results == [(task,)]
            assert errors == []
        else:
            assert results == []
            assert [str(error) for error in errors] == ["interrupt_failed"]
        assert registry.selected(_unowned_activity()) == (task,)
        assert len(attempts) == 1
    finally:
        release_request.set()
        registry.close()


def _create_registry(root: Path, clients: list[FakeAppServer]) -> DaemonRegistry:
    def factory(
        _fingerprint: str,
        listener: Callable[[AppServerActivity], None],
    ) -> FakeAppServer:
        client = FakeAppServer(listener)
        clients.append(client)
        return client

    return DaemonRegistry(root, factory, lambda _activity: None, lambda: 100.0, RLock())


def _unowned_activity() -> AppServerActivity:
    return AppServerActivity(
        AppServerActivityKind.TURN_STARTED,
        thread_id="thread-unowned",
        turn_id="turn-unowned",
    )


def _promote_exact_owner(root: Path, turn_id: str) -> None:
    path = runtime_path(root, FIRST_SESSION)
    values = dict(load_state(root, path).values)
    values["handoff_requested"] = True
    values["managed_turn_id"] = None
    values["pending_turn_id"] = turn_id
    values["pending_turn_timed_out_at"] = None
    save_state(root, path, StateDocument(values=values))
    record_turn_started(root, path, turn_id)
