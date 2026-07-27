from __future__ import annotations

import json
import socket
import threading
import time
from typing import TYPE_CHECKING, override

import pytest

from scripts.daemon_control_endpoint import (
    ControlEndpoint,
    EndpointDependencies,
    EndpointError,
    EndpointLocator,
    control_endpoint_path,
)
from scripts.mcp_server import McpServer
from tests.test_daemon_control_endpoint import FakeDaemon, exchange, request_bytes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _closed_socket(family: int, kind: int) -> socket.socket:
    result = socket.socket(family, kind)
    result.close()
    return result


def test_endpoint_start_failure_closes_listener_and_writes_no_locator(
    tmp_path: Path,
) -> None:
    # Given
    dependencies = EndpointDependencies(
        socket_factory=_closed_socket,
    )
    endpoint = ControlEndpoint(FakeDaemon(), b"k" * 32, tmp_path, McpServer, dependencies)

    # When / Then
    with pytest.raises(OSError, match=r".+"):
        _ = endpoint.start()
    assert not control_endpoint_path(tmp_path).exists()


def test_nonce_interruption_rolls_back_listener_and_locator(tmp_path: Path) -> None:
    # Given
    listeners: list[socket.socket] = []

    def socket_factory(family: int, kind: int) -> socket.socket:
        listener = socket.socket(family, kind)
        listeners.append(listener)
        return listener

    def interrupted_nonce(byte_count: int) -> str:
        _ = byte_count
        raise KeyboardInterrupt

    endpoint = ControlEndpoint(
        FakeDaemon(),
        b"k" * 32,
        tmp_path,
        McpServer,
        EndpointDependencies(
            socket_factory=socket_factory,
            nonce_factory=interrupted_nonce,
        ),
    )

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        _ = endpoint.start()
    assert len(listeners) == 1
    assert listeners[0].fileno() == -1
    assert not control_endpoint_path(tmp_path).exists()
    endpoint.close()


def test_invalid_listener_address_rolls_back_socket_and_locator(tmp_path: Path) -> None:
    # Given
    listeners: list[socket.socket] = []

    class InvalidAddressSocket(socket.socket):
        @override
        def getsockname(self) -> tuple[str]:
            return ("127.0.0.1",)

    def socket_factory(family: int, kind: int) -> socket.socket:
        listener = InvalidAddressSocket(family, kind)
        listeners.append(listener)
        return listener

    endpoint = ControlEndpoint(
        FakeDaemon(),
        b"k" * 32,
        tmp_path,
        McpServer,
        EndpointDependencies(socket_factory=socket_factory),
    )

    # When / Then
    with pytest.raises(EndpointError, match="control_endpoint_start_failed"):
        _ = endpoint.start()
    assert listeners[0].fileno() == -1
    assert not control_endpoint_path(tmp_path).exists()


def test_locator_publication_failure_rolls_back_started_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    endpoint = ControlEndpoint(FakeDaemon(), b"k" * 32, tmp_path, McpServer)

    def fail_publish(_locator: EndpointLocator) -> None:
        reason = "publish failed"
        raise OSError(reason)

    monkeypatch.setattr(endpoint, "_publish", fail_publish)

    # When / Then
    with pytest.raises(OSError, match="publish failed"):
        _ = endpoint.start()
    assert not control_endpoint_path(tmp_path).exists()
    endpoint.close()
    endpoint.close()


@pytest.mark.parametrize("failure", [OSError("thread"), KeyboardInterrupt(), SystemExit(9)])
def test_thread_start_failure_is_atomic_and_close_is_nonthrowing(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    # Given
    listeners: list[socket.socket] = []

    def socket_factory(family: int, kind: int) -> socket.socket:
        listener = socket.socket(family, kind)
        listeners.append(listener)
        return listener

    class FailingThread:
        def start(self) -> None:
            raise failure

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            reason = "unstarted thread must not be joined"
            raise AssertionError(reason)

        def is_alive(self) -> bool:
            return False

    def thread_factory(
        target: Callable[[], None],
        name: str,
        *,
        daemon: bool,
    ) -> FailingThread:
        _ = (target, name, daemon)
        return FailingThread()

    endpoint = ControlEndpoint(
        FakeDaemon(),
        b"k" * 32,
        tmp_path,
        McpServer,
        EndpointDependencies(
            socket_factory=socket_factory,
            thread_factory=thread_factory,
        ),
    )

    # When / Then
    with pytest.raises(type(failure), match=str(failure) or None):
        _ = endpoint.start()
    assert listeners[0].fileno() == -1
    assert not control_endpoint_path(tmp_path).exists()
    endpoint.close()
    endpoint.close()


def test_thread_that_starts_then_raises_is_joined_during_rollback(
    tmp_path: Path,
) -> None:
    # Given
    joined = threading.Event()

    class StartsThenFails:
        def __init__(self, _target: Callable[[], None]) -> None:
            self._started: bool = False

        def start(self) -> None:
            self._started = True
            raise SystemExit(7)

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            joined.set()
            self._started = False

        def is_alive(self) -> bool:
            return self._started

    def thread_factory(
        target: Callable[[], None],
        name: str,
        *,
        daemon: bool,
    ) -> StartsThenFails:
        _ = (name, daemon)
        return StartsThenFails(target)

    endpoint = ControlEndpoint(
        FakeDaemon(),
        b"k" * 32,
        tmp_path,
        McpServer,
        EndpointDependencies(thread_factory=thread_factory),
    )

    # When / Then
    with pytest.raises(SystemExit):
        _ = endpoint.start()
    assert joined.is_set()
    assert not control_endpoint_path(tmp_path).exists()


def test_closed_endpoint_is_one_shot(tmp_path: Path) -> None:
    # Given
    endpoint = ControlEndpoint(FakeDaemon(), b"k" * 32, tmp_path, McpServer)
    _ = endpoint.start()
    endpoint.close()

    # When / Then
    with pytest.raises(EndpointError, match="control_endpoint_closed"):
        _ = endpoint.start()


def test_endpoint_replaces_stale_locator_and_removes_only_own_generation(
    tmp_path: Path,
) -> None:
    # Given
    path = control_endpoint_path(tmp_path)
    _ = path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": 1,
                "process_created_ns": 1,
                "port": 1,
                "endpoint_nonce": "stale",
            }
        ),
        encoding="utf-8",
    )
    endpoint = ControlEndpoint(FakeDaemon(), b"k" * 32, tmp_path, McpServer)

    # When
    locator = endpoint.start()
    replacement = path.read_text(encoding="utf-8").replace(
        locator.endpoint_nonce,
        "replacement-owned-elsewhere",
    )
    _ = path.write_text(replacement, encoding="utf-8")
    endpoint.close()

    # Then
    assert locator.endpoint_nonce != "stale"
    assert path.exists()


def test_slow_client_is_timed_out_before_later_request_is_served(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    endpoint = ControlEndpoint(daemon, b"k" * 32, tmp_path, McpServer)

    # When
    with (
        endpoint as locator,
        socket.create_connection(("127.0.0.1", locator.port), timeout=1.0) as slow,
    ):
        slow.sendall(b'{"jsonrpc":"2.0"')
        started = time.monotonic()
        response = exchange(locator, request_bytes())
        elapsed = time.monotonic() - started

    # Then
    assert "result" in response
    assert 1.5 <= elapsed < 4.0
    assert daemon.calls == ["status"]


def test_unexpected_worker_failure_fails_closed_and_removes_locator(
    tmp_path: Path,
) -> None:
    # Given
    endpoint = ControlEndpoint(
        FakeDaemon(crash_status=True),
        b"k" * 32,
        tmp_path,
        McpServer,
    )
    locator = endpoint.start()

    # When
    with pytest.raises((OSError, AssertionError)):
        _ = exchange(locator, request_bytes())
    _ = threading.Event().wait(0.1)

    # Then
    assert not control_endpoint_path(tmp_path).exists()
    endpoint.close()
