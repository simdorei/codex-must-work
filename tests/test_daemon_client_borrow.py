from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from scripts.daemon_client_pool import SharedClientPool
from scripts.daemon_service import DaemonService
from scripts.daemon_task import DaemonTask, DaemonTaskConfig
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

    from scripts.app_server_protocol import AppServerActivity, ManagedAppServer
    from scripts.daemon_models import StartRequest, ToolResult
    from scripts.manager_lease import ManagerLease


def test_failed_session_does_not_close_other_sessions_unpublished_client_borrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, first_transcript = session_files(tmp_path, FIRST_SESSION)
    _, second_transcript = session_files(tmp_path, SECOND_SESSION)
    clients: list[FakeAppServer] = []
    service = DaemonService(
        root=root,
        client_factory=lambda _fingerprint, listener: _client(clients, listener),
        fingerprint_provider=lambda: "digest",
    )
    borrowed = threading.Event()
    release_borrower = threading.Event()
    original_init = DaemonTask.__init__
    original_initialize = DaemonTask.initialize

    def pause_after_borrow(
        task: DaemonTask,
        config: DaemonTaskConfig,
        client: ManagedAppServer | None,
        lease: ManagerLease | None,
    ) -> None:
        original_init(task, config, client, lease)
        if task.session_id == SECOND_SESSION:
            borrowed.set()
            assert release_borrower.wait(3.0)

    def fail_first(task: DaemonTask) -> None:
        if task.session_id == FIRST_SESSION:
            message = "initialize_failed"
            raise OSError(message)
        original_initialize(task)

    monkeypatch.setattr(DaemonTask, "__init__", pause_after_borrow)
    monkeypatch.setattr(DaemonTask, "initialize", fail_first)
    results: list[ToolResult] = []
    errors: list[BaseException] = []

    def run(request: StartRequest) -> None:
        try:
            results.append(service.start(request))
        except BaseException as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    borrower = threading.Thread(
        target=run,
        args=(start_request(SECOND_SESSION, second_transcript),),
    )
    failing = threading.Thread(target=run, args=(start_request(FIRST_SESSION, first_transcript),))
    borrower.start()
    assert borrowed.wait(2.0)
    failing.start()
    failing.join(timeout=3.0)

    try:
        assert not failing.is_alive()
        assert len(errors) == 1
        assert len(clients) == 1
        assert clients[0].close_count == 0
        release_borrower.set()
        borrower.join(timeout=3.0)
        assert not borrower.is_alive()
        assert len(results) == 1
        assert clients[0].close_count == 0
    finally:
        release_borrower.set()
        borrower.join(timeout=3.0)
        service.close()


def test_client_borrow_duplicate_commit_is_bounded_and_release_is_idempotent() -> None:
    clients: list[FakeAppServer] = []
    pool = SharedClientPool(
        lambda _fingerprint, listener: _client(clients, listener),
        lambda _activity: None,
        threading.RLock(),
    )
    borrow = pool.borrow("digest")

    borrow.commit()
    with pytest.raises(RuntimeError, match="client_borrow_already_committed"):
        borrow.commit()

    assert pool.reference_counts() == (0, 1, 0)
    assert clients[0].close_count == 0
    borrow.release()
    borrow.release()
    borrow.rollback()
    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1


def test_client_borrow_duplicate_rollback_is_idempotent() -> None:
    clients: list[FakeAppServer] = []
    pool = SharedClientPool(
        lambda _fingerprint, listener: _client(clients, listener),
        lambda _activity: None,
        threading.RLock(),
    )
    borrow = pool.borrow("digest")

    borrow.rollback()
    borrow.rollback()
    borrow.release()

    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1


def _client(
    clients: list[FakeAppServer],
    listener: Callable[[AppServerActivity], None],
) -> FakeAppServer:
    client = FakeAppServer(listener)
    clients.append(client)
    return client
