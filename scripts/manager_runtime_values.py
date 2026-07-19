"""Validate internal manager runtime values and paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, override

from scripts.state import CorruptReason, CorruptStateError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from scripts.state_io import JsonValue

_RUNTIME_NAME_LENGTH: Final = 69


@dataclass(frozen=True, slots=True)
class ManagerRuntimeError(RuntimeError):
    """Report invalid or conflicting resident-manager state."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


def fail(reason_code: str) -> Never:
    """Raise one stable manager runtime failure."""
    raise ManagerRuntimeError(reason_code)


def runtime_file(root: Path, name: str) -> Path:
    """Resolve one validated hashed runtime filename."""
    valid = (
        len(name) == _RUNTIME_NAME_LENGTH
        and name.endswith(".json")
        and all(character in "0123456789abcdef" for character in name[:-5])
    )
    if not valid:
        fail("runtime_name_invalid")
    return root / "runtime" / name


def bump_revision(values: dict[str, JsonValue], path: Path) -> None:
    """Increment one validated runtime revision."""
    revision = values.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    values["revision"] = revision + 1


def string_value(values: Mapping[str, JsonValue], key: str, path: Path) -> str:
    """Read one required nonempty string."""
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def optional_string(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
) -> str | None:
    """Read one optional string."""
    value = values.get(key)
    if value is not None and not isinstance(value, str):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def bool_value(values: Mapping[str, JsonValue], key: str, path: Path) -> bool:
    """Read one required strict boolean."""
    value = values.get(key)
    if type(value) is not bool:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def int_value(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
    *,
    minimum: int,
) -> int:
    """Read one integer at or above its minimum."""
    value = values.get(key)
    if type(value) is not int or value < minimum:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def optional_int_value(
    values: Mapping[str, JsonValue],
    key: str,
    path: Path,
    *,
    minimum: int,
) -> int | None:
    """Read one optional integer at or above its minimum."""
    value = values.get(key)
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return value


def require_managed(values: dict[str, JsonValue], path: Path) -> None:
    """Require an enabled managed runtime before mutation."""
    if values.get("enabled") is not True or values.get("managed_mode") is not True:
        fail("managed_mode_not_enabled")
    _ = bool_value(values, "manager_ready", path)
