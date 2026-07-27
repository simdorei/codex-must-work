from __future__ import annotations

import threading
from typing import TYPE_CHECKING, final

import pytest

from tests.cmw_process_probe_etw_native import NativeEtwSession
from tests.cmw_process_probe_etw_native_api import NativeEtwError
from tests.cmw_process_probe_models import LossCounters

if TYPE_CHECKING:
    from tests.cmw_process_probe_etw_native_types import EventTraceLogfile, Guid


@final
class FakeEtwApi:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        process_result: int = 0,
    ) -> None:
        self.fail_at = fail_at
        self.process_result = process_result
        self.calls: list[str] = []
        self.release = threading.Event()

    def start(self, name: str) -> int:
        _ = name
        self.calls.append("start")
        self._fail("start")
        return 11

    def enable(self, handle: int, provider: Guid, keyword: int) -> None:
        _ = (handle, provider, keyword)
        self.calls.append("enable")
        if self.fail_at == f"enable-{self.calls.count('enable')}":
            reason = "etw_provider_enable_failed"
            raise NativeEtwError(reason)

    def open(self, logfile: EventTraceLogfile) -> int:
        _ = logfile
        self.calls.append("open")
        self._fail("open")
        return 22

    def process(self, trace_handle: int) -> int:
        _ = trace_handle
        self.calls.append("process")
        _ = self.release.wait(2)
        return self.process_result

    def stop(self, handle: int, name: str) -> LossCounters:
        _ = (handle, name)
        self.calls.append("stop")
        self.release.set()
        self._fail("stop")
        return LossCounters(provider_losses=None)

    def close(self, trace_handle: int) -> None:
        _ = trace_handle
        self.calls.append("close")
        self.release.set()
        self._fail("close")

    def _fail(self, operation: str) -> None:
        if self.fail_at == operation:
            reason = f"etw_{operation}_failed"
            raise NativeEtwError(reason)


@pytest.mark.parametrize(
    ("failure", "expected_calls"),
    [
        ("start", ["start"]),
        ("enable-1", ["start", "enable", "stop"]),
        ("enable-2", ["start", "enable", "enable", "stop"]),
        ("open", ["start", "enable", "enable", "open", "stop"]),
    ],
)
def test_native_start_failure_attempts_exact_owned_cleanup(
    failure: str,
    expected_calls: list[str],
) -> None:
    # Given
    api = FakeEtwApi(fail_at=failure)
    session = NativeEtwSession("test", api=api)

    # When / Then
    with pytest.raises(NativeEtwError):
        session.start()
    assert api.calls == expected_calls


def test_native_stop_returns_final_loss_then_drains_and_closes_once() -> None:
    # Given
    api = FakeEtwApi()
    session = NativeEtwSession("test", api=api)
    session.start()

    # When
    result = session.stop()

    # Then
    assert result.losses == LossCounters(provider_losses=None)
    assert api.calls == [
        "start",
        "enable",
        "enable",
        "open",
        "process",
        "stop",
        "close",
    ]


@pytest.mark.parametrize("failure", ["stop", "close"])
def test_native_stop_preserves_primary_and_closes_consumer(
    failure: str,
) -> None:
    # Given
    api = FakeEtwApi(fail_at=failure)
    session = NativeEtwSession("test", api=api)
    session.start()

    # When / Then
    with pytest.raises(NativeEtwError, match=f"etw_{failure}_failed"):
        _ = session.stop()
    assert api.calls.count("stop") == 1
    assert api.calls.count("close") == 1
