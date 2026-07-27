from __future__ import annotations

from typing import TYPE_CHECKING, final

import pytest

from tests.cmw_process_probe_etw_native import NativeEtwResult
from tests.cmw_process_probe_etw_native_api import NativeEtwError
from tests.cmw_process_probe_etw_session import EtwSession, EtwSessionError
from tests.cmw_process_probe_events import AuditEvent, EventKind, ProcessIdentity
from tests.cmw_process_probe_models import LossCounters

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDERS = (
    "Microsoft-Windows-Kernel-Process",
    "Microsoft-Windows-WMI-Activity",
)
_MONITOR = ProcessIdentity(99, 900)
_SENTINEL = ProcessIdentity(55, 550)


def _native_result(*, include_sentinel: bool = True) -> NativeEtwResult:
    events = (
        (
            AuditEvent(
                EventKind.PROCESS_START,
                1,
                _MONITOR,
                subject=_SENTINEL,
                parent=_MONITOR,
            ),
            AuditEvent(
                EventKind.PROCESS_STOP,
                2,
                _SENTINEL,
                subject=_SENTINEL,
            ),
        )
        if include_sentinel
        else ()
    )
    return NativeEtwResult(
        LossCounters(provider_losses=None),
        events,
        _PROVIDERS,
        records_seen=len(events),
        provider_records=(("Microsoft-Windows-Kernel-Process", len(events)),),
    )


@final
class FakeNative:
    def __init__(
        self,
        *,
        start_error: NativeEtwError | None = None,
        stop_error: NativeEtwError | None = None,
        result: NativeEtwResult | None = None,
    ) -> None:
        self.start_error = start_error
        self.stop_error = stop_error
        self.result = result or _native_result()
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> NativeEtwResult:
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error
        return self.result


def _sentinel_runner() -> int:
    return _SENTINEL.pid


def test_etw_session_starts_native_consumer_before_returning(tmp_path: Path) -> None:
    # Given
    native = FakeNative()

    def native_factory(name: str) -> FakeNative:
        _ = name
        return native

    session = EtwSession(
        tmp_path,
        native_factory=native_factory,
        sentinel_runner=_sentinel_runner,
    )

    # When
    session.start(_MONITOR)

    # Then
    assert native.calls == ["start"]


@pytest.mark.parametrize(
    "failure",
    [
        NativeEtwError("etw_start_failed"),
        NativeEtwError("etw_provider_enable_failed"),
        NativeEtwError("etw_open_trace_failed"),
    ],
)
def test_etw_session_surfaces_native_startup_failure(
    tmp_path: Path,
    failure: NativeEtwError,
) -> None:
    # Given
    native = FakeNative(start_error=failure)

    def native_factory(name: str) -> FakeNative:
        _ = name
        return native

    session = EtwSession(
        tmp_path,
        native_factory=native_factory,
        sentinel_runner=_sentinel_runner,
    )

    # When / Then
    with pytest.raises(EtwSessionError, match=failure.reason_code):
        session.start(_MONITOR)


def test_etw_session_returns_drained_events_final_loss_and_provider_identity(
    tmp_path: Path,
) -> None:
    # Given
    native = FakeNative()

    def native_factory(name: str) -> FakeNative:
        _ = name
        return native

    session = EtwSession(
        tmp_path,
        native_factory=native_factory,
        sentinel_runner=_sentinel_runner,
    )
    session.start(_MONITOR)

    # When
    trace = session.stop(bootstrap_boundary_ns=2, coverage_end_ns=8)

    # Then
    assert native.calls == ["start", "stop"]
    assert trace.losses == LossCounters(provider_losses=None)
    assert trace.enabled_providers == _PROVIDERS
    assert trace.event_coverage_complete


def test_etw_session_rejects_trace_without_exact_sentinel_start_and_stop(
    tmp_path: Path,
) -> None:
    # Given
    native = FakeNative(result=_native_result(include_sentinel=False))

    def native_factory(name: str) -> FakeNative:
        _ = name
        return native

    session = EtwSession(
        tmp_path,
        native_factory=native_factory,
        sentinel_runner=_sentinel_runner,
    )
    session.start(_MONITOR)

    # When / Then
    with pytest.raises(EtwSessionError, match="etw_sentinel_not_observed"):
        _ = session.stop(bootstrap_boundary_ns=2, coverage_end_ns=8)
