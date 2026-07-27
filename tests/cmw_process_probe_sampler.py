"""Exact-PID resource sampling without WMI or process-name discovery."""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, final

if TYPE_CHECKING:
    from collections.abc import Callable

from scripts.daemon_control_endpoint_identity import process_created_ns
from tests.cmw_process_probe_events import ProcessIdentity
from tests.cmw_process_probe_models import ProcessSample

_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_TH32CS_SNAPTHREAD: Final = 0x00000004
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value


@final
class ProcessSampleError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _FileTimeLike(Protocol):
    low: int
    high: int


@final
class _FileTime(ctypes.Structure):
    low: int = 0
    high: int = 0
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


@final
class _ThreadEntry(ctypes.Structure):
    owner_pid: int = 0
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("owner_pid", ctypes.c_uint32),
        ("base_priority", ctypes.c_int32),
        ("delta_priority", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


def sample_process(pid: int, heartbeat_monotonic: float | None = None) -> ProcessSample:
    """Sample one exact PID and reject reuse across the measurement call."""
    before = process_created_ns(pid)
    if os.name == "nt":
        cpu_seconds, handles, threads = _sample_windows(pid)
    else:
        cpu_seconds, handles, threads = _sample_proc(pid)
    after = process_created_ns(pid)
    if before != after:
        reason = "process_identity_reused"
        raise ProcessSampleError(reason)
    return ProcessSample(
        identity=ProcessIdentity(pid, before),
        cpu_seconds=cpu_seconds,
        handle_count=handles,
        thread_count=threads,
        heartbeat_monotonic=(
            time.monotonic() if heartbeat_monotonic is None else heartbeat_monotonic
        ),
        child_spawn_counter=0,
    )


def _sample_proc(pid: int) -> tuple[float, int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
    ticks = os.sysconf("SC_CLK_TCK")
    cpu_seconds = (int(fields[13]) + int(fields[14])) / ticks
    handles = sum(1 for _entry in Path(f"/proc/{pid}/fd").iterdir())
    return cpu_seconds, handles, int(fields[19])


def _sample_windows(pid: int) -> tuple[float, int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = cast("Callable[[int, int, int], int]", kernel32.OpenProcess)
    close_handle = cast("Callable[[int], int]", kernel32.CloseHandle)
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        reason = "process_sample_unavailable"
        raise ProcessSampleError(reason)
    creation, exit_time, kernel, user = (_FileTime() for _index in range(4))
    handle_count = ctypes.c_uint32()
    try:
        get_times = cast("Callable[..., int]", kernel32.GetProcessTimes)
        get_handles = cast("Callable[..., int]", kernel32.GetProcessHandleCount)
        ok = get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ) and get_handles(handle, ctypes.byref(handle_count))
    finally:
        _ = close_handle(handle)
    if not ok:
        reason = "process_sample_unavailable"
        raise ProcessSampleError(reason)
    cpu_100ns = _filetime_value(kernel) + _filetime_value(user)
    return cpu_100ns / 10_000_000, int(handle_count.value), _windows_thread_count(pid)


def _windows_thread_count(pid: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = cast(
        "Callable[[int, int], int]",
        kernel32.CreateToolhelp32Snapshot,
    )
    first = cast("Callable[..., int]", kernel32.Thread32First)
    next_entry = cast("Callable[..., int]", kernel32.Thread32Next)
    close_handle = cast("Callable[[int], int]", kernel32.CloseHandle)
    snapshot = create_snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        reason = "thread_snapshot_unavailable"
        raise ProcessSampleError(reason)
    entry = _ThreadEntry(size=ctypes.sizeof(_ThreadEntry))
    count = 0
    try:
        available = first(snapshot, ctypes.byref(entry))
        while available:
            if entry.owner_pid == pid:
                count += 1
            available = next_entry(snapshot, ctypes.byref(entry))
    finally:
        _ = close_handle(snapshot)
    return count


def _filetime_value(value: _FileTimeLike) -> int:
    return (int(value.high) << 32) | int(value.low)
