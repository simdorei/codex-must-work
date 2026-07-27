from __future__ import annotations

import threading
from threading import Event
from typing import TYPE_CHECKING

import pytest

from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind
from scripts.daemon_models import DaemonServiceError
from scripts.daemon_scheduler import DeadlineScheduler
from scripts.daemon_task import DaemonTask
from scripts.manager_lease import manager_lease_owner
from scripts.state import runtime_path
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    SECOND_SESSION,
    CloseSynchronization,
    FakeAppServer,
    StartSynchronization,
    create_service,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.daemon_scheduler import SchedulerKey


def test_stdout_response_during_last_task_detach_does_not_invert_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    synchronization = CloseSynchronization(
        threading.Barrier(2),
        threading.Barrier(2),
        Event(),
    )
    service = create_service(
        root,
        clients,
        close_synchronization=synchronization,
    )
    complete_on_next_drive = Event()

    def complete_task(_task: DaemonTask) -> bool:
        return not complete_on_next_drive.is_set()

    monkeypatch.setattr(DaemonTask, "drive", complete_task)
    _ = service.start(start_request(FIRST_SESSION, transcript))

    # When
    complete_on_next_drive.set()
    clients[0].emit_activation_complete(FIRST_SESSION)

    # Then
    assert clients[0].closed_event.wait(2.0)
    assert synchronization.response_delivered.is_set()
    assert clients[0].closed is True
    service.close()
    service.close()
    assert clients[0].close_count == 1


def test_close_failure_still_closes_remaining_tasks_with_bounded_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, first = session_files(tmp_path, FIRST_SESSION)
    _, second = session_files(tmp_path, SECOND_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    _ = service.start(start_request(FIRST_SESSION, first))
    _ = service.start(start_request(SECOND_SESSION, second))
    closed: list[str] = []
    original_close = DaemonTask.close

    def close_with_first_failure(task: DaemonTask) -> None:
        closed.append(task.session_id)
        original_close(task)
        if task.session_id == FIRST_SESSION:
            message = "private-" + ("x" * 1_000)
            raise OSError(message)

    monkeypatch.setattr(DaemonTask, "close", close_with_first_failure)

    # When
    with pytest.raises(
        DaemonServiceError,
        match=r"task_close_failed:OSError",
    ) as raised:
        service.close()

    # Then
    assert closed == [FIRST_SESSION, SECOND_SESSION]
    assert len(raised.value.reason_code) <= 256
    assert "private-" not in raised.value.reason_code
    assert clients[0].closed is True
    assert manager_lease_owner(root, runtime_path(root, FIRST_SESSION).name) is None
    assert manager_lease_owner(root, runtime_path(root, SECOND_SESSION).name) is None

    monkeypatch.setattr(DaemonTask, "close", original_close)
    with pytest.raises(
        DaemonServiceError,
        match=r"task_close_failed:OSError",
    ) as repeated:
        service.close()
    assert repeated.value.reason_code == raised.value.reason_code
    assert clients[0].close_count == 1


def test_start_admission_cannot_install_after_close_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    synchronization = StartSynchronization(Event(), Event())
    service = create_service(root, clients, start_synchronization=synchronization)
    created_tasks: list[DaemonTask] = []
    original_initialize = DaemonTask.initialize

    def capture_task(task: DaemonTask) -> None:
        created_tasks.append(task)
        original_initialize(task)

    monkeypatch.setattr(DaemonTask, "initialize", capture_task)
    start_errors: list[Exception] = []
    start_done = Event()
    close_done = Event()

    def start_session() -> None:
        try:
            _ = service.start(start_request(FIRST_SESSION, transcript))
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            start_errors.append(error)
        finally:
            start_done.set()

    def close_service() -> None:
        service.close()
        close_done.set()

    start_thread = threading.Thread(target=start_session, daemon=True)
    close_thread = threading.Thread(target=close_service, daemon=True)
    start_thread.start()
    assert synchronization.factory_entered.wait(1.0)

    # When
    close_thread.start()
    close_returned_before_release = close_done.wait(0.25)
    synchronization.allow_factory_return.set()

    # Then
    assert start_done.wait(2.0)
    assert close_done.wait(2.0)
    start_thread.join(timeout=0.25)
    close_thread.join(timeout=0.25)
    assert close_returned_before_release is False
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], DaemonServiceError)
    assert start_errors[0].reason_code == "daemon_closed"
    assert manager_lease_owner(root, runtime_path(root, FIRST_SESSION).name) is None
    assert clients[0].closed is True

    for task in created_tasks:
        task.close()
    for client in clients:
        if not client.closed:
            client.close()


def test_concurrent_close_waits_for_owner_and_observes_same_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    service = create_service(root, [])
    _ = service.start(start_request(FIRST_SESSION, transcript))
    close_entered = threading.Barrier(2)
    allow_close = threading.Barrier(2)
    original_close = DaemonTask.close

    def blocked_failing_close(task: DaemonTask) -> None:
        _ = close_entered.wait()
        _ = allow_close.wait()
        original_close(task)
        message = "private-" + ("x" * 1_000)
        raise OSError(message)

    monkeypatch.setattr(DaemonTask, "close", blocked_failing_close)
    results: list[str] = []
    first_done = Event()
    second_started = Event()
    second_done = Event()

    def close_service(done: Event, started: Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            service.close()
        except DaemonServiceError as error:
            results.append(error.reason_code)
        else:
            results.append("ok")
        finally:
            done.set()

    first = threading.Thread(target=close_service, args=(first_done,), daemon=True)
    second = threading.Thread(
        target=close_service,
        args=(second_done, second_started),
        daemon=True,
    )
    first.start()
    _ = close_entered.wait()

    # When
    second.start()
    assert second_started.wait(1.0)
    second_returned_before_cleanup = second_done.wait(0.25)
    _ = allow_close.wait()

    # Then
    assert first_done.wait(2.0)
    assert second_done.wait(2.0)
    first.join(timeout=0.25)
    second.join(timeout=0.25)
    assert second_returned_before_cleanup is False
    assert results == ["task_close_failed:OSError", "task_close_failed:OSError"]
    assert manager_lease_owner(root, runtime_path(root, FIRST_SESSION).name) is None


def test_activity_scheduling_at_close_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    service = create_service(root, [])
    _ = service.start(start_request(FIRST_SESSION, transcript))
    wake_entered = threading.Barrier(2)
    allow_wake = threading.Barrier(2)
    original_wake = DeadlineScheduler.wake
    wake_blocked = False

    def blocked_wake(
        scheduler: DeadlineScheduler,
        key: SchedulerKey,
        callback: Callable[[], None],
    ) -> None:
        nonlocal wake_blocked
        if not wake_blocked:
            wake_blocked = True
            _ = wake_entered.wait()
            _ = allow_wake.wait()
        original_wake(scheduler, key, callback)

    monkeypatch.setattr(DeadlineScheduler, "wake", blocked_wake)
    activity_errors: list[Exception] = []
    activity_done = Event()
    close_done = Event()
    activity = AppServerActivity(
        AppServerActivityKind.TURN_PROGRESS,
        FIRST_SESSION,
        "turn-1",
        None,
    )

    def deliver_activity() -> None:
        try:
            service.app_server_activity(activity)
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            activity_errors.append(error)
        finally:
            activity_done.set()

    def close_after_activity() -> None:
        service.close()
        close_done.set()

    activity_thread = threading.Thread(target=deliver_activity, daemon=True)
    close_thread = threading.Thread(target=close_after_activity, daemon=True)
    activity_thread.start()
    _ = wake_entered.wait()

    # When
    close_thread.start()
    close_returned_before_activity = close_done.wait(0.25)
    _ = allow_wake.wait()

    # Then
    assert activity_done.wait(2.0)
    assert close_done.wait(2.0)
    activity_thread.join(timeout=0.25)
    close_thread.join(timeout=0.25)
    assert close_returned_before_activity is False
    assert activity_errors == []
