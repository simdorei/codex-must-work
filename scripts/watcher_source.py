"""Bounded incremental reads for one session rollout."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Never, NewType, Protocol, override

from scripts.state import (
    CorruptReason,
    CorruptStateError,
    StateDocument,
    cursor_path,
    load_state,
    save_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.state_io import JsonValue

DeviceId = NewType("DeviceId", int)
FileId = NewType("FileId", int)
ByteOffset = NewType("ByteOffset", int)
_MAX_LINE_BYTES: Final = 8 * 1_048_576
_MAX_BATCH_BYTES: Final = 8 * 1_048_576
_MAX_BATCH_RECORDS: Final = 512


class _JsonLoader(Protocol):
    def __call__(
        self,
        s: str,
        *,
        parse_constant: Callable[[str], Never],
    ) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class RolloutCursor:
    """Stable file identity plus the next unread byte."""

    device: DeviceId
    file_id: FileId
    offset: ByteOffset


@dataclass(frozen=True, slots=True)
class RecordBatch:
    """Decoded top-level records and the cursor after complete lines."""

    records: tuple[dict[str, JsonValue], ...]
    cursor: RolloutCursor


@dataclass(frozen=True, slots=True)
class RolloutCorruptError(ValueError):
    """Report unreadable rollout framing without including source text."""

    path: Path
    reason_code: str

    @override
    def __str__(self) -> str:
        return f"rollout unreadable at {self.path}: {self.reason_code}"


@dataclass(frozen=True, slots=True)
class RolloutRotatedError(OSError):
    """Report an identity or size change that invalidates a cursor."""

    path: Path

    @override
    def __str__(self) -> str:
        return f"rollout cursor invalidated at {self.path}"


@dataclass(frozen=True, slots=True)
class UnsafeRolloutPathError(OSError):
    """Reject rollout paths outside CODEX_HOME or behind a redirect."""

    path: Path

    @override
    def __str__(self) -> str:
        return f"unsafe rollout path: {self.path}"


def resolve_rollout_path(root: Path, stored_relative: str) -> Path:
    """Resolve a persisted POSIX path while enforcing CODEX_HOME containment."""
    codex_home = Path(os.path.abspath(root.parent))  # noqa: PTH100
    relative = PurePosixPath(stored_relative)
    if relative.is_absolute():
        raise UnsafeRolloutPathError(Path(stored_relative))
    candidate = codex_home.joinpath(*relative.parts)
    absolute = Path(os.path.abspath(candidate))  # noqa: PTH100
    if absolute == codex_home or not absolute.is_relative_to(codex_home):
        raise UnsafeRolloutPathError(absolute)
    current = codex_home
    for part in absolute.relative_to(codex_home).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise UnsafeRolloutPathError(current)
    return absolute


def initial_cursor(path: Path) -> RolloutCursor:
    """Start at current EOF so history before opt-in is never replayed."""
    with path.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
    return RolloutCursor(
        device=DeviceId(metadata.st_dev),
        file_id=FileId(metadata.st_ino),
        offset=ByteOffset(metadata.st_size),
    )


def read_new_records(path: Path, cursor: RolloutCursor) -> RecordBatch:
    """Decode complete new JSON lines without advancing across partial input."""
    records: list[dict[str, JsonValue]] = []
    next_offset = cursor.offset
    with path.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (
            metadata.st_dev != cursor.device
            or metadata.st_ino != cursor.file_id
            or metadata.st_size < cursor.offset
        ):
            raise RolloutRotatedError(path)
        _ = handle.seek(cursor.offset)
        source_bytes = 0
        while len(records) < _MAX_BATCH_RECORDS:
            line_start = handle.tell()
            line = handle.readline(_MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > _MAX_LINE_BYTES:
                raise RolloutCorruptError(path, "line_too_large")
            if not line.endswith(b"\n"):
                next_offset = ByteOffset(line_start)
                break
            if source_bytes + len(line) > _MAX_BATCH_BYTES:
                next_offset = ByteOffset(line_start)
                break
            records.append(_decode_record(path, line))
            source_bytes += len(line)
            next_offset = ByteOffset(handle.tell())
    return RecordBatch(
        records=tuple(records),
        cursor=RolloutCursor(cursor.device, cursor.file_id, next_offset),
    )


def load_cursor(root: Path, session_id: str) -> RolloutCursor | None:
    """Load a persisted cursor or return None before the first watcher pass."""
    path = cursor_path(root, session_id)
    if not path.is_file():
        return None
    values = load_state(root, path).values
    device = values.get("device")
    file_id = values.get("file_id")
    offset = values.get("offset")
    if (
        type(device) is not int
        or type(file_id) is not int
        or type(offset) is not int
        or device < 0
        or file_id < 0
        or offset < 0
    ):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    return RolloutCursor(DeviceId(device), FileId(file_id), ByteOffset(offset))


def save_cursor(root: Path, session_id: str, cursor: RolloutCursor) -> None:
    """Persist the next unread byte in the session's hashed cursor file."""
    save_state(
        root,
        cursor_path(root, session_id),
        StateDocument(
            values={
                "device": int(cursor.device),
                "file_id": int(cursor.file_id),
                "offset": int(cursor.offset),
            }
        ),
    )


def _decode_record(path: Path, line: bytes) -> dict[str, JsonValue]:
    try:
        source = line.decode("utf-8")
        decoded = _LOAD_JSON(source, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, _NonFiniteNumberError) as error:
        raise RolloutCorruptError(path, "invalid_json") from error
    if type(decoded) is not dict:
        raise RolloutCorruptError(path, "invalid_top_level")
    return decoded


@dataclass(frozen=True, slots=True)
class _NonFiniteNumberError(ValueError):
    token: str


def _reject_constant(token: str) -> Never:
    raise _NonFiniteNumberError(token)
