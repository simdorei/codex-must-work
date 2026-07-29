"""Strict ctypes boundary for Windows Job Object process ownership."""

from __future__ import annotations

import _winapi
import ctypes
from ctypes import wintypes
from typing import Final, NoReturn, cast, final

CREATE_SUSPENDED: Final = 0x00000004
CREATE_UNICODE_ENVIRONMENT: Final = 0x00000400
ABORT_EXIT_CODE: Final = 250
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_RESUME_FAILED: Final = 0xFFFFFFFF


@final
class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


@final
class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


@final
class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
_KERNEL32.SetInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
_KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
_KERNEL32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
_KERNEL32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
_KERNEL32.TerminateJobObject.restype = wintypes.BOOL
_KERNEL32.ResumeThread.argtypes = (wintypes.HANDLE,)
_KERNEL32.ResumeThread.restype = wintypes.DWORD


@final
class WindowsJobError(RuntimeError):
    """One exact Windows Job Object API failure."""

    def __init__(self, operation: str, winerror: int) -> None:
        self.operation: str = operation
        self.winerror: int = winerror
        super().__init__(f"{operation} failed: winerror={winerror}")


def create_job() -> int:
    """Create one unnamed Job Object and return its retained handle."""
    job_handle = cast("int", _KERNEL32.CreateJobObjectW(None, None))
    if not job_handle:
        _raise_last_error("CreateJobObjectW")
    return job_handle


def configure_kill_on_close(job_handle: int) -> None:
    """Configure final containment on the already-owned Job handle."""
    information = _JobObjectExtendedLimitInformation()
    basic = cast(
        "_JobObjectBasicLimitInformation",
        information.BasicLimitInformation,
    )
    basic.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = cast(
        "int",
        _KERNEL32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ),
    )
    if not configured:
        _raise_last_error("SetInformationJobObject")


def assign_process_to_job(job_handle: int, process_handle: int) -> None:
    """Assign the retained suspended-process handle to its owned job."""
    if not _KERNEL32.AssignProcessToJobObject(job_handle, process_handle):
        _raise_last_error("AssignProcessToJobObject")


def resume_thread(thread_handle: int) -> None:
    """Resume the retained primary-thread handle after job assignment."""
    if _KERNEL32.ResumeThread(thread_handle) == _RESUME_FAILED:
        _raise_last_error("ResumeThread")


def terminate_job(job_handle: int) -> None:
    """Terminate only processes associated with this retained job handle."""
    if not _KERNEL32.TerminateJobObject(job_handle, ABORT_EXIT_CODE):
        _raise_last_error("TerminateJobObject")


def terminate_process(process_handle: int) -> None:
    """Abort a still-suspended process through its retained identity handle."""
    _winapi.TerminateProcess(process_handle, ABORT_EXIT_CODE)


def close_handle(handle: int) -> None:
    """Close one retained Windows kernel handle."""
    _winapi.CloseHandle(handle)


def _raise_last_error(operation: str) -> NoReturn:
    raise WindowsJobError(operation, ctypes.get_last_error())
