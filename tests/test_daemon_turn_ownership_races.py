from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

import pytest

from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind, JsonObject
from scripts.daemon_registry import DaemonRegistry
from scripts.manager_runtime import ManagerRuntime, load_manager_runtime, record_turn_started
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


def test_expired_start_is_not_interrupted_when_concurrent_manager_promotes_before_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    try:
        task, _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")
        _set_expired_pending_turn(root, "turn-known")
        client = clients[0]
        interrupts: list[JsonObject] = []
        original_request = client.request

        def capture_request(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            if method == "turn/interrupt":
                interrupts.append(params)
            return original_request(method, params, timeout_seconds=timeout_seconds)

        def promote_before_clear(
            target_root: Path,
            target_path: Path,
            turn_id: str,
        ) -> bool:
            record_turn_started(target_root, target_path, turn_id)
            return False

        monkeypatch.setattr(client, "request", capture_request)
        monkeypatch.setattr("scripts.daemon_registry.clear_pending_turn", promote_before_clear)
        activity = AppServerActivity(
            AppServerActivityKind.TURN_STARTED,
            thread_id=FIRST_SESSION,
            turn_id="turn-known",
        )

        selected = registry.selected(activity)

        runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert interrupts == []
        assert selected == (task,)
        assert (runtime["pending_turn_id"], runtime["managed_turn_id"]) == (None, "turn-known")
    finally:
        registry.close()


@pytest.mark.parametrize(
    "failure",
    [OSError("reread_failed"), KeyboardInterrupt(), SystemExit(73)],
    ids=["os-error", "keyboard-interrupt", "system-exit"],
)
def test_concurrent_promotion_survives_base_exception_at_durable_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    try:
        _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")
        _set_expired_pending_turn(root, "turn-known")
        original_load = load_manager_runtime
        load_count = 0
        interrupts: list[JsonObject] = []

        def capture_request(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            _ = timeout_seconds
            if method == "turn/interrupt":
                interrupts.append(params)
            return {}

        def promote_before_clear(
            target_root: Path,
            target_path: Path,
            turn_id: str,
        ) -> bool:
            record_turn_started(target_root, target_path, turn_id)
            return False

        def fail_durable_reread(
            root_path: Path,
            runtime_name: str,
        ) -> ManagerRuntime | None:
            nonlocal load_count
            load_count += 1
            if load_count == 3:
                raise failure
            return original_load(root_path, runtime_name)

        monkeypatch.setattr(clients[0], "request", capture_request)
        monkeypatch.setattr("scripts.daemon_registry.clear_pending_turn", promote_before_clear)
        monkeypatch.setattr("scripts.daemon_registry.load_manager_runtime", fail_durable_reread)
        activity = AppServerActivity(
            AppServerActivityKind.TURN_STARTED,
            thread_id=FIRST_SESSION,
            turn_id="turn-known",
        )

        with pytest.raises(type(failure)) as raised:
            _ = registry.selected(activity)

        runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert raised.value is failure
        assert (runtime["pending_turn_id"], runtime["managed_turn_id"]) == (None, "turn-known")
        assert interrupts == []
    finally:
        registry.close()


@pytest.mark.parametrize(
    "failure",
    [OSError("interrupt_failed"), KeyboardInterrupt(), SystemExit(73)],
    ids=["os-error", "keyboard-interrupt", "system-exit"],
)
def test_failed_interrupt_attempt_allows_exact_activity_to_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    registry = _create_registry(root, clients)
    try:
        _ = registry.start(start_request(FIRST_SESSION, transcript), "digest")
        attempts: list[JsonObject] = []

        def fail_first_interrupt(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            _ = timeout_seconds
            if method == "turn/interrupt":
                attempts.append(params)
                if len(attempts) == 1:
                    raise failure
            return {}

        monkeypatch.setattr(clients[0], "request", fail_first_interrupt)
        activity = AppServerActivity(
            AppServerActivityKind.TURN_STARTED,
            thread_id="thread-unowned",
            turn_id="turn-unowned",
        )

        with pytest.raises(type(failure)) as raised:
            _ = registry.selected(activity)
        selected = registry.selected(activity)

        assert raised.value is failure
        assert selected == ()
        assert attempts == [
            {"threadId": "thread-unowned", "turnId": "turn-unowned"},
            {"threadId": "thread-unowned", "turnId": "turn-unowned"},
        ]
    finally:
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


def _set_expired_pending_turn(root: Path, turn_id: str) -> None:
    path = runtime_path(root, FIRST_SESSION)
    values = dict(load_state(root, path).values)
    values["handoff_requested"] = True
    values["managed_turn_id"] = None
    values["pending_turn_id"] = turn_id
    values["pending_turn_timed_out_at"] = 0.0
    save_state(root, path, StateDocument(values=values))
