from __future__ import annotations

import threading
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, final

import pytest

from scripts import private_root
from scripts.daemon_models import DaemonServiceError
from scripts.daemon_service import DaemonService
from scripts.setup import enable_session
from scripts.state import runtime_path
from tests.daemon_service_fixture import (
    FakeAppServer,
    activation_request,
    capabilities,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.app_server_protocol import AppServerActivity
    from scripts.daemon_models import StartRequest, ToolResult


@pytest.mark.parametrize("worker_count", [2, 20])
def test_fresh_root_different_session_managed_starts_all_succeed(
    tmp_path: Path,
    worker_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex-must-work"
    requests = tuple(_request(tmp_path, index) for index in range(worker_count))
    clients: list[FakeAppServer] = []
    service = _service(root, lambda listener: _record_client(clients, listener))
    initialization = threading.Barrier(worker_count)
    restore_lock = threading.Lock()
    restored = False

    def overlap_initialization(path: Path) -> None:
        nonlocal restored
        with suppress(threading.BrokenBarrierError):
            _ = initialization.wait(timeout=2.0)
        with restore_lock:
            if not restored:
                monkeypatch.undo()
                restored = True
        private_root.ensure_private_root(path)

    monkeypatch.setattr(private_root, "_initialize_root", overlap_initialization)
    launch = threading.Barrier(worker_count + 1)
    results: list[ToolResult] = []
    errors: list[BaseException] = []

    def run(request: StartRequest) -> None:
        _ = launch.wait()
        try:
            results.append(service.start(request))
        except BaseException as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    workers = [threading.Thread(target=run, args=(request,)) for request in requests]
    for worker in workers:
        worker.start()
    _ = launch.wait()
    for worker in workers:
        worker.join(timeout=10.0)

    try:
        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == worker_count
        assert all(not result.reused for result in results)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OSError("factory_failed"), DaemonServiceError),
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(), SystemExit),
    ],
)
def test_factory_failure_removes_runtime_created_by_attempt(
    tmp_path: Path,
    failure: BaseException,
    expected: type[BaseException],
) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    root, transcript = session_files(tmp_path, session_id)
    service = _service(root, lambda _listener: _raise(failure))

    try:
        with pytest.raises(expected):
            _ = service.start(start_request(session_id, transcript))
        assert not runtime_path(root, session_id).exists()
    finally:
        service.close()


def test_factory_failure_preserves_preexisting_runtime_bytes(tmp_path: Path) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    root, transcript = session_files(tmp_path, session_id)
    service = _service(root, lambda _listener: _raise(OSError("factory_failed")))
    _ = enable_session(root, activation_request(session_id, transcript), capabilities())
    path = runtime_path(root, session_id)
    before = path.read_bytes()

    try:
        with pytest.raises(DaemonServiceError):
            _ = service.start(start_request(session_id, transcript))
        assert path.read_bytes() == before
    finally:
        service.close()


def test_waiter_retries_after_factory_failure_and_owns_clean_runtime(tmp_path: Path) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    root, transcript = session_files(tmp_path, session_id)
    factory = _RetryFactory()
    service = _service(root, factory.create)
    request = start_request(session_id, transcript)
    results: list[ToolResult] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(service.start(request))
        except BaseException as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert factory.entered.wait(2.0)
    second.start()
    factory.release.set()
    first.join(timeout=3.0)
    second.join(timeout=3.0)

    try:
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(errors) == 1
        assert len(results) == 1
        assert not results[0].reused
        assert runtime_path(root, session_id).is_file()
        assert factory.calls == 2
    finally:
        factory.release.set()
        service.close()


def _request(tmp_path: Path, index: int) -> StartRequest:
    session_id = str(uuid.UUID(int=index + 1))
    _, transcript = session_files(tmp_path, session_id)
    return start_request(session_id, transcript)


def _record_client(
    clients: list[FakeAppServer],
    listener: Callable[[AppServerActivity], None],
) -> FakeAppServer:
    client = FakeAppServer(listener)
    clients.append(client)
    return client


def _service(
    root: Path,
    factory: Callable[[Callable[[AppServerActivity], None]], FakeAppServer],
) -> DaemonService:
    return DaemonService(
        root=root,
        client_factory=lambda _fingerprint, listener: factory(listener),
        fingerprint_provider=lambda: "digest",
    )


def _raise(failure: BaseException) -> FakeAppServer:
    raise failure


@final
class _RetryFactory:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def create(self, listener: Callable[[AppServerActivity], None]) -> FakeAppServer:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(2.0)
            message = "factory_failed"
            raise OSError(message)
        return FakeAppServer(listener)
