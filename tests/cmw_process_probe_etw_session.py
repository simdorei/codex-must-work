"""One bounded native real-time ETW session owned by the audit monitor."""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, assert_never, final

from tests.cmw_process_probe_etw_native import NativeEtwResult, NativeEtwSession
from tests.cmw_process_probe_etw_native_api import NativeEtwError
from tests.cmw_process_probe_events import EventKind, ProcessIdentity
from tests.cmw_process_probe_models import TraceWindow

if TYPE_CHECKING:
    from tests.cmw_process_probe_events import AuditEvent

_SENTINEL_TIMEOUT_SECONDS: Final = 5.0
_EXPECTED_PROVIDERS: Final = frozenset(
    {
        "Microsoft-Windows-Kernel-Process",
        "Microsoft-Windows-WMI-Activity",
    }
)


class NativeSession(Protocol):
    def start(self) -> None: ...

    def stop(self) -> NativeEtwResult: ...


class NativeSessionFactory(Protocol):
    def __call__(self, name: str) -> NativeSession: ...


class SentinelRunner(Protocol):
    def __call__(self) -> int: ...


def _run_native_sentinel() -> int:
    """Run one short-lived native child and return its PID after exit."""
    with subprocess.Popen(  # noqa: S603 - fixed COMSPEC and constant arguments.
        (str(Path(os.environ["COMSPEC"])), "/d", "/c", "exit", "0"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        pid = process.pid
        try:
            returncode = process.wait(timeout=_SENTINEL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            process.kill()
            _ = process.wait()
            reason = "etw_sentinel_timeout"
            raise EtwSessionError(reason) from error
    if returncode != 0:
        reason = "etw_sentinel_failed"
        raise EtwSessionError(reason)
    return pid


@final
class EtwSessionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@final
class EtwSession:
    """Start both providers before the boundary and drain before returning."""

    def __init__(
        self,
        _directory: Path,
        *,
        native_factory: NativeSessionFactory = NativeEtwSession,
        sentinel_runner: SentinelRunner = _run_native_sentinel,
    ) -> None:
        self.name = f"CMWProbe-{secrets.token_hex(12)}"
        self._native_factory = native_factory
        self._sentinel_runner = sentinel_runner
        self._native: NativeSession | None = None
        self._started_ns: int | None = None
        self._monitor: ProcessIdentity | None = None
        self._sentinel_pid: int | None = None

    def start(self, monitor: ProcessIdentity) -> None:
        """Open the consumer and enable both providers before returning."""
        native = self._native_factory(self.name)
        try:
            native.start()
        except NativeEtwError as error:
            raise EtwSessionError(error.reason_code) from error
        started_ns = time.time_ns()
        try:
            sentinel_pid = self._sentinel_runner()
        except (EtwSessionError, OSError):
            with suppress(NativeEtwError):
                _ = native.stop()
            raise
        self._native = native
        self._started_ns = started_ns
        self._monitor = monitor
        self._sentinel_pid = sentinel_pid

    def stop(self, *, bootstrap_boundary_ns: int, coverage_end_ns: int) -> TraceWindow:
        """Stop the controller, drain ProcessTrace, and expose final counters."""
        started = self._started_ns
        monitor = self._monitor
        native = self._native
        sentinel_pid = self._sentinel_pid
        self._started_ns = None
        self._monitor = None
        self._native = None
        self._sentinel_pid = None
        if started is None or monitor is None or native is None or sentinel_pid is None:
            reason = "etw_session_not_started"
            raise EtwSessionError(reason)
        try:
            result = native.stop()
        except NativeEtwError as error:
            raise EtwSessionError(error.reason_code) from error
        if not _sentinel_observed(result.events, monitor, sentinel_pid):
            reason = "etw_sentinel_not_observed"
            raise EtwSessionError(reason)
        stopped_ns = time.time_ns()
        coverage_complete = (
            frozenset(result.enabled_providers) == _EXPECTED_PROVIDERS
            and result.records_seen > 0
            and result.losses.events_lost == 0
            and result.losses.buffers_lost == 0
        )
        return TraceWindow(
            provider_started_ns=started,
            bootstrap_boundary_ns=bootstrap_boundary_ns,
            coverage_end_ns=coverage_end_ns,
            provider_stopped_ns=stopped_ns,
            monitor=monitor,
            losses=result.losses,
            events=result.events,
            event_coverage_complete=coverage_complete,
            enabled_providers=result.enabled_providers,
            records_seen=result.records_seen,
            provider_records=result.provider_records,
            sentinel_verified=True,
        )


def _sentinel_observed(
    events: tuple[AuditEvent, ...],
    monitor: ProcessIdentity,
    sentinel_pid: int,
) -> bool:
    started: ProcessIdentity | None = None
    stopped: set[ProcessIdentity] = set()
    for event in events:
        if event.kind is EventKind.PROCESS_START:
            if (
                event.parent == monitor
                and event.subject is not None
                and event.subject.pid == sentinel_pid
            ):
                started = event.subject
            continue
        if event.kind is EventKind.PROCESS_STOP:
            if event.subject is not None and event.subject.pid == sentinel_pid:
                stopped.add(event.subject)
            continue
        if event.kind is EventKind.WMI_OPERATION:
            continue
        assert_never(event.kind)
    return started is not None and started in stopped
