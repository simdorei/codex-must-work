"""Secure schema-v1 persistence for Codex Must Work state."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol, override

from scripts.state_io import (
    ExclusiveWriteLock,
    JsonValue,
    StateError,
    StateLockTimeoutError,
    UnsafeStatePathError,
    atomic_json_write,
    ensure_direct_regular_file,
    ensure_existing_components_are_direct,
    prepare_parent_directories,
    safe_absolute_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = (
    "SCHEMA_VERSION",
    "CorruptReason",
    "CorruptStateError",
    "FutureSchemaError",
    "JsonValue",
    "StateDocument",
    "StateError",
    "StateLockTimeoutError",
    "UnsafeStatePathError",
    "config_path",
    "cursor_path",
    "load_state",
    "mutate_existing_state",
    "runtime_path",
    "save_state",
    "state_root",
)

SCHEMA_VERSION: Final = 1


class _JsonLoader(Protocol):
    def __call__(
        self,
        s: str,
        *,
        parse_constant: Callable[[str], Never],
    ) -> JsonValue: ...


def _stdlib_json_loader() -> _JsonLoader:
    return json.loads


_JSON_LOAD: Final = _stdlib_json_loader()


class CorruptReason(StrEnum):
    """Stable categories for malformed persisted state."""

    INVALID_JSON = "invalid_json"
    INVALID_TOP_LEVEL = "invalid_top_level"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_VALUE = "invalid_value"
    RESERVED_SCHEMA_FIELD = "reserved_schema_field"


@dataclass(frozen=True, slots=True)
class CorruptStateError(StateError):
    """Report persisted state that cannot be safely interpreted."""

    path: Path
    reason: CorruptReason

    @override
    def __str__(self) -> str:
        return f"corrupt state at {self.path}: {self.reason.value}"


@dataclass(frozen=True, slots=True)
class FutureSchemaError(StateError):
    """Protect state written by a newer plugin from being overwritten."""

    path: Path
    found: int
    supported: int = SCHEMA_VERSION

    @override
    def __str__(self) -> str:
        return f"future state schema at {self.path}: found={self.found}, supported={self.supported}"


@dataclass(frozen=True, slots=True)
class StateDocument:
    """A schema-tagged JSON state payload."""

    values: Mapping[str, JsonValue]
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _NonFiniteJsonError(ValueError):
    token: str

    @override
    def __str__(self) -> str:
        return f"non-finite JSON number: {self.token}"


def state_root() -> Path:
    """Return the plugin root below CODEX_HOME or the default Codex home."""
    configured = os.environ.get("CODEX_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return base / "codex-must-work"


def config_path(root: Path) -> Path:
    """Return the persistent configuration path."""
    return root / "config.json"


def runtime_path(root: Path, session_id: str) -> Path:
    """Return a runtime path whose filename hashes the opaque session ID."""
    return root / "runtime" / _session_filename(session_id)


def cursor_path(root: Path, session_id: str) -> Path:
    """Return a cursor path whose filename hashes the opaque session ID."""
    return root / "cursors" / _session_filename(session_id)


def load_state(root: Path, path: Path) -> StateDocument:
    """Load schema-v1 state after containment and corruption checks."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    ensure_direct_regular_file(root_absolute, path_absolute)
    try:
        decoded = _JSON_LOAD(
            path_absolute.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, _NonFiniteJsonError) as error:
        raise CorruptStateError(path_absolute, CorruptReason.INVALID_JSON) from error

    if type(decoded) is not dict:
        raise CorruptStateError(path_absolute, CorruptReason.INVALID_TOP_LEVEL) from None
    values = decoded
    schema_value = values.get("schema_version")

    if type(schema_value) is not int:
        raise CorruptStateError(path_absolute, CorruptReason.INVALID_SCHEMA)
    _check_schema(path_absolute, schema_value)
    payload: dict[str, JsonValue] = {
        key: value for key, value in values.items() if key != "schema_version"
    }
    ensure_existing_components_are_direct(root_absolute, path_absolute)
    return StateDocument(values=payload)


def save_state(root: Path, path: Path, document: StateDocument) -> None:
    """Atomically save state without replacing corrupt or future state."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    _check_schema(path_absolute, document.schema_version)
    if "schema_version" in document.values:
        raise CorruptStateError(path_absolute, CorruptReason.RESERVED_SCHEMA_FIELD)

    prepare_parent_directories(root_absolute, path_absolute)
    with ExclusiveWriteLock(path_absolute):
        ensure_existing_components_are_direct(root_absolute, path_absolute)
        if path_absolute.exists():
            if not path_absolute.is_file():
                raise CorruptStateError(path_absolute, CorruptReason.INVALID_VALUE)
            _ = load_state(root_absolute, path_absolute)
        try:
            atomic_json_write(
                path_absolute,
                schema_version=document.schema_version,
                values=document.values,
            )
        except (TypeError, ValueError) as error:
            raise CorruptStateError(path_absolute, CorruptReason.INVALID_VALUE) from error


def mutate_existing_state[Result](
    root: Path,
    path: Path,
    mutator: Callable[[dict[str, JsonValue]], Result],
    *,
    after_commit: Callable[[], None] | None = None,
) -> Result | None:
    """Mutate state, then run an optional commit step under the same lock."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    if not path_absolute.is_file():
        return None
    with ExclusiveWriteLock(path_absolute):
        if not path_absolute.is_file():
            return None
        values = dict(load_state(root_absolute, path_absolute).values)
        original = deepcopy(values)
        result = mutator(values)
        if values != original:
            try:
                atomic_json_write(
                    path_absolute,
                    schema_version=SCHEMA_VERSION,
                    values=values,
                )
            except (TypeError, ValueError) as error:
                raise CorruptStateError(path_absolute, CorruptReason.INVALID_VALUE) from error
        if after_commit is not None:
            after_commit()
    return result


def _session_filename(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"


def _check_schema(path: Path, schema_version: int) -> None:
    if schema_version > SCHEMA_VERSION:
        raise FutureSchemaError(path=path, found=schema_version)
    if schema_version != SCHEMA_VERSION:
        raise CorruptStateError(path=path, reason=CorruptReason.INVALID_SCHEMA)


def _reject_nonfinite(token: str) -> Never:
    raise _NonFiniteJsonError(token=token)
