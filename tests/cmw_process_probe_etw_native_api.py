"""Checked ctypes wrappers over the Windows ETW controller/consumer APIs."""

from __future__ import annotations

import ctypes
from typing import TYPE_CHECKING, Final, Protocol, cast, final

from tests.cmw_process_probe_etw_native_types import (
    EnableTraceParameters,
    EventTraceLogfile,
    EventTraceProperties,
    Guid,
)
from tests.cmw_process_probe_models import LossCounters

if TYPE_CHECKING:
    from collections.abc import Callable

_ERROR_SUCCESS: Final = 0
_EVENT_TRACE_REAL_TIME_MODE: Final = 0x00000100
_EVENT_CONTROL_CODE_ENABLE_PROVIDER: Final = 1
_EVENT_TRACE_CONTROL_STOP: Final = 1
_WNODE_FLAG_TRACED_GUID: Final = 0x00020000
_INVALID_PROCESSTRACE_HANDLE: Final = ctypes.c_uint64(-1).value
_LOGGER_NAME_CHARS: Final = 1_024


@final
class NativeEtwError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class EtwApi(Protocol):
    def start(self, name: str) -> int: ...

    def enable(self, handle: int, provider: Guid, keyword: int) -> None: ...

    def open(self, logfile: EventTraceLogfile) -> int: ...

    def process(self, trace_handle: int) -> int: ...

    def stop(self, handle: int, name: str) -> LossCounters: ...

    def close(self, trace_handle: int) -> None: ...


@final
class WindowsEtwApi:
    """Thin checked wrapper over advapi32 ETW APIs."""

    def __init__(self) -> None:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        start = advapi.StartTraceW
        start.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_wchar_p,
            ctypes.POINTER(EventTraceProperties),
        ]
        start.restype = ctypes.c_uint32
        enable = advapi.EnableTraceEx2
        enable.argtypes = [
            ctypes.c_uint64,
            ctypes.POINTER(Guid),
            ctypes.c_uint32,
            ctypes.c_ubyte,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.POINTER(EnableTraceParameters),
        ]
        enable.restype = ctypes.c_uint32
        open_trace = advapi.OpenTraceW
        open_trace.argtypes = [ctypes.POINTER(EventTraceLogfile)]
        open_trace.restype = ctypes.c_uint64
        process = advapi.ProcessTrace
        process.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        process.restype = ctypes.c_uint32
        control = advapi.ControlTraceW
        control.argtypes = [
            ctypes.c_uint64,
            ctypes.c_wchar_p,
            ctypes.POINTER(EventTraceProperties),
            ctypes.c_uint32,
        ]
        control.restype = ctypes.c_uint32
        close = advapi.CloseTrace
        close.argtypes = [ctypes.c_uint64]
        close.restype = ctypes.c_uint32
        self._start = cast("Callable[..., int]", start)
        self._enable = cast("Callable[..., int]", enable)
        self._open = cast("Callable[..., int]", open_trace)
        self._process = cast("Callable[..., int]", process)
        self._control = cast("Callable[..., int]", control)
        self._close = cast("Callable[[int], int]", close)
        self._properties: dict[int, ctypes.Array[ctypes.c_char]] = {}

    def start(self, name: str) -> int:
        storage, properties = _properties_buffer()
        handle = ctypes.c_uint64()
        result = self._start(ctypes.byref(handle), name, ctypes.byref(properties))
        if result != _ERROR_SUCCESS:
            reason = "etw_start_failed"
            raise NativeEtwError(reason)
        self._properties[int(handle.value)] = storage
        return int(handle.value)

    def enable(self, handle: int, provider: Guid, keyword: int) -> None:
        parameters = EnableTraceParameters(version=2)
        result = self._enable(
            handle,
            ctypes.byref(provider),
            _EVENT_CONTROL_CODE_ENABLE_PROVIDER,
            4,
            keyword,
            0,
            0,
            ctypes.byref(parameters),
        )
        if result != _ERROR_SUCCESS:
            reason = "etw_provider_enable_failed"
            raise NativeEtwError(reason)

    def open(self, logfile: EventTraceLogfile) -> int:
        handle = int(self._open(ctypes.byref(logfile)))
        if handle == _INVALID_PROCESSTRACE_HANDLE:
            reason = "etw_open_trace_failed"
            raise NativeEtwError(reason)
        return handle

    def process(self, trace_handle: int) -> int:
        handles = (ctypes.c_uint64 * 1)(trace_handle)
        return int(self._process(handles, 1, None, None))

    def stop(self, handle: int, name: str) -> LossCounters:
        storage = self._properties.pop(handle, None)
        if storage is None:
            reason = "etw_session_not_started"
            raise NativeEtwError(reason)
        properties = ctypes.cast(
            storage,
            ctypes.POINTER(EventTraceProperties),
        ).contents
        result = self._control(
            handle,
            name,
            ctypes.byref(properties),
            _EVENT_TRACE_CONTROL_STOP,
        )
        if result != _ERROR_SUCCESS:
            reason = "etw_stop_failed"
            raise NativeEtwError(reason)
        return LossCounters(
            events_lost=int(properties.events_lost),
            buffers_lost=(int(properties.log_buffers_lost) + int(properties.realtime_buffers_lost)),
            provider_losses=None,
        )

    def close(self, trace_handle: int) -> None:
        result = self._close(trace_handle)
        if result != _ERROR_SUCCESS:
            reason = "etw_close_trace_failed"
            raise NativeEtwError(reason)


def _properties_buffer() -> tuple[ctypes.Array[ctypes.c_char], EventTraceProperties]:
    properties_size = ctypes.sizeof(EventTraceProperties)
    total_size = properties_size + (_LOGGER_NAME_CHARS * ctypes.sizeof(ctypes.c_wchar))
    storage = ctypes.create_string_buffer(total_size)
    properties = ctypes.cast(storage, ctypes.POINTER(EventTraceProperties)).contents
    properties.wnode.buffer_size = total_size
    properties.wnode.client_context = 2
    properties.wnode.flags = _WNODE_FLAG_TRACED_GUID
    properties.buffer_size = 64
    properties.minimum_buffers = 16
    properties.maximum_buffers = 64
    properties.log_file_mode = _EVENT_TRACE_REAL_TIME_MODE
    properties.flush_timer = 1
    properties.logger_name_offset = properties_size
    return storage, properties
