"""Own one native real-time ETW controller and EVENT_RECORD consumer."""

from __future__ import annotations

import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from tests.cmw_process_probe_etw_native_api import (
    EtwApi,
    NativeEtwError,
    WindowsEtwApi,
)
from tests.cmw_process_probe_etw_native_tdh import EventRecordDecoder
from tests.cmw_process_probe_etw_native_types import (
    EventRecordCallback,
    EventRecordPointer,
    EventTraceLogfile,
    Guid,
)

if TYPE_CHECKING:
    from tests.cmw_process_probe_events import AuditEvent
    from tests.cmw_process_probe_models import LossCounters

_PROCESS_PROVIDER: Final = Guid.parse("22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716")
_WMI_PROVIDER: Final = Guid.parse("1418ef04-b0b4-4623-bf7e-d74ab47bbdaa")
_PROVIDERS: Final = (
    ("Microsoft-Windows-Kernel-Process", _PROCESS_PROVIDER, 0x10),
    ("Microsoft-Windows-WMI-Activity", _WMI_PROVIDER, 0x8000000000000000),
)
_PROVIDER_NAMES: Final = {provider.key(): name for name, provider, _keyword in _PROVIDERS}
_ERROR_SUCCESS: Final = 0
_PROCESS_TRACE_MODE_REAL_TIME: Final = 0x00000100
_PROCESS_TRACE_MODE_EVENT_RECORD: Final = 0x10000000
_JOIN_TIMEOUT_SECONDS: Final = 5.0
_START_TIMEOUT_SECONDS: Final = 2.0
_NATIVE_FAILURES: Final = (Exception, KeyboardInterrupt, SystemExit, GeneratorExit)


@dataclass(frozen=True, slots=True)
class NativeEtwResult:
    losses: LossCounters
    events: tuple[AuditEvent, ...]
    enabled_providers: tuple[str, ...]
    records_seen: int = 0
    provider_records: tuple[tuple[str, int], ...] = ()


@final
class NativeEtwSession:
    """Pair one controller generation with one draining consumer thread."""

    def __init__(
        self,
        name: str,
        *,
        api: EtwApi | None = None,
        decoder: EventRecordDecoder | None = None,
    ) -> None:
        self._name = name
        self._api = api or WindowsEtwApi()
        self._decoder = decoder or EventRecordDecoder()
        self._session_handle: int | None = None
        self._trace_handle: int | None = None
        self._thread: threading.Thread | None = None
        self._events: list[AuditEvent] = []
        self._enabled: list[str] = []
        self._provider_records = {name: 0 for name, _provider, _keyword in _PROVIDERS}
        self._consumer_error: str | None = None
        self._consumer_ready = threading.Event()
        self._callback = EventRecordCallback(self._on_event)
        self._logfile = EventTraceLogfile()

    def start(self) -> None:
        try:
            self._session_handle = self._api.start(self._name)
            for provider_name, provider, keyword in _PROVIDERS:
                self._api.enable(self._session_handle, provider, keyword)
                self._enabled.append(provider_name)
            self._logfile.logger_name = self._name
            self._logfile.process_trace_mode = (
                _PROCESS_TRACE_MODE_REAL_TIME | _PROCESS_TRACE_MODE_EVENT_RECORD
            )
            self._logfile.event_record_callback = self._callback
            self._trace_handle = self._api.open(self._logfile)
            self._thread = threading.Thread(
                target=self._consume,
                name="cmw-etw-consumer",
                daemon=False,
            )
            self._thread.start()
            if not self._consumer_ready.wait(_START_TIMEOUT_SECONDS) or not self._thread.is_alive():
                reason = "etw_consumer_start_failed"
                raise NativeEtwError(reason)
        except _NATIVE_FAILURES:
            self._cleanup_failed_start()
            raise

    def stop(self) -> NativeEtwResult:
        session_handle = self._session_handle
        if session_handle is None:
            reason = "etw_session_not_started"
            raise NativeEtwError(reason)
        self._session_handle = None
        primary: BaseException | None = None
        losses: LossCounters | None = None
        try:
            losses = self._api.stop(session_handle, self._name)
        except _NATIVE_FAILURES as error:
            primary = error
        close_error = self._finish_consumer()
        if primary is not None:
            raise primary
        if close_error is not None:
            raise close_error
        if self._consumer_error is not None:
            raise NativeEtwError(self._consumer_error)
        if losses is None:
            reason = "etw_loss_counters_unavailable"
            raise NativeEtwError(reason)
        provider_records = tuple(self._provider_records.items())
        return NativeEtwResult(
            losses,
            tuple(self._events),
            tuple(self._enabled),
            records_seen=sum(self._provider_records.values()),
            provider_records=provider_records,
        )

    def _consume(self) -> None:
        trace_handle = self._trace_handle
        if trace_handle is None:
            return
        self._consumer_ready.set()
        result = self._api.process(trace_handle)
        if result != _ERROR_SUCCESS:
            self._consumer_error = "etw_process_trace_failed"

    def _on_event(self, record: EventRecordPointer) -> None:
        provider_name = _PROVIDER_NAMES.get(record.contents.header.provider_id.key())
        if provider_name is not None:
            self._provider_records[provider_name] += 1
        try:
            event = self._decoder.decode(record)
        except (OSError, RuntimeError, ValueError):
            self._consumer_error = "etw_event_decode_failed"
            return
        if event is not None:
            self._events.append(event)

    def _finish_consumer(self) -> BaseException | None:
        trace_handle = self._trace_handle
        self._trace_handle = None
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(_JOIN_TIMEOUT_SECONDS)
        close_error: BaseException | None = None
        if trace_handle is not None:
            try:
                self._api.close(trace_handle)
            except _NATIVE_FAILURES as error:
                close_error = error
        if thread is not None and thread.is_alive():
            return NativeEtwError("etw_consumer_did_not_stop")
        return close_error

    def _cleanup_failed_start(self) -> None:
        _ = self._finish_consumer()
        session_handle = self._session_handle
        self._session_handle = None
        if session_handle is not None:
            with suppress(*_NATIVE_FAILURES):
                _ = self._api.stop(session_handle, self._name)
