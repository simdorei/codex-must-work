from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Protocol, override

import pytest

from scripts.daemon_client_close import CloseGate
from scripts.daemon_client_pool import ClientBorrow, SharedClientPool
from tests.daemon_service_fixture import FakeAppServer

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType, FrameType

    from scripts.app_server_protocol import AppServerActivity


class _TracePayload(Protocol):
    pass


class _TraceFunction(Protocol):
    def __call__(
        self,
        frame: FrameType,
        event: str,
        payload: _TracePayload,
        /,
    ) -> _TraceFunction | None: ...


@dataclass(frozen=True, slots=True)
class _TracePoint:
    code: CodeType
    line: int

    @override
    def __str__(self) -> str:
        return f"{self.code.co_name}:{self.line}"


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_borrow_converges_after_every_line_cancellation(
    failure_type: type[BaseException],
) -> None:
    baseline_pool, baseline_clients = _pool()
    baseline: list[ClientBorrow] = []
    points = _transition_lines(
        lambda: baseline.append(baseline_pool.borrow("digest")),
        (SharedClientPool.borrow.__code__,),
    )
    baseline[0].release()
    assert baseline_clients[0].close_count == 1

    for point in points:
        pool, clients = _pool()
        borrowed: list[ClientBorrow] = []
        try:
            _inject(point, failure_type, partial(_borrow_into, pool, borrowed))
            if borrowed:
                borrowed[0].release()
            else:
                pool.release_installed(0)
            assert pool.reference_counts() == (0, 0, 0), str(point)
            _retry_lifecycle(pool)
            assert all(client.close_count == 1 for client in clients), str(point)
        finally:
            for client in clients:
                if not client.closed:
                    client.close()


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_commit_converges_after_every_line_cancellation(
    failure_type: type[BaseException],
) -> None:
    baseline_pool, baseline_clients = _pool()
    baseline = baseline_pool.borrow("digest")
    points = _transition_lines(baseline.commit, (SharedClientPool.commit.__code__,))
    baseline.release()
    assert baseline_clients[0].close_count == 1

    for point in points:
        pool, clients = _pool()
        borrow = pool.borrow("digest")
        try:
            _inject(point, failure_type, borrow.commit)
            borrow.release()
            assert pool.reference_counts() == (0, 0, 0), str(point)
            _retry_lifecycle(pool)
            assert all(client.close_count == 1 for client in clients), str(point)
        finally:
            for client in clients:
                if not client.closed:
                    client.close()


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_release_and_detach_converge_after_every_line_cancellation(
    failure_type: type[BaseException],
) -> None:
    baseline_pool, baseline_clients = _pool()
    baseline = baseline_pool.borrow("digest")
    points = _transition_lines(
        baseline.release,
        (
            ClientBorrow.release.__code__,
            SharedClientPool.release.__code__,
            CloseGate.finish.__code__,
        ),
        additional_names=frozenset({"_release_active", "_finish_close"}),
    )
    assert baseline_clients[0].close_count == 1

    for point in points:
        pool, clients = _pool()
        borrow = pool.borrow("digest")
        try:
            _inject(point, failure_type, borrow.release)
            borrow.release()
            assert pool.reference_counts() == (0, 0, 0), str(point)
            _retry_lifecycle(pool)
            assert all(client.close_count == 1 for client in clients), str(point)
        finally:
            for client in clients:
                if not client.closed:
                    client.close()


def _transition_lines(
    action: Callable[[], None],
    codes: tuple[CodeType, ...],
    *,
    additional_names: frozenset[str] | None = None,
) -> tuple[_TracePoint, ...]:
    points: list[_TracePoint] = []
    source = SharedClientPool.release.__code__.co_filename
    names: frozenset[str] = frozenset() if additional_names is None else additional_names

    def record(
        frame: FrameType,
        event: str,
        _payload: _TracePayload,
    ) -> _TraceFunction | None:
        if event == "line" and (
            frame.f_code in codes
            or (frame.f_code.co_filename == source and frame.f_code.co_name in names)
        ):
            point = _TracePoint(frame.f_code, frame.f_lineno)
            if point not in points:
                points.append(point)
        return record

    try:
        sys.settrace(record)
        action()
    finally:
        sys.settrace(None)
    return tuple(points)


def _inject(
    point: _TracePoint,
    failure_type: type[BaseException],
    action: Callable[[], None],
) -> None:
    injected = False

    def inject(
        frame: FrameType,
        event: str,
        _payload: _TracePayload,
    ) -> _TraceFunction | None:
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is point.code
            and frame.f_lineno == point.line
        ):
            injected = True
            raise failure_type(str(point))
        return inject

    try:
        sys.settrace(inject)
        with pytest.raises(failure_type):
            action()
    finally:
        sys.settrace(None)
    assert injected, str(point)


def _borrow_into(pool: SharedClientPool, borrowed: list[ClientBorrow]) -> None:
    borrowed.append(pool.borrow("digest"))


def _retry_lifecycle(pool: SharedClientPool) -> None:
    retry = pool.borrow("digest")
    retry.commit()
    assert pool.reference_counts() == (0, 1, 0)
    retry.release()
    assert pool.reference_counts() == (0, 0, 0)


def _pool() -> tuple[SharedClientPool, list[FakeAppServer]]:
    clients: list[FakeAppServer] = []
    pool = SharedClientPool(
        lambda _fingerprint, listener: _client(clients, listener),
        lambda _activity: None,
        threading.RLock(),
    )
    return pool, clients


def _client(
    clients: list[FakeAppServer],
    listener: Callable[[AppServerActivity], None],
) -> FakeAppServer:
    client = FakeAppServer(listener)
    clients.append(client)
    return client
