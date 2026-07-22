"""Provide typed, descriptor-bound Windows filesystem primitives."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Final, Never, Protocol, final

try:
    import msvcrt as _msvcrt_module
except ImportError:
    _msvcrt_module = None

_BINARY: Final = getattr(os, "O_BINARY", 0)
_INVALID_HANDLE: Final = ctypes.c_void_p(-1).value
_CREATE: Final = "CreateFileW"
_FINAL_PATH: Final = "GetFinalPathNameByHandleW"
_FLUSH: Final = "FlushFileBuffers"
_GET_HANDLE: Final = "msvcrt.get_osfhandle"
_INHERITABLE: Final = "inheritable Windows descriptor"
_OPEN_HANDLE: Final = "msvcrt.open_osfhandle"
_RENAME: Final = "SetFileInformationByHandle"
_REPLACE: Final = "ReplaceFileW"
_STREAM_END: Final = 38
type _WinArgument = int | ctypes.c_void_p | ctypes.c_wchar_p | None


class _WinFunction(Protocol):
    def __call__(self, *arguments: _WinArgument) -> int | None: ...


class _WindowsFileApi:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.create: _WinFunction = ctypes.WINFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            *(ctypes.c_uint32,) * 2,
            ctypes.c_void_p,
            *(ctypes.c_uint32,) * 2,
            ctypes.c_void_p,
        )(("CreateFileW", kernel))
        self.final_path: _WinFunction = kernel.GetFinalPathNameByHandleW
        self.flush: _WinFunction = kernel.FlushFileBuffers
        self.close: _WinFunction = kernel.CloseHandle
        self.rename: _WinFunction = kernel.SetFileInformationByHandle
        self.first_stream: _WinFunction = ctypes.WINFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )(("FindFirstStreamW", kernel))
        self.next_stream: _WinFunction = kernel.FindNextStreamW
        self.close_stream: _WinFunction = kernel.FindClose
        self.query_file: _WinFunction = ntdll.NtQueryInformationFile
        self.replace: _WinFunction = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            *(ctypes.c_wchar_p,) * 3,
            ctypes.c_uint32,
            *(ctypes.c_void_p,) * 2,
        )(("ReplaceFileW", kernel))


@final
class _StreamData(ctypes.Structure):
    _fields_ = [("size", ctypes.c_longlong), ("name", ctypes.c_wchar * 296)]


def _raise_windows(operation: str) -> Never:
    raise OSError(ctypes.get_last_error(), operation)


def windows_handle(descriptor: int) -> ctypes.c_void_p:
    """Return the native handle for one CRT descriptor."""
    if _msvcrt_module is None:
        _raise_windows(_GET_HANDLE)
    return ctypes.c_void_p(_msvcrt_module.get_osfhandle(descriptor))


def open_windows_path(
    path: Path,
    access: int,
    *,
    attributes: int = 0,
    descriptor_flags: int = os.O_RDONLY,
) -> int:
    """Open one share-safe Windows path as a non-inheritable CRT descriptor."""
    if _msvcrt_module is None:
        _raise_windows(_OPEN_HANDLE)
    api = _WindowsFileApi()
    handle = api.create(ctypes.c_wchar_p(str(path)), access, 7, None, 3, attributes, None)
    if handle in {None, _INVALID_HANDLE} or not isinstance(handle, int):
        _raise_windows(_CREATE)
    try:
        descriptor = _msvcrt_module.open_osfhandle(handle, descriptor_flags | _BINARY)
    except OSError:
        _ = api.close(ctypes.c_void_p(handle))
        raise
    if os.get_inheritable(descriptor):
        os.close(descriptor)
        _raise_windows(_INHERITABLE)
    return descriptor


def final_windows_path(descriptor: int) -> Path:
    """Resolve the normalized final path of an opened Windows descriptor."""
    api = _WindowsFileApi()
    handle = windows_handle(descriptor)
    needed = api.final_path(handle, None, 0, 0)
    if not isinstance(needed, int) or needed == 0:
        _raise_windows(_FINAL_PATH)
    buffer = ctypes.create_unicode_buffer(needed)
    written = api.final_path(handle, ctypes.cast(buffer, ctypes.c_wchar_p), needed, 0)
    if not isinstance(written, int) or written >= needed:
        _raise_windows(_FINAL_PATH)
    return Path(ctypes.wstring_at(buffer).removeprefix("\\\\?\\"))


def is_plain_windows_file(path: Path, descriptor: int) -> bool:
    """Reject named streams and extended attributes on an opened file."""
    api, data = _WindowsFileApi(), _StreamData()
    address = ctypes.c_void_p(ctypes.addressof(data))
    stream = api.first_stream(ctypes.c_wchar_p(str(path)), 0, address, 0)
    if stream in {None, _INVALID_HANDLE}:
        return False
    try:
        following = api.next_stream(ctypes.c_void_p(stream), address)
        offset = ctypes.addressof(data) + ctypes.sizeof(ctypes.c_longlong)
        plain_stream = ctypes.wstring_at(offset) == "::$DATA" and not following
        if not plain_stream or ctypes.get_last_error() != _STREAM_END:
            return False
    finally:
        _ = api.close_stream(ctypes.c_void_p(stream))
    status, ea_size = (ctypes.c_void_p * 2)(), ctypes.c_uint32()
    result = api.query_file(
        windows_handle(descriptor),
        ctypes.c_void_p(ctypes.addressof(status)),
        ctypes.c_void_p(ctypes.addressof(ea_size)),
        ctypes.sizeof(ea_size),
        7,
    )
    return result == 0 and ea_size.value == 0


def rename_windows_file(descriptor: int, target: Path, *, replace: bool) -> None:
    """Rename an opened file, optionally replacing the destination atomically."""
    name = str(target).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(22 + len(name))
    ctypes.c_uint32.from_buffer(buffer).value = replace
    ctypes.c_uint32.from_buffer(buffer, 16).value = len(name)
    _ = ctypes.memmove(ctypes.addressof(buffer) + 20, name, len(name))
    address = ctypes.c_void_p(ctypes.addressof(buffer))
    if not _WindowsFileApi().rename(windows_handle(descriptor), 3, address, len(buffer)):
        _raise_windows(_RENAME)


def replace_windows_file(target: Path, replacement: Path, backup: Path) -> None:
    """Replace an existing file with `ReplaceFileW` and zero flags."""
    replaced = _WindowsFileApi().replace(
        ctypes.c_wchar_p(str(target)),
        ctypes.c_wchar_p(str(replacement)),
        ctypes.c_wchar_p(str(backup)),
        0,
        None,
        None,
    )
    if not replaced:
        _raise_windows(_REPLACE)


def flush_windows_directory(path: Path) -> None:
    """Flush a directory through its opened Windows handle."""
    descriptor = open_windows_path(path, 0xC0000000, attributes=0x02000000)
    try:
        if not _WindowsFileApi().flush(windows_handle(descriptor)):
            _raise_windows(_FLUSH)
    finally:
        os.close(descriptor)


def flush_directory(path: Path) -> None:
    """Flush a directory with the host durability primitive."""
    if os.name == "nt":
        flush_windows_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
