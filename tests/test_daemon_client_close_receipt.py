from __future__ import annotations

import dis
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, cast, final

import pytest

from scripts.daemon_client_close import CloseGate
from scripts.daemon_client_pool import SharedClientPool

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType, FrameType

    from scripts.app_server_protocol import AppServerActivity
    from scripts.daemon_client_pool import ClientFactory


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


class _CodeFunction(Protocol):
    __code__: CodeType


_CLOSE_ATTEMPT_CODE = cast(
    "_CodeFunction",
    getattr(CloseGate, "_close_attempt"),  # noqa: B009
).__code__
_WORKER_CODE = cast(
    "_CodeFunction",
    getattr(CloseGate, "_worker"),  # noqa: B009
).__code__


@final
class _CloseOnlyClient:
    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0
        self.fail_before_effect = False

    def close(self) -> None:
        if self.closed:
            return
        if self.fail_before_effect:
            self.fail_before_effect = False
            reason = "close_before_effect"
            raise OSError(reason)
        self.closed = True
        self.close_count += 1


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_worker_death_after_close_effect_retries_and_drains(
    failure_type: type[BaseException],
) -> None:
    clients: list[_CloseOnlyClient] = []
    pool = _pool(clients)
    borrow = pool.borrow("digest")
    receipt_offset = next(
        instruction.offset
        for instruction in dis.get_instructions(_CLOSE_ATTEMPT_CODE)
        if instruction.opname == "STORE_ATTR" and cast("object", instruction.argval) == "_succeeded"
    )
    injected = threading.Event()
    armed = threading.Event()

    def inject(
        frame: FrameType,
        event: str,
        _payload: _TracePayload,
    ) -> _TraceFunction | None:
        if frame.f_code is _CLOSE_ATTEMPT_CODE:
            frame.f_trace_opcodes = True
            if (
                event == "opcode"
                and frame.f_lasti == receipt_offset
                and armed.is_set()
                and not injected.is_set()
            ):
                injected.set()
                reason = "post_close_pre_receipt"
                raise failure_type(reason)
        return inject

    threading.settrace(inject)
    try:
        assert CloseGate().finish(_CloseOnlyClient())
        armed.set()
        with pytest.raises(failure_type, match="post_close_pre_receipt"):
            borrow.release()
    finally:
        threading.settrace(None)

    assert injected.is_set()
    assert pool.reference_counts() == (0, 0, 1)
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
    pool.release_installed(0)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1
    assert not any(
        thread.name == "daemon-client-close" and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_every_close_attempt_opcode_converges(
    failure_type: type[BaseException],
) -> None:
    for offset in _observed_close_attempt_offsets():
        clients: list[_CloseOnlyClient] = []
        pool = _pool(clients)
        borrow = pool.borrow("digest")
        injected = threading.Event()
        armed = threading.Event()

        def inject(
            frame: FrameType,
            event: str,
            _payload: _TracePayload,
            target_offset: int = offset,
            target_armed: threading.Event = armed,
            target_injected: threading.Event = injected,
        ) -> _TraceFunction | None:
            if frame.f_code is _CLOSE_ATTEMPT_CODE:
                frame.f_trace_opcodes = True
                if (
                    event == "opcode"
                    and frame.f_lasti == target_offset
                    and target_armed.is_set()
                    and not target_injected.is_set()
                ):
                    target_injected.set()
                    reason = f"close_attempt_opcode:{target_offset}"
                    raise failure_type(reason)
            return inject

        threading.settrace(inject)
        try:
            assert CloseGate().finish(_CloseOnlyClient())
            armed.set()
            with suppress(failure_type):
                borrow.release()
        finally:
            threading.settrace(None)

        assert injected.is_set(), str(offset)
        borrow.release()
        pool.release_installed(0)
        assert pool.reference_counts() == (0, 0, 0), str(offset)
        assert clients[0].close_count == 1, str(offset)
        assert not any(
            thread.name == "daemon-client-close" and thread.is_alive()
            for thread in threading.enumerate()
        ), str(offset)


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    ("offset", "fail_close"),
    [(42, False), (176, True)],
    ids=["receipt-before-notify", "error-before-notify"],
)
def test_worker_terminal_handoffs_converge(
    failure_type: type[BaseException],
    offset: int,
    fail_close: bool,
) -> None:
    clients: list[_CloseOnlyClient] = []
    pool = _pool(clients)
    borrow = pool.borrow("digest")
    clients[0].fail_before_effect = fail_close
    injected = threading.Event()
    armed = threading.Event()

    def inject(
        frame: FrameType,
        event: str,
        _payload: _TracePayload,
    ) -> _TraceFunction | None:
        if frame.f_code is _WORKER_CODE:
            frame.f_trace_opcodes = True
            if (
                event == "opcode"
                and frame.f_lasti == offset
                and armed.is_set()
                and not injected.is_set()
            ):
                injected.set()
                reason = f"worker_handoff:{offset}"
                raise failure_type(reason)
        return inject

    threading.settrace(inject)
    try:
        assert CloseGate().finish(_CloseOnlyClient())
        armed.set()
        with suppress(failure_type):
            borrow.release()
    finally:
        threading.settrace(None)

    assert injected.is_set()
    borrow.release()
    pool.release_installed(0)
    assert pool.reference_counts() == (0, 0, 0)
    assert clients[0].close_count == 1


def _observed_close_attempt_offsets() -> tuple[int, ...]:
    offsets: list[int] = []

    def record(
        frame: FrameType,
        event: str,
        _payload: _TracePayload,
    ) -> _TraceFunction | None:
        if frame.f_code is _CLOSE_ATTEMPT_CODE:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti not in offsets:
                offsets.append(frame.f_lasti)
        return record

    threading.settrace(record)
    try:
        assert CloseGate().finish(_CloseOnlyClient())
        if not offsets:
            assert CloseGate().finish(_CloseOnlyClient())
    finally:
        threading.settrace(None)
    assert offsets
    return tuple(offsets)


def _pool(clients: list[_CloseOnlyClient]) -> SharedClientPool:
    def factory(
        _fingerprint: str,
        _listener: Callable[[AppServerActivity], None],
    ) -> _CloseOnlyClient:
        client = _CloseOnlyClient()
        clients.append(client)
        return client

    return SharedClientPool(
        cast("ClientFactory", factory),
        lambda _activity: None,
        threading.RLock(),
    )
