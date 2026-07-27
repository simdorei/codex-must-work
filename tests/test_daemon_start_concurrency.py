from __future__ import annotations

import threading
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING, final

import pytest

from scripts.daemon_models import DaemonServiceError, SessionId, SessionRequest
from scripts.daemon_scheduler import DeadlineScheduler, SchedulerError
from scripts.daemon_service import DaemonService
from scripts.daemon_task import DaemonTask
from scripts.manager_lease import manager_lease_owner
from scripts.state import runtime_path
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    SECOND_SESSION,
    FakeAppServer,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.app_server_protocol import AppServerActivity
    from scripts.daemon_models import StartRequest, ToolResult
    from scripts.daemon_scheduler import SchedulerKey


def test_scheduler_publication_failure_rolls_back_started_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = _service(root, clients)

    def reject_publication(
        _scheduler: DeadlineScheduler,
        _key: SchedulerKey,
        _deadline: float,
        _callback: Callable[[], None],
    ) -> None:
        reason = "scheduler_closed"
        raise SchedulerError(reason)

    monkeypatch.setattr(DeadlineScheduler, "schedule", reject_publication)

    # When
    try:
        with pytest.raises(DaemonServiceError) as raised:
            _ = service.start(start_request(FIRST_SESSION, transcript))
        observed = (
            raised.value.reason_code,
            service.status(SessionRequest(SessionId(FIRST_SESSION))).managed is None,
            manager_lease_owner(root, runtime_path(root, FIRST_SESSION).name) is None,
            len(clients) == 1 and clients[0].closed,
            clients[0].close_count if clients else 0,
        )

        # Then
        assert observed == ("scheduler_unavailable", True, True, True, 1)
    finally:
        service.close()
        for client in clients:
            if not client.closed:
                client.close()


@pytest.mark.parametrize("worker_count", [2, 10])
def test_same_session_concurrent_starts_create_once_and_exactly_reuse(
    tmp_path: Path,
    worker_count: int,
) -> None:
    # Given
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    probe = _FactoryProbe(worker_count, clients)
    service = DaemonService(
        root=root,
        client_factory=probe.create,
        fingerprint_provider=lambda: "digest",
    )
    request = start_request(FIRST_SESSION, transcript)
    launch = threading.Barrier(worker_count + 1)
    results: list[ToolResult] = []
    errors: list[Exception] = []

    def start() -> None:
        _ = launch.wait()
        try:
            results.append(service.start(request))
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    workers = [threading.Thread(target=start, daemon=True) for _index in range(worker_count)]
    for worker in workers:
        worker.start()

    # When
    _ = launch.wait()
    assert probe.first_entered.wait(1.0)
    _ = probe.all_entered.wait(0.5)
    probe.release.set()
    for worker in workers:
        worker.join(timeout=3.0)

    # Then
    try:
        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert probe.entry_count == 1
        assert len(results) == worker_count
        assert [result.reused for result in results].count(False) == 1
        assert [result.reused for result in results].count(True) == worker_count - 1
    finally:
        probe.release.set()
        service.close()
        for client in clients:
            if not client.closed:
                client.close()


def test_different_session_starts_overlap_without_holding_global_io_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    root, first = session_files(tmp_path, FIRST_SESSION)
    _, second = session_files(tmp_path, SECOND_SESSION)
    service = _service(root, [])
    requests = (
        replace(start_request(FIRST_SESSION, first), auto_restart=False, observe_only=True),
        replace(
            start_request(SECOND_SESSION, second),
            auto_restart=False,
            observe_only=True,
        ),
    )
    initialize_overlap = threading.Barrier(2)
    original_initialize = DaemonTask.initialize

    def overlapping_initialize(task: DaemonTask) -> None:
        _ = initialize_overlap.wait(timeout=1.0)
        original_initialize(task)

    monkeypatch.setattr(DaemonTask, "initialize", overlapping_initialize)
    results: list[ToolResult] = []
    errors: list[Exception] = []

    def start(request: StartRequest) -> None:
        try:
            results.append(service.start(request))
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    # When
    workers = [threading.Thread(target=start, args=(request,), daemon=True) for request in requests]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3.0)

    # Then
    try:
        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
    finally:
        service.close()


def _service(root: Path, clients: list[FakeAppServer]) -> DaemonService:
    def factory(
        _fingerprint: str,
        listener: Callable[[AppServerActivity], None],
    ) -> FakeAppServer:
        client = FakeAppServer(listener)
        clients.append(client)
        return client

    return DaemonService(
        root=root,
        client_factory=factory,
        fingerprint_provider=lambda: "digest",
    )


@final
class _FactoryProbe:
    def __init__(self, expected: int, clients: list[FakeAppServer]) -> None:
        self._expected = expected
        self._clients = clients
        self._lock = threading.Lock()
        self.first_entered = Event()
        self.all_entered = Event()
        self.release = Event()
        self.entry_count = 0

    def create(
        self,
        _fingerprint: str,
        listener: Callable[[AppServerActivity], None],
    ) -> FakeAppServer:
        with self._lock:
            self.entry_count += 1
            self.first_entered.set()
            if self.entry_count == self._expected:
                self.all_entered.set()
        assert self.release.wait(2.0)
        client = FakeAppServer(listener)
        with self._lock:
            self._clients.append(client)
        return client
