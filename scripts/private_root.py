"""Protect the plugin state root before any private state is written."""

from __future__ import annotations

import base64
import ctypes
import os
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Protocol, override

from scripts.state_io import StateError

_DIRECTORY_MODE: Final = stat.S_IRWXU
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_MARKER_NAME: Final = ".private-root-v1"
_MARKER_CONTENT: Final = b"private-root-v1\n"
_POWERSHELL_TIMEOUT_SECONDS: Final = 15.0
_SYSTEM_DIRECTORY_BUFFER_CHARS: Final = 32_768
_POWERSHELL_SOURCE: Final = r"""
$ErrorActionPreference='Stop'
$path=[Environment]::GetEnvironmentVariable('CODEX_MUST_WORK_PRIVATE_ROOT','Process')
$mode=[Environment]::GetEnvironmentVariable('CODEX_MUST_WORK_ACL_MODE','Process')
if ([String]::IsNullOrWhiteSpace($path)) { exit 20 }
if ($mode -ne 'apply' -and $mode -ne 'verify') { exit 21 }
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$inheritance=[Security.AccessControl.InheritanceFlags]::ContainerInherit `
  -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
if ($mode -eq 'apply') {
  $security=New-Object Security.AccessControl.DirectorySecurity
  $security.SetOwner($sid)
  $security.SetAccessRuleProtection($true,$false)
  $rule=New-Object Security.AccessControl.FileSystemAccessRule(`
    $sid,`
    [Security.AccessControl.FileSystemRights]::FullControl,`
    $inheritance,`
    [Security.AccessControl.PropagationFlags]::None,`
    [Security.AccessControl.AccessControlType]::Allow)
  $security.AddAccessRule($rule)
  [IO.Directory]::SetAccessControl($path,$security)
}
$sections=[Security.AccessControl.AccessControlSections]::Owner `
  -bor [Security.AccessControl.AccessControlSections]::Access
$check=[IO.Directory]::GetAccessControl($path,$sections)
$rules=@($check.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
if (-not $check.AreAccessRulesProtected) { exit 11 }
if ($check.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { exit 12 }
if ($rules.Count -ne 1) { exit 13 }
$actual=$rules[0]
if ($actual.IdentityReference.Value -ne $sid.Value) { exit 14 }
if ($actual.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { exit 15 }
if ($actual.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) { exit 16 }
if ($actual.InheritanceFlags -ne $inheritance) { exit 17 }
if ($actual.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) { exit 18 }
exit 0
""".strip()
_POWERSHELL_COMMAND: Final = base64.b64encode(_POWERSHELL_SOURCE.encode("utf-16le")).decode("ascii")


@unique
class PrivateRootReason(StrEnum):
    """Stable reason codes for fail-closed state-root errors."""

    PATH_UNSAFE = "path_unsafe"
    MIGRATION_REQUIRED = "migration_required"
    SYSTEM_DIRECTORY_UNAVAILABLE = "system_directory_unavailable"
    POWERSHELL_UNAVAILABLE = "powershell_unavailable"
    ACL_APPLY_FAILED = "acl_apply_failed"
    ACL_VERIFY_FAILED = "acl_verify_failed"
    ACL_TIMEOUT = "acl_timeout"


@dataclass(frozen=True, slots=True)
class PrivateRootError(StateError):
    """Report a public-safe state-root security failure."""

    root: Path
    reason: PrivateRootReason
    detail_code: int | None = None

    @override
    def __str__(self) -> str:
        detail = "" if self.detail_code is None else f": detail_code={self.detail_code}"
        return f"private state root rejected: {self.reason.value}: {self.root}{detail}"


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@unique
class _AclMode(StrEnum):
    APPLY = "apply"
    VERIFY = "verify"


class _GetSystemDirectory(Protocol):
    def __call__(
        self,
        buffer: ctypes.Array[ctypes.c_wchar],
        size: int,
        /,
    ) -> int: ...


class _SystemDirectoryApi:
    def __init__(self) -> None:
        self._get: _GetSystemDirectory = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        ).GetSystemDirectoryW

    def read(self, buffer: ctypes.Array[ctypes.c_wchar]) -> int:
        return self._get(buffer, len(buffer))


def ensure_private_root(root: Path) -> None:
    """Create or verify a state root restricted to the current OS user."""
    absolute = Path(os.path.abspath(root))  # noqa: PTH100
    _require_direct_parent(absolute)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        _initialize_root(absolute)
        return
    _require_direct_directory(absolute, metadata)
    marker = absolute / _MARKER_NAME
    try:
        marker_metadata = marker.lstat()
    except FileNotFoundError as error:
        raise PrivateRootError(absolute, PrivateRootReason.MIGRATION_REQUIRED) from error
    if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink != 1:
        raise PrivateRootError(absolute, PrivateRootReason.PATH_UNSAFE)
    _secure_root(absolute, _AclMode.VERIFY)


def _initialize_root(root: Path) -> None:
    root.mkdir(mode=_DIRECTORY_MODE)
    identity = _identity(root.lstat())
    initialized = False
    try:
        _secure_root(root, _AclMode.APPLY)
        try:
            secured_metadata = root.lstat()
        except FileNotFoundError as error:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE) from error
        _require_direct_directory(root, secured_metadata)
        if _identity(secured_metadata) != identity:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE)
        _create_marker(root / _MARKER_NAME)
        initialized = True
    finally:
        if not initialized:
            _remove_empty_same_root(root, identity)


def _secure_root(root: Path, mode: _AclMode) -> None:
    if os.name == "nt":
        _run_windows_acl(root, mode)
        return
    root.chmod(_DIRECTORY_MODE)


def _run_windows_acl(root: Path, mode: _AclMode) -> None:
    system_directory = _windows_system_directory(root)
    powershell = system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_absolute() or not powershell.is_file():
        raise PrivateRootError(root, PrivateRootReason.POWERSHELL_UNAVAILABLE)
    environment = os.environ.copy()
    environment["CODEX_MUST_WORK_PRIVATE_ROOT"] = str(root)
    environment["CODEX_MUST_WORK_ACL_MODE"] = mode.value
    try:
        result = subprocess.run(  # noqa: S603
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                _POWERSHELL_COMMAND,
            ],
            check=False,
            cwd=system_directory,
            env=environment,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_POWERSHELL_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        raise PrivateRootError(root, PrivateRootReason.ACL_TIMEOUT) from error
    except OSError as error:
        raise PrivateRootError(root, PrivateRootReason.POWERSHELL_UNAVAILABLE) from error
    if result.returncode != 0:
        reason = (
            PrivateRootReason.ACL_APPLY_FAILED
            if mode is _AclMode.APPLY
            else PrivateRootReason.ACL_VERIFY_FAILED
        )
        raise PrivateRootError(root, reason, result.returncode)


def _windows_system_directory(root: Path) -> Path:
    buffer = ctypes.create_unicode_buffer(_SYSTEM_DIRECTORY_BUFFER_CHARS)
    try:
        length = _SystemDirectoryApi().read(buffer)
    except OSError as error:
        raise PrivateRootError(root, PrivateRootReason.SYSTEM_DIRECTORY_UNAVAILABLE) from error
    if length == 0 or length >= len(buffer):
        raise PrivateRootError(
            root,
            PrivateRootReason.SYSTEM_DIRECTORY_UNAVAILABLE,
            ctypes.get_last_error(),
        )
    system_directory = Path(ctypes.wstring_at(buffer, length))
    if not system_directory.is_absolute() or not system_directory.is_dir():
        raise PrivateRootError(root, PrivateRootReason.SYSTEM_DIRECTORY_UNAVAILABLE)
    return system_directory


def _require_direct_parent(root: Path) -> None:
    parent = root.parent
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE) from error
        _require_direct_directory(root, metadata)


def _require_direct_directory(root: Path, metadata: os.stat_result) -> None:
    redirected = stat.S_ISLNK(metadata.st_mode) or (
        os.name == "nt" and bool(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )
    if redirected or not stat.S_ISDIR(metadata.st_mode):
        raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE)


def _create_marker(marker: Path) -> None:
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
    identity = _identity(os.fstat(descriptor))
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(_MARKER_CONTENT)
            handle.flush()
            os.fsync(handle.fileno())
        complete = True
    finally:
        if not complete:
            _unlink_same_file(marker, identity)


def _remove_empty_same_root(root: Path, identity: _FileIdentity) -> None:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if _identity(metadata) != identity:
        return
    with suppress(OSError):
        root.rmdir()


def _unlink_same_file(path: Path, identity: _FileIdentity) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _identity(metadata) == identity:
        with suppress(OSError):
            path.unlink()


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(device=metadata.st_dev, inode=metadata.st_ino)
