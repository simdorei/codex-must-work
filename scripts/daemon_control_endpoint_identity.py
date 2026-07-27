"""Read exact process creation identities without WMI."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast, final

if TYPE_CHECKING:
    from collections.abc import Callable

_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000


class ProcessIdentityError(RuntimeError):
    """Report an unavailable or replaced operating-system process identity."""


@final
class _FileTime(ctypes.Structure):
    low: int = 0
    high: int = 0
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def process_created_ns(pid: int) -> int:
    """Return one PID's stable creation identity without polling discovery."""
    if os.name != "nt":
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return int(fields[21])
    return _windows_process_created_ns(pid)


def current_process_created_ns() -> int:
    """Return the current process creation identity."""
    return process_created_ns(os.getpid())


def _windows_process_created_ns(pid: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = cast("Callable[[int, int, int], int]", kernel32.OpenProcess)
    close_handle = cast("Callable[[int], int]", kernel32.CloseHandle)
    get_times = cast("Callable[..., int]", kernel32.GetProcessTimes)
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        reason = "process_identity_unavailable"
        raise ProcessIdentityError(reason)
    creation, exit_time, kernel, user = (_FileTime() for _ in range(4))
    try:
        ok = get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
    finally:
        _ = close_handle(handle)
    if not ok:
        reason = "process_identity_unavailable"
        raise ProcessIdentityError(reason)
    return ((int(creation.high) << 32) | int(creation.low)) * 100
