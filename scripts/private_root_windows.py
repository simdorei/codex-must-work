"""Apply and verify the Windows ACL for a private state root."""

from __future__ import annotations

import base64
import ctypes
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Protocol, override

from scripts.state_io import StateError

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


def secure_windows_root(root: Path, *, verify: bool) -> None:
    """Apply or verify an owner-only Windows ACL."""
    mode = _AclMode.VERIFY if verify else _AclMode.APPLY
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
            PrivateRootReason.ACL_VERIFY_FAILED if verify else PrivateRootReason.ACL_APPLY_FAILED
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
