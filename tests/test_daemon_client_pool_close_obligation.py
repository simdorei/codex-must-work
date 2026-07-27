from __future__ import annotations

import threading
from typing import TYPE_CHECKING, final

import pytest

from scripts.daemon_client_pool import ClientReference, SharedClientPool
from scripts.daemon_service import DaemonService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.app_server_protocol import AppServerActivity, JsonObject, TurnOutcome
    from scripts.daemon_client_pool import ClientFactory


@final
class _RetryingClient:
    def __init__(self, listener: Callable[[AppServerActivity], None]) -> None:
        del listener
        self.close_attempts = 0
        self.close_count = 0
        self.closed = False
        self.fail_before_effect = False
        self.fail_after_effect = False
        self._close_lock = threading.Lock()

    @property
    def pending_server_request(self) -> str | None:
        return None

    def start(self) -> None:
        return

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = 10.0,
    ) -> JsonObject:
        del method, params, timeout_seconds
        return {}

    def active_turn(self, thread_id: str) -> str | None:
        del thread_id
        return None

    def turn_completed(self, turn_id: str) -> bool:
        del turn_id
        return False

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        del turn_id
        return None

    def latest_started_turn(self, thread_id: str) -> str | None:
        del thread_id
        return None

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        del thread_id, turn_id, timeout_seconds
        return False

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        del turn_id, timeout_seconds
        return False

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        del thread_id, previous_turn_id, timeout_seconds
        return None

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.close_attempts += 1
            if self.fail_before_effect:
                self.fail_before_effect = False
                reason = "close_before_effect"
                raise OSError(reason)
            self.close_count += 1
            self.closed = True
            if self.fail_after_effect:
                self.fail_after_effect = False
                reason = "close_after_effect"
                raise OSError(reason)


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_release_retries_close_after_explicit_post_pool_handoff_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    clients: list[_RetryingClient] = []
    pool = _pool(clients)
    borrow = pool.borrow("digest")
    original_release = SharedClientPool.release
    injected = False

    def release_then_fail(
        target: SharedClientPool,
        reference: ClientReference,
    ) -> None:
        nonlocal injected
        original_release(target, reference)
        if not injected:
            injected = True
            reason = "post_pool_release_handoff"
            raise failure_type(reason)

    monkeypatch.setattr(SharedClientPool, "release", release_then_fail)

    with pytest.raises(failure_type, match="post_pool_release_handoff"):
        borrow.release()
    borrow.release()

    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1


@pytest.mark.parametrize("failure_seam", ["before_effect", "after_effect"])
def test_close_failure_retains_retryable_obligation(failure_seam: str) -> None:
    clients: list[_RetryingClient] = []
    pool = _pool(clients)
    borrow = pool.borrow("digest")
    client = clients[0]
    if failure_seam == "before_effect":
        client.fail_before_effect = True
    else:
        client.fail_after_effect = True

    with pytest.raises(OSError, match=f"close_{failure_seam}"):
        borrow.release()

    assert pool.reference_counts() == (0, 0, 1)
    assert pool.get_existing() is None
    borrow.release()
    assert pool.reference_counts() == (0, 0, 0)
    assert client.close_count == 1


def test_concurrent_retries_finish_one_close_attempt() -> None:
    clients: list[_RetryingClient] = []
    pool = _pool(clients)
    borrow = pool.borrow("digest")
    clients[0].fail_before_effect = True
    with pytest.raises(OSError, match="close_before_effect"):
        borrow.release()
    barrier = threading.Barrier(9)
    errors: list[BaseException] = []

    def retry() -> None:
        _ = barrier.wait()
        try:
            borrow.release()
        except BaseException as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            errors.append(error)

    threads = [threading.Thread(target=retry) for _ in range(8)]
    for thread in threads:
        thread.start()
    _ = barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_attempts == 2
    assert clients[0].close_count == 1


def test_service_close_drains_preexisting_closing_obligation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[_RetryingClient] = []
    pools: list[SharedClientPool] = []
    original_init = SharedClientPool.__init__

    def capture_pool(
        pool: SharedClientPool,
        factory: ClientFactory,
        activity_listener: Callable[[AppServerActivity], None],
        lock: threading.RLock,
    ) -> None:
        original_init(pool, factory, activity_listener, lock)
        pools.append(pool)

    monkeypatch.setattr(SharedClientPool, "__init__", capture_pool)
    service = DaemonService(
        root=tmp_path,
        client_factory=lambda _fingerprint, listener: _client(clients, listener),
        fingerprint_provider=lambda: "digest",
    )
    borrow = pools[0].borrow("digest")
    clients[0].fail_before_effect = True
    with pytest.raises(OSError, match="close_before_effect"):
        borrow.release()

    service.close()

    assert pools[0].reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1


def _pool(clients: list[_RetryingClient]) -> SharedClientPool:
    def factory(
        _fingerprint: str,
        _listener: Callable[[AppServerActivity], None],
    ) -> _RetryingClient:
        client = _RetryingClient(_listener)
        clients.append(client)
        return client

    return SharedClientPool(factory, lambda _activity: None, threading.RLock())


def _client(
    clients: list[_RetryingClient],
    listener: Callable[[AppServerActivity], None],
) -> _RetryingClient:
    client = _RetryingClient(listener)
    clients.append(client)
    return client
