from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.daemon_client_pool import ClientBorrow, SharedClientPool
from scripts.daemon_models import DaemonServiceError
from scripts.daemon_service import DaemonService
from scripts.daemon_task import DaemonTask
from scripts.private_root import ensure_private_root
from scripts.setup import enable_session
from scripts.state import runtime_path
from tests.daemon_service_fixture import (
    FIRST_SESSION,
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
    from scripts.daemon_client_pool import ClientReference, ClosableAppServer


@pytest.mark.parametrize("cleanup_failure", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("preexisting", [False, True])
def test_start_rollback_finishes_after_task_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: type[BaseException],
    preexisting: bool,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = _service(root, clients, fail_first_resume=True)
    path = runtime_path(root, FIRST_SESSION)
    before: bytes | None = None
    if preexisting:
        ensure_private_root(root)
        _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
        before = path.read_bytes()
        assert (root / ".private-root-v1").is_file()
    original_close = DaemonTask.close

    def close_then_fail(task: DaemonTask) -> None:
        original_close(task)
        if task.session_id == FIRST_SESSION:
            raise cleanup_failure()

    monkeypatch.setattr(DaemonTask, "close", close_then_fail)

    try:
        with pytest.raises(DaemonServiceError) as raised:
            _ = service.start(start_request(FIRST_SESSION, transcript))
        assert raised.value.reason_code == "resume_failed"
        cause = raised.value.__cause__
        assert cause is not None
        assert "daemon_cleanup=task_close" in getattr(cause, "__notes__", ())
        if before is None:
            assert not path.exists()
        else:
            assert path.read_bytes() == before
        assert len(clients) == 1
        assert clients[0].close_count == 1
        service.close()
        assert clients[0].close_count == 1
    finally:
        if not clients or not clients[0].closed:
            monkeypatch.setattr(DaemonTask, "close", original_close)
            service.close()


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "seam",
    ["before", "internal_after_promotion", "after_local_install"],
)
@pytest.mark.parametrize("preexisting", [False, True])
def test_publication_failure_rolls_back_index_reference_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    seam: str,
    preexisting: bool,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = _service(root, clients, fail_first_resume=False)
    path = runtime_path(root, FIRST_SESSION)
    before: bytes | None = None
    if preexisting:
        ensure_private_root(root)
        _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
        before = path.read_bytes()
        assert (root / ".private-root-v1").is_file()
    pools: list[SharedClientPool] = []
    _install_commit_failure(monkeypatch, failure_type, seam, pools)
    expected = DaemonServiceError if failure_type is OSError else failure_type

    try:
        with pytest.raises(expected):
            _ = service.start(start_request(FIRST_SESSION, transcript))
        if before is None:
            assert not path.exists()
        else:
            assert path.read_bytes() == before
        assert len(pools) == 1
        assert pools[0].reference_counts() == (0, 0, 0)
        result = service.start(start_request(FIRST_SESSION, transcript))
        assert not result.reused
        assert len(clients) == 2
        assert clients[0].close_count == 1
        assert pools[0].reference_counts() == (0, 1, 0)
        service.close()
        assert [client.close_count for client in clients] == [1, 1]
        assert pools[0].reference_counts() == (0, 0, 0)
    finally:
        for client in clients:
            if not client.closed:
                client.close()


def _install_commit_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    seam: str,
    pools: list[SharedClientPool],
) -> None:
    original_commit = ClientBorrow.commit
    original_pool_commit = SharedClientPool.commit
    original_pool_release = SharedClientPool.release
    commit_calls = 0
    promotion_calls = 0

    def fail_first_commit(borrow: ClientBorrow) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if seam == "before" and commit_calls == 1:
            message = "commit_failed"
            raise failure_type(message)
        original_commit(borrow)
        if seam == "after_local_install" and commit_calls == 1:
            message = "commit_failed"
            raise failure_type(message)

    def fail_after_first_promotion(
        pool: SharedClientPool,
        reference: ClientReference,
    ) -> None:
        nonlocal promotion_calls
        if pool not in pools:
            pools.append(pool)
        original_pool_commit(pool, reference)
        promotion_calls += 1
        if seam == "internal_after_promotion" and promotion_calls == 1:
            message = "commit_failed"
            raise failure_type(message)

    def capture_release(
        pool: SharedClientPool,
        reference: ClientReference,
    ) -> ClosableAppServer | None:
        if pool not in pools:
            pools.append(pool)
        return original_pool_release(pool, reference)

    monkeypatch.setattr(ClientBorrow, "commit", fail_first_commit)
    monkeypatch.setattr(SharedClientPool, "commit", fail_after_first_promotion)
    monkeypatch.setattr(SharedClientPool, "release", capture_release)


def _service(
    root: Path,
    clients: list[FakeAppServer],
    *,
    fail_first_resume: bool,
) -> DaemonService:
    def factory(
        _fingerprint: str,
        listener: Callable[[AppServerActivity], None],
    ) -> FakeAppServer:
        client = FakeAppServer(listener, fail_resume=fail_first_resume and not clients)
        clients.append(client)
        return client

    return DaemonService(
        root=root,
        client_factory=factory,
        fingerprint_provider=lambda: "digest",
    )
