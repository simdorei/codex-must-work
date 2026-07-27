from __future__ import annotations

import os
import threading
from threading import Event
from typing import TYPE_CHECKING

import pytest

from scripts.codex_executable import CodexExecutableError
from scripts.daemon_models import (
    DaemonServiceError,
    SessionId,
    SessionRequest,
)
from scripts.daemon_task import DaemonTask
from scripts.goal_control import GoalControlError
from scripts.manager_lease import manager_lease_owner
from scripts.setup import enable_session
from scripts.state import load_state, runtime_path
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    SECOND_SESSION,
    FakeAppServer,
    activation_request,
    append_turn_event,
    bind_pending_activation,
    capabilities,
    create_service,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.watcher_engine import WatcherEngine
    from scripts.watcher_source import RolloutCursor


def test_handoff_waits_for_activation_turn_completion(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    request = start_request(FIRST_SESSION, transcript)
    try:
        _ = service.start(request)
        path = runtime_path(root, FIRST_SESSION)
        assert load_state(root, path).values["handoff_requested"] is False
        client = clients[0]
        client.allow_turn_start.clear()

        # When
        append_turn_event(transcript, "task_complete")
        client.emit_activation_complete(FIRST_SESSION)
        assert client.turn_start_called.wait(1.0)

        # Then
        assert load_state(root, path).values["handoff_requested"] is True
    finally:
        if clients:
            clients[0].allow_turn_start.set()
        service.close()


def test_transcript_completion_starts_handoff_without_app_server_event(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        client = clients[0]
        client.allow_turn_start.clear()

        # When
        append_turn_event(transcript, "task_complete")

        # Then
        assert client.turn_start_called.wait(1.0)
    finally:
        if clients:
            clients[0].allow_turn_start.set()
        service.close()


def test_app_server_completion_alone_cannot_open_activation_fence(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        path = runtime_path(root, FIRST_SESSION)
        client = clients[0]

        # When
        client.emit_activation_complete(FIRST_SESSION)

        # Then
        assert not client.turn_start_called.wait(0.5)
        assert load_state(root, path).values["handoff_requested"] is False
    finally:
        service.close()


def test_two_sessions_hold_distinct_leases_until_service_close(tmp_path: Path) -> None:
    # Given
    root, first = session_files(tmp_path, FIRST_SESSION)
    _, second = session_files(tmp_path, SECOND_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    first_path = runtime_path(root, FIRST_SESSION)
    second_path = runtime_path(root, SECOND_SESSION)

    # When
    _ = service.start(start_request(FIRST_SESSION, first))
    _ = service.start(start_request(SECOND_SESSION, second))

    # Then
    assert manager_lease_owner(root, first_path.name) == os.getpid()
    assert manager_lease_owner(root, second_path.name) == os.getpid()
    service.close()
    assert manager_lease_owner(root, first_path.name) is None
    assert manager_lease_owner(root, second_path.name) is None


def test_start_schedules_immediate_watcher_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    reconciled = Event()

    def tick(_watcher: WatcherEngine, _now: float, _wall: datetime) -> bool:
        reconciled.set()
        return True

    monkeypatch.setattr("scripts.daemon_service.WatcherEngine.tick", tick)
    service = create_service(root, [])
    try:
        # When
        _ = service.start(start_request(FIRST_SESSION, transcript))

        # Then
        assert reconciled.wait(1.0)
    finally:
        service.close()


def test_resume_failure_rolls_back_runtime_lease_and_client(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients, fail_resume=True)
    path = runtime_path(root, FIRST_SESSION)

    # When
    with pytest.raises(DaemonServiceError, match="resume_failed"):
        _ = service.start(start_request(FIRST_SESSION, transcript))

    # Then
    assert not path.exists()
    assert manager_lease_owner(root, path.name) is None
    assert clients[0].closed is True
    service.close()


def test_cursor_failure_rolls_back_runtime_lease_and_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    path = runtime_path(root, FIRST_SESSION)

    def fail_cursor(_root: Path, _session_id: str, _cursor: RolloutCursor) -> None:
        message = "cursor_failed"
        raise OSError(message)

    monkeypatch.setattr("scripts.daemon_activation_fence.save_cursor", fail_cursor)

    # When
    with pytest.raises(DaemonServiceError, match="cursor_failed"):
        _ = service.start(start_request(FIRST_SESSION, transcript))

    # Then
    assert not path.exists()
    assert manager_lease_owner(root, path.name) is None
    assert clients[0].closed is True
    service.close()


def test_persisted_runtime_is_recovered_with_a_fresh_lease(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    request = activation_request(FIRST_SESSION, transcript)
    _ = enable_session(root, request, capabilities())
    bind_pending_activation(root, FIRST_SESSION, transcript)
    path = runtime_path(root, FIRST_SESSION)

    # When
    service = create_service(root, [])
    try:
        result = service.status(SessionRequest(SessionId(FIRST_SESSION)))

        # Then
        assert result.enabled is True
        assert manager_lease_owner(root, path.name) == os.getpid()
    finally:
        service.close()


def test_completed_goal_runtime_is_disabled_during_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    request = activation_request(FIRST_SESSION, transcript)
    _ = enable_session(root, request, capabilities())
    bind_pending_activation(root, FIRST_SESSION, transcript)
    path = runtime_path(root, FIRST_SESSION)

    def goal_complete(_task: DaemonTask) -> None:
        reason = "goal_complete"
        raise GoalControlError(reason)

    monkeypatch.setattr(DaemonTask, "initialize", goal_complete)

    # When
    service = create_service(root, [])
    try:
        result = service.status(SessionRequest(SessionId(FIRST_SESSION)))

        # Then
        assert result.enabled is False
        assert not path.exists()
    finally:
        service.close()


def test_recovery_resume_failure_releases_owned_resources(tmp_path: Path) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
    bind_pending_activation(root, FIRST_SESSION, transcript)
    path = runtime_path(root, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    schedulers_before = _scheduler_count()

    # When
    service = create_service(root, clients, fail_resume=True)
    try:
        result = service.status(SessionRequest(SessionId(FIRST_SESSION)))

        # Then
        assert result.manager_error == "app_server_failed"
    finally:
        service.close()

    assert manager_lease_owner(root, path.name) is None
    assert clients[0].closed is True
    assert _scheduler_count() == schedulers_before


def test_stale_recovery_failure_isolated_without_blocking_daemon_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an enabled legacy runtime whose trusted Codex executable has changed.
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
    bind_pending_activation(root, FIRST_SESSION, transcript)
    path = runtime_path(root, FIRST_SESSION)
    clients: list[FakeAppServer] = []

    def reject_stale_runtime(_task: DaemonTask) -> None:
        reason = "trusted_codex_executable_changed"
        raise CodexExecutableError(reason)

    monkeypatch.setattr(DaemonTask, "initialize", reject_stale_runtime)

    # When the resident daemon starts and recovers persisted work.
    service = create_service(root, clients)
    try:
        result = service.status(SessionRequest(SessionId(FIRST_SESSION)))

        # Then the failed task is fenced while the daemon remains queryable.
        runtime = load_state(root, path).values
        assert result.enabled is True
        assert result.manager_error == "trusted_codex_executable_changed"
        assert runtime["manager_ready"] is False
        assert runtime["manager_pid"] is None
        assert manager_lease_owner(root, path.name) is None
        assert clients[0].closed is True
    finally:
        service.close()


def _scheduler_count() -> int:
    return sum(thread.name == "cmw-deadline-scheduler" for thread in threading.enumerate())
