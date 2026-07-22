"""Preserve only filesystem metadata the installer can reproduce exactly.

Custom audit-SACL enterprise configs are unsupported. This non-elevated installer does not
request ``SE_SECURITY_NAME`` or SACL security information and must not be used on a config
whose custom audit SACL must be preserved.
"""

import ctypes
import os
import stat
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Never, Protocol, final

from scripts.install_errors import InstallPluginError
from scripts.installer_lock import (
    FileIdentity,
    InstallerLease,
    file_identity,
    home_lock_key,
    require_live_lease,
)
from scripts.state_io import UnsafeStatePathError, open_direct_file
from scripts.windows_file import is_plain_windows_file, open_windows_path, windows_handle

BASE_SECURITY_INFORMATION: Final = 0x01 | 0x02 | 0x04
LABEL_SECURITY_INFORMATION, ATTRIBUTE_SECURITY_INFORMATION = 0x10, 0x20
_FILE_OBJECT: Final = 1
_UNSUPPORTED, _UNSAFE = "codex_config_metadata_unsupported", "codex_config_unsafe_path"
_APPLY_FAILED: Final = "codex_config_metadata_apply_failed"
_BINARY: Final = getattr(os, "O_BINARY", 0)
type _WinArg = int | ctypes.c_void_p | ctypes.c_wchar_p | None


@dataclass(frozen=True, slots=True)
class PosixMetadata:
    """Supported POSIX metadata."""

    owner: int
    group: int
    mode: int

    def private_creation(self) -> "PosixMetadata":
        """Return metadata for a new current-user-only file."""
        return PosixMetadata(self.owner, self.group, stat.S_IRUSR | stat.S_IWUSR)

    def __call__(self, descriptor: int) -> None:
        """Apply this metadata to an open descriptor."""
        os.fchown(descriptor, self.owner, self.group)
        os.fchmod(descriptor, self.mode)


@dataclass(frozen=True, slots=True)
class WindowsMetadata:
    """Supported non-elevated Windows metadata."""

    base_security: str
    mandatory_label: str | None
    resource_attributes: str | None
    file_attributes: int

    def private_creation(self) -> "WindowsMetadata":
        """Return metadata for a new current-user-only file."""
        owner = self.base_security.partition("O:")[2].partition("G:")[0]
        group = self.base_security.partition("G:")[2].partition("D:")[0]
        if not owner or not group:
            _fail(_UNSUPPORTED)
        private = f"O:{owner}G:{group}D:P(A;;FA;;;{owner})"
        return replace(self, base_security=private)

    def __call__(self, descriptor: int) -> None:
        """Apply this metadata to an open descriptor."""
        _apply_windows(descriptor, self)


type FileMetadata = PosixMetadata | WindowsMetadata


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Exact config state bound to one installer lease."""

    data: bytes
    identity: FileIdentity | None
    metadata: FileMetadata | None
    path: Path
    lease_owner: tuple[int, int]

    @property
    def state(self) -> tuple[bytes, FileIdentity | None, FileMetadata | None]:
        """Return the destination-independent state used for race comparison."""
        return self.data, self.identity, self.metadata


class _IntFunction(Protocol):
    def __call__(self, *arguments: _WinArg) -> int: ...


@final
class _FileBasic(ctypes.Structure):
    _fields_ = [
        *((name, ctypes.c_longlong) for name in ("creation", "access", "write", "change")),
        ("attributes", ctypes.c_uint32),
    ]


class WindowsMetadataApi:
    """Expose typed Windows metadata calls used by exact capture and restore."""

    def __init__(self) -> None:
        """Bind supported security, stream, attribute, and EA functions."""
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.get_security: _IntFunction = advapi.GetSecurityInfo
        self.set_security: _IntFunction = advapi.SetSecurityInfo
        self.get_sacl: _IntFunction = advapi.GetSecurityDescriptorSacl
        self.to_text: _IntFunction = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        self.from_text: _IntFunction = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        self.local_free: _IntFunction = kernel.LocalFree
        self.set_ea: _IntFunction = ntdll.NtSetEaFile
        self.set_file: _IntFunction = kernel.SetFileInformationByHandle
        self.backup_write: _IntFunction = kernel.BackupWrite


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def capture_config_snapshot(path: Path, lease: InstallerLease) -> ConfigSnapshot:
    """Capture exact bytes, identity, and supported metadata under one lease."""
    require_live_lease(lease)
    try:
        named = path.lstat()
    except FileNotFoundError:
        return ConfigSnapshot(b"", None, None, path, lease.owner)
    try:
        descriptor = open_direct_file(path, os.O_RDONLY | _BINARY)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
            opened = os.fstat(handle.fileno())
            metadata = capture_metadata(path, handle.fileno())
    except (OSError, UnsafeStatePathError) as error:
        raise InstallPluginError(_UNSAFE) from error
    if file_identity(named) != file_identity(opened):
        _fail(_UNSAFE)
    return ConfigSnapshot(data, file_identity(opened), metadata, path, lease.owner)


def read_config_bytes(codex_home: Path, lease: InstallerLease) -> ConfigSnapshot:
    """Read exact config bytes under the matching live installer lease."""
    require_live_lease(lease)
    if home_lock_key(codex_home) != lease.home_key:
        _fail("installer_lock_lease_mismatch")
    return capture_config_snapshot(lease.home / "config.toml", lease)


def _address(
    value: ctypes.Structure | ctypes.c_void_p | ctypes.c_int | ctypes.c_uint32 | ctypes.c_wchar_p,
) -> ctypes.c_void_p:
    return ctypes.c_void_p(ctypes.addressof(value))


def open_metadata_descriptor(path: Path, original: int) -> int:
    """Open the same file identity with Windows security-write rights."""
    try:
        descriptor = open_windows_path(path, 0xC00E0000, descriptor_flags=os.O_RDWR)
    except OSError as error:
        raise InstallPluginError(_APPLY_FAILED) from error
    if file_identity(os.fstat(descriptor)) != file_identity(os.fstat(original)):
        os.close(descriptor)
        _fail(_UNSAFE)
    return descriptor


def _require_identity(path: Path, descriptor: int) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as error:
        raise InstallPluginError(_UNSAFE) from error
    if not (
        stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
        and file_identity(opened) == file_identity(named)
    ):
        _fail(_UNSAFE)
    return opened


def read_windows_sddl(api: WindowsMetadataApi, handle: ctypes.c_void_p, flags: int) -> str:
    """Read one supported security subset from an opened Windows handle."""
    descriptor = ctypes.c_void_p()
    result = api.get_security(
        handle, _FILE_OBJECT, flags, None, None, None, None, _address(descriptor)
    )
    if result != 0 or descriptor.value is None:
        _fail(_UNSUPPORTED)
    text = ctypes.c_wchar_p()
    try:
        if not api.to_text(descriptor, 1, flags, _address(text), None):
            _fail(_UNSUPPORTED)
        return text.value or ""
    finally:
        if text.value is not None:
            _ = api.local_free(ctypes.c_void_p.from_buffer(text))
        _ = api.local_free(descriptor)


def _apply_sddl(api: WindowsMetadataApi, handle: ctypes.c_void_p, flags: int, sddl: str) -> None:
    descriptor, length = ctypes.c_void_p(), ctypes.c_uint32()
    if (
        not api.from_text(ctypes.c_wchar_p(sddl), 1, _address(descriptor), _address(length))
        or descriptor.value is None
    ):
        _fail(_APPLY_FAILED)
    try:
        if flags == BASE_SECURITY_INFORMATION:
            payload = struct.pack("<IIqI", 3, 2, length.value, 0) + ctypes.string_at(
                descriptor, length.value
            )
            buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            written, context = ctypes.c_uint32(), ctypes.c_void_p()
            try:
                restored = api.backup_write(
                    handle,
                    ctypes.c_void_p(ctypes.addressof(buffer)),
                    len(payload),
                    _address(written),
                    0,
                    1,
                    _address(context),
                )
                if not restored or written.value != len(payload):
                    _fail(_APPLY_FAILED)
            finally:
                _ = api.backup_write(handle, None, 0, None, 1, 1, _address(context))
            return
        sacl, present, defaulted = ctypes.c_void_p(), ctypes.c_int(), ctypes.c_int()
        if not api.get_sacl(descriptor, _address(present), _address(sacl), _address(defaulted)):
            _fail(_APPLY_FAILED)
        if api.set_security(handle, _FILE_OBJECT, flags, None, None, None, sacl) != 0:
            _fail(_APPLY_FAILED)
    finally:
        _ = api.local_free(descriptor)


def capture_metadata(path: Path, descriptor: int) -> FileMetadata:
    """Capture supported metadata through the already-verified descriptor."""
    opened = _require_identity(path, descriptor)
    if os.name != "nt":
        try:
            attributes = os.listxattr(descriptor)
        except OSError as error:
            raise InstallPluginError(_UNSUPPORTED) from error
        if attributes:
            _fail(_UNSUPPORTED)
        return PosixMetadata(opened.st_uid, opened.st_gid, stat.S_IMODE(opened.st_mode))
    api, handle = WindowsMetadataApi(), windows_handle(descriptor)
    if not is_plain_windows_file(path, descriptor):
        _fail(_UNSUPPORTED)
    base = read_windows_sddl(api, handle, BASE_SECURITY_INFORMATION)
    label = read_windows_sddl(api, handle, LABEL_SECURITY_INFORMATION) or None
    resource = read_windows_sddl(api, handle, ATTRIBUTE_SECURITY_INFORMATION) or None
    _ = _require_identity(path, descriptor)
    return WindowsMetadata(base, label, resource, opened.st_file_attributes)


def private_creation_metadata(metadata: FileMetadata) -> FileMetadata:
    """Derive a destination-stable current-user-only creation policy."""
    return metadata.private_creation()


def _apply_windows(descriptor: int, metadata: WindowsMetadata) -> None:
    api, handle = WindowsMetadataApi(), windows_handle(descriptor)
    _apply_sddl(api, handle, BASE_SECURITY_INFORMATION, metadata.base_security)
    if metadata.mandatory_label is not None:
        _apply_sddl(api, handle, LABEL_SECURITY_INFORMATION, metadata.mandatory_label)
    if metadata.resource_attributes is not None:
        _apply_sddl(api, handle, ATTRIBUTE_SECURITY_INFORMATION, metadata.resource_attributes)
    basic = _FileBasic(attributes=metadata.file_attributes)
    if not api.set_file(handle, 0, _address(basic), ctypes.sizeof(basic)):
        _fail(_APPLY_FAILED)


def apply_metadata(descriptor: int, metadata: FileMetadata) -> None:
    """Apply every captured component through the temporary file handle."""
    try:
        metadata(descriptor)
    except OSError as error:
        raise InstallPluginError(_APPLY_FAILED) from error
