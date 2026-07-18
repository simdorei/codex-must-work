"""Bounded privacy-safe diagnostics for the local watcher."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Never, override

from scripts.state_io import ExclusiveWriteLock

MAX_LOG_BYTES: Final = 1_048_576
_BACKUP_COUNT: Final = 2
_DIRECTORY_MODE: Final = stat.S_IRWXU
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_SHA256_HEX_LENGTH: Final = 64


@unique
class DiagnosticCode(StrEnum):
    """Fixed codes that cannot carry source text or raw errors."""

    WATCHER_STARTED = "watcher_started"
    HEARTBEAT_ACTIVE = "heartbeat_active"
    WATCHER_COMPLETED = "watcher_completed"
    OBSERVABLE_PROGRESS_SILENCE = "observable_progress_silence"
    RESTART_REQUESTED = "restart_requested"
    RESTART_PERFORMED = "restart_performed"
    RESTART_UNAVAILABLE = "restart_unavailable"
    MANAGER_FAILED = "manager_failed"
    ROLLOUT_CORRUPT = "rollout_corrupt"
    ROLLOUT_ROTATED = "rollout_rotated"
    STATE_UNAVAILABLE = "state_unavailable"


@unique
class MonitorState(StrEnum):
    """Sanitized watcher states used by operators."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED_CLOSED = "failed_closed"


@dataclass(frozen=True, slots=True)
class InvalidDiagnosticError(ValueError):
    """Reject a diagnostic that could violate the fixed safe schema."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return f"invalid diagnostic: {self.reason_code}"


@dataclass(frozen=True, slots=True)
class UnsafeDiagnosticPathError(OSError):
    """Reject redirected diagnostic directories."""

    path: Path

    @override
    def __str__(self) -> str:
        return f"unsafe diagnostic path: {self.path}"


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """The complete allowlist of values diagnostics may persist."""

    occurred_at: datetime
    code: DiagnosticCode
    state: MonitorState
    session_hash: str
    child_hash: str | None = None
    elapsed_ms: int | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        """Reject unsafe values before they can reach disk."""
        if self.occurred_at.utcoffset() is None:
            _invalid("timestamp_timezone_missing")
        if not _is_sha256(self.session_hash):
            _invalid("session_hash_invalid")
        if self.child_hash is not None and not _is_sha256(self.child_hash):
            _invalid("child_hash_invalid")
        if self.elapsed_ms is not None and self.elapsed_ms < 0:
            _invalid("elapsed_ms_invalid")
        if self.event_id is not None and not _is_sha256(self.event_id):
            _invalid("event_id_invalid")


def append_diagnostic(
    root: Path,
    event: DiagnosticEvent,
    max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """Append one bounded JSON line, retaining at most two backups."""
    if max_bytes < 1:
        _invalid("max_bytes_invalid")
    path = _diagnostic_path(root)
    encoded = _encode(event)
    if len(encoded) > max_bytes:
        _invalid("record_exceeds_limit")
    with ExclusiveWriteLock(path):
        _ensure_direct_file(path)
        if event.event_id is not None and _contains_event_id(path, event.event_id):
            return
        existing = _complete_bytes(path, max_bytes)
        if len(existing) + len(encoded) > max_bytes:
            _rotate(path)
            existing = b""
        _atomic_log_write(path, existing + encoded)


def _diagnostic_path(root: Path) -> Path:
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100
    logs = absolute_root / "logs"
    path = logs / "diagnostic.jsonl"
    for candidate in (absolute_root, logs):
        if candidate.is_symlink() or candidate.is_junction():
            raise UnsafeDiagnosticPathError(candidate)
    logs.mkdir(parents=True, exist_ok=True)
    absolute_root.chmod(_DIRECTORY_MODE)
    logs.chmod(_DIRECTORY_MODE)
    _ensure_direct_file(path)
    return path


def _encode(event: DiagnosticEvent) -> bytes:
    timestamp = event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return (
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": timestamp,
                "code": event.code.value,
                "state": event.state.value,
                "session_hash": event.session_hash,
                "child_hash": event.child_hash,
                "elapsed_ms": event.elapsed_ms,
                "event_id": event.event_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _rotate(path: Path) -> None:
    for candidate in _log_paths(path):
        _ensure_direct_file(candidate)
    oldest = path.with_name(f"{path.name}.{_BACKUP_COUNT}")
    oldest.unlink(missing_ok=True)
    for index in range(_BACKUP_COUNT - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.is_file():
            _ = source.replace(path.with_name(f"{path.name}.{index + 1}"))
    _ = path.replace(path.with_name(f"{path.name}.1"))


def _log_paths(path: Path) -> tuple[Path, ...]:
    return (path, *(path.with_name(f"{path.name}.{index}") for index in range(1, 3)))


def _ensure_direct_file(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_junction():
        raise UnsafeDiagnosticPathError(path)
    metadata = path.lstat()
    if (
        not path.is_file()
        or metadata.st_nlink != 1
        or (os.name == "nt" and metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    ):
        raise UnsafeDiagnosticPathError(path)


def _complete_bytes(path: Path, max_bytes: int) -> bytes:
    if not path.exists():
        return b""
    content = path.read_bytes()
    if len(content) > max_bytes:
        _invalid("log_size_invalid")
    if content.endswith(b"\n"):
        return content
    last_line = content.rfind(b"\n")
    return b"" if last_line < 0 else content[: last_line + 1]


def _contains_event_id(path: Path, event_id: str) -> bool:
    needle = f'"event_id":"{event_id}"'.encode("ascii")
    return any(
        needle in _complete_bytes(candidate, MAX_LOG_BYTES) for candidate in _log_paths(path)
    )


def _atomic_log_write(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            _ = handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(_FILE_MODE)
        _ = temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _invalid(reason_code: str) -> Never:
    raise InvalidDiagnosticError(reason_code)
