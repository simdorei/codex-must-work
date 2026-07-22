"""Handle-bound Windows security for immutable cache trees."""

from __future__ import annotations

import ctypes
import os
import stat
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Protocol, final

if TYPE_CHECKING:
    from pathlib import Path

from scripts.cache_types import identity
from scripts.windows_file import final_windows_path, windows_handle

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

_INVALID: Final = ctypes.c_void_p(-1).value
_READ_CONTROL: Final = 0x00020000
_WRITE_DAC: Final = 0x00040000
_WRITE_OWNER: Final = 0x00080000
_READ_ATTRIBUTES: Final = 0x80
_WRITE_ATTRIBUTES: Final = 0x100
_READ_EA: Final = 0x8
_DELETE: Final = 0x00010000
_BACKUP_SEMANTICS: Final = 0x02000000
_OPEN_REPARSE: Final = 0x00200000
_OWNER_DACL: Final = 0x00000001 | 0x00000004
_PROTECTED_DACL: Final = 0x80000000
_NORMAL: Final = 0x80
_DIRECTORY: Final = 0x10
_STREAM_END: Final = 38


class _Function(Protocol):
    def __call__(self, *arguments: object) -> int | None: ...


@final
class _BasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation", ctypes.c_longlong),
        ("access", ctypes.c_longlong),
        ("write", ctypes.c_longlong),
        ("change", ctypes.c_longlong),
        ("attributes", ctypes.c_uint32),
    ]


@final
class _StreamData(ctypes.Structure):
    _fields_ = [("size", ctypes.c_longlong), ("name", ctypes.c_wchar * 296)]


class _Api:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        create = kernel.CreateFileW
        create.restype = ctypes.c_void_p
        self.create: _Function = create
        self.close: _Function = kernel.CloseHandle
        self.get_info: _Function = kernel.GetFileInformationByHandleEx
        self.set_info: _Function = kernel.SetFileInformationByHandle
        first_stream = kernel.FindFirstStreamW
        first_stream.restype = ctypes.c_void_p
        self.first_stream: _Function = first_stream
        self.next_stream: _Function = kernel.FindNextStreamW
        self.close_stream: _Function = kernel.FindClose
        self.query_file: _Function = ntdll.NtQueryInformationFile
        self.get_security: _Function = advapi.GetSecurityInfo
        self.set_security: _Function = advapi.SetSecurityInfo
        self.from_sddl: _Function = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        self.to_sddl: _Function = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        self.sd_owner: _Function = advapi.GetSecurityDescriptorOwner
        self.sd_dacl: _Function = advapi.GetSecurityDescriptorDacl
        self.open_token: _Function = advapi.OpenProcessToken
        self.token_info: _Function = advapi.GetTokenInformation
        self.sid_string: _Function = advapi.ConvertSidToStringSidW
        current_process = kernel.GetCurrentProcess
        current_process.restype = ctypes.c_void_p
        self.current_process: _Function = current_process
        self.local_free: _Function = kernel.LocalFree


def open_locked(
    path: Path,
    *,
    write_security: bool = False,
    delete_access: bool = False,
) -> int:
    """Open a direct path while denying concurrent rename or deletion."""
    if _msvcrt is None:
        message = "Windows descriptor support unavailable"
        raise OSError(message)
    access = _READ_CONTROL | _READ_ATTRIBUTES | _READ_EA
    if write_security:
        access |= _WRITE_DAC | _WRITE_OWNER | _WRITE_ATTRIBUTES
    if delete_access:
        access |= _DELETE
    handle = _Api().create(
        ctypes.c_wchar_p(str(path)), access, 3, None, 3, _BACKUP_SEMANTICS | _OPEN_REPARSE, None
    )
    if handle in {None, _INVALID} or not isinstance(handle, int):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        _ = _Api().close(ctypes.c_void_p(handle))
        raise


def mark_windows_delete(descriptor: int) -> None:
    """Mark the exact opened file or directory for deletion on close."""
    disposition = ctypes.c_int(1)
    if not _Api().set_info(
        windows_handle(descriptor), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def secure_windows_path(path: Path, *, directory: bool, apply: bool) -> bool:
    """Apply or verify the exact current-user Windows cache policy."""
    descriptor = open_locked(path, write_security=apply)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_kind(opened.st_mode) or identity(opened) != identity(named):
            return False
        if not directory and opened.st_nlink != 1:
            return False
        if _normalized(final_windows_path(descriptor)) != _normalized(path):
            return False
        if apply:
            _apply(descriptor, directory)
        return _verify(descriptor, path, directory) and identity(os.fstat(descriptor)) == identity(
            path.lstat()
        )
    finally:
        os.close(descriptor)


def _apply(descriptor: int, directory: bool) -> None:
    api, security = _Api(), ctypes.c_void_p()
    sddl = _expected_sddl(directory)
    if not api.from_sddl(ctypes.c_wchar_p(sddl), 1, ctypes.byref(security), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        present, defaulted = ctypes.c_int(), ctypes.c_int()
        if not api.sd_owner(security, ctypes.byref(owner), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.sd_dacl(
            security, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        result = api.set_security(
            windows_handle(descriptor), 1, _OWNER_DACL | _PROTECTED_DACL, owner, None, dacl, None
        )
        if result:
            raise OSError(result, "SetSecurityInfo")
    finally:
        _ = api.local_free(security)
    info = _basic_info(descriptor)
    info.attributes = _NORMAL
    if not api.set_info(windows_handle(descriptor), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


def _verify(descriptor: int, path: Path, directory: bool) -> bool:
    expected_attributes = _DIRECTORY if directory else _NORMAL
    metadata = os.fstat(descriptor)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        getattr(metadata, "st_file_attributes", 0) == expected_attributes
        and not getattr(metadata, "st_file_attributes", 0) & reparse
        and _security_sddl(descriptor) == _expected_sddl(directory)
        and _plain_streams(path, directory)
        and _empty_ea(descriptor)
    )


def _basic_info(descriptor: int) -> _BasicInfo:
    info = _BasicInfo()
    if not _Api().get_info(windows_handle(descriptor), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _security_sddl(descriptor: int) -> str:
    api, security = _Api(), ctypes.c_void_p()
    result = api.get_security(
        windows_handle(descriptor), 1, _OWNER_DACL, None, None, None, None, ctypes.byref(security)
    )
    if result:
        raise OSError(result, "GetSecurityInfo")
    text, length = ctypes.c_void_p(), ctypes.c_uint32()
    try:
        if not api.to_sddl(security, 1, _OWNER_DACL, ctypes.byref(text), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.wstring_at(text)
    finally:
        if text.value:
            _ = api.local_free(text)
        if security.value:
            _ = api.local_free(security)


@lru_cache(maxsize=1)
def _current_sid() -> str:
    api, token, size = _Api(), ctypes.c_void_p(), ctypes.c_uint32()
    process = api.current_process()
    if not api.open_token(ctypes.c_void_p(process), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _ = api.token_info(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not api.token_info(token, 1, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(buffer).value
        text = ctypes.c_void_p()
        if not api.sid_string(ctypes.c_void_p(sid), ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            _ = api.local_free(text)
    finally:
        _ = api.close(token)


def _expected_sddl(directory: bool) -> str:
    flags = "OICI" if directory else ""
    sid = _current_sid()
    return f"O:{sid}D:PAI(A;{flags};FA;;;{sid})"


def _plain_streams(path: Path, directory: bool) -> bool:
    api, data = _Api(), _StreamData()
    stream = api.first_stream(ctypes.c_wchar_p(str(path)), 0, ctypes.byref(data), 0)
    if stream in {None, _INVALID}:
        return directory and ctypes.get_last_error() == _STREAM_END
    try:
        following = api.next_stream(ctypes.c_void_p(stream), ctypes.byref(data))
        offset = ctypes.addressof(data) + ctypes.sizeof(ctypes.c_longlong)
        stream_name = ctypes.wstring_at(offset)
        return (
            not directory
            and stream_name == "::$DATA"
            and not following
            and ctypes.get_last_error() == _STREAM_END
        )
    finally:
        _ = api.close_stream(ctypes.c_void_p(stream))


def _empty_ea(descriptor: int) -> bool:
    status, size = (ctypes.c_void_p * 2)(), ctypes.c_uint32()
    result = _Api().query_file(
        windows_handle(descriptor), ctypes.byref(status), ctypes.byref(size), ctypes.sizeof(size), 7
    )
    return result == 0 and size.value == 0


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=True)))
