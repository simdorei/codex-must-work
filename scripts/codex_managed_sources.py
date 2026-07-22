"""Resolve host-managed Codex sources without trusting process environment."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Final, Never, Protocol, cast, final

from scripts.install_errors import InstallPluginError

_PROGRAM_DATA: Final = uuid.UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97")
_PREFERENCES_DOMAIN: Final = "com.openai.codex"
_UNVERIFIABLE: Final = "managed_hook_policy_unverifiable"
_RUN_COMMAND = subprocess.run


@final
class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    )


class _Function(Protocol):
    def __call__(self, *arguments: object) -> int | None: ...


def platform_name() -> str:
    """Return the policy platform family used by pinned Codex loaders."""
    if os.name == "nt":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def windows_program_data() -> Path:
    """Resolve ProgramData through the Windows Known Folder API."""
    try:
        raw = _PROGRAM_DATA.bytes_le
        guid = _Guid.from_buffer_copy(raw)
        output = ctypes.c_wchar_p()
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        known_folder = shell.SHGetKnownFolderPath
        known_folder.restype = ctypes.c_long
        call = cast("_Function", known_folder)
        result: int | None = call(
            ctypes.byref(guid),
            ctypes.c_uint32(0),
            ctypes.c_void_p(),
            ctypes.byref(output),
        )
        if result != 0 or not output.value:
            _fail()
        path = Path(output.value).absolute()
        ctypes.OleDLL("ole32").CoTaskMemFree(output)
    except (AttributeError, OSError, TypeError, ValueError):
        _fail()
    if not path.is_absolute():
        _fail()
    return path


def managed_preference(key: str) -> str | None:
    """Read one bounded Codex CFPreferences key through the system tool."""
    if platform_name() != "darwin" or key not in {
        "config_toml_base64",
        "requirements_toml_base64",
    }:
        _fail()
    try:
        result = _RUN_COMMAND(
            ("/usr/bin/defaults", "read", _PREFERENCES_DOMAIN, key),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise InstallPluginError(_UNVERIFIABLE) from error
    if result.returncode == 0 and not result.stderr:
        return result.stdout.rstrip("\r\n")
    missing = result.returncode == 1 and any(
        phrase in result.stderr.lower() for phrase in ("does not exist", "not found")
    )
    if missing:
        return None
    return _fail()


def _fail() -> Never:
    raise InstallPluginError(_UNVERIFIABLE)
