"""Resolve equivalent local path spellings to one platform identity."""

from __future__ import annotations

import ntpath
import os
from pathlib import Path
from string import ascii_letters
from typing import Final

_WINDOWS_EXTENDED_PREFIX: Final = "\\\\?\\"
_WINDOWS_DEVICE_PREFIX: Final = "\\\\.\\"
_WINDOWS_DRIVE_LENGTH: Final = 2
_WINDOWS_DRIVE_LETTERS: Final = frozenset(ascii_letters)
_WINDOWS_INVALID_FILENAME_CHARACTERS: Final = frozenset('<>:"|?*')
_FIRST_PRINTABLE_CODE_POINT: Final = 32
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class UnsupportedLocalPathError(ValueError):
    """Reject Windows device namespaces that are not absolute local drives."""


def resolve_local_path(raw: str | Path) -> Path:
    """Resolve one local path, allowing only extended absolute drive spellings."""
    value = os.fspath(raw)
    if os.name == "nt":
        if value.startswith("//"):
            raise UnsupportedLocalPathError(value)
        if value.startswith(_WINDOWS_DEVICE_PREFIX):
            raise UnsupportedLocalPathError(value)
        if value.startswith(_WINDOWS_EXTENDED_PREFIX):
            candidate = value[len(_WINDOWS_EXTENDED_PREFIX) :]
            drive, tail = ntpath.splitdrive(candidate)
            if (
                len(drive) != _WINDOWS_DRIVE_LENGTH
                or drive[0] not in _WINDOWS_DRIVE_LETTERS
                or drive[1] != ":"
                or not tail.startswith(("\\", "/"))
            ):
                raise UnsupportedLocalPathError(value)
            _validate_extended_tail(value, tail)
            value = candidate
        elif value.startswith("\\\\"):
            raise UnsupportedLocalPathError(value)
    return Path(value).resolve()


def _validate_extended_tail(value: str, tail: str) -> None:
    if "/" in tail or not tail.startswith("\\"):
        raise UnsupportedLocalPathError(value)
    components = tail[1:].split("\\")
    if not components or any(_unsafe_windows_component(part) for part in components):
        raise UnsupportedLocalPathError(value)


def _unsafe_windows_component(component: str) -> bool:
    if not component or component in {".", ".."} or component.endswith((" ", ".")):
        return True
    if any(
        ord(character) < _FIRST_PRINTABLE_CODE_POINT
        or character in _WINDOWS_INVALID_FILENAME_CHARACTERS
        for character in component
    ):
        return True
    return component.partition(".")[0].upper() in _WINDOWS_RESERVED_NAMES
