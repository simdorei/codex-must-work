"""Scan bounded local rollout history for privacy-safe progress gaps."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Never, Protocol, assert_never, final

from scripts.calibration import CalibrationOutcome, CalibrationUnavailable, recommend_thresholds
from scripts.event_source import EventKind, parse_rollout_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from scripts.event_source import JsonRecord, JsonValue, ObservedEvent

_MIB: Final = 1024 * 1024
_MAX_LINE_BYTES: Final = 8 * _MIB


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
class ScanLimits:
    """Bound file discovery and bytes parsed during one first-thread scan."""

    lookback: timedelta = timedelta(days=30)
    max_files: int = 100
    max_total_bytes: int = 64 * _MIB
    max_file_bytes: int = 8 * _MIB


DEFAULT_SCAN_LIMITS: Final = ScanLimits()


def scan_history(
    codex_home: Path,
    now: datetime,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
) -> CalibrationOutcome:
    """Calculate a recommendation from bounded recent local rollouts."""
    try:
        files = _recent_rollouts(codex_home, now, limits)
        gaps: list[timedelta] = []
        remaining = limits.max_total_bytes
        for path in files:
            byte_limit = min(
                path.stat(follow_symlinks=False).st_size,
                limits.max_file_bytes,
                remaining,
            )
            if byte_limit <= 0:
                break
            gaps.extend(_progress_gaps(path, byte_limit))
            remaining -= byte_limit
        return recommend_thresholds(gaps)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return CalibrationUnavailable(reason_code=type(error).__name__)


def _recent_rollouts(codex_home: Path, now: datetime, limits: ScanLimits) -> list[Path]:
    cutoff = (now - limits.lookback).timestamp()
    candidates: list[tuple[float, Path]] = []
    for directory_name in ("sessions", "archived_sessions"):
        directory = codex_home / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.jsonl"):
            metadata = path.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and metadata.st_mtime >= cutoff
                and not path.is_symlink()
                and not path.is_junction()
            ):
                candidates.append((metadata.st_mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[: limits.max_files]]


@final
class _GapTracker:
    __slots__ = ("active", "gaps", "last_progress", "open_tools")

    def __init__(self) -> None:
        self.gaps: list[timedelta] = []
        self.active = False
        self.open_tools = 0
        self.last_progress: datetime | None = None

    def observe(self, event: ObservedEvent) -> None:
        match event.kind:
            case EventKind.SESSION_METADATA:
                return
            case EventKind.TURN_STARTED:
                self._start_turn(event.occurred_at)
            case EventKind.TURN_COMPLETED | EventKind.TURN_ABORTED:
                self._end_turn()
            case EventKind.TOOL_STARTED:
                self._start_tool(event.occurred_at)
            case EventKind.TOOL_RESULT:
                self._finish_tool(event.occurred_at)
            case EventKind.ITEM | EventKind.DELTA:
                self._record_progress(event.occurred_at)
            case _:
                assert_never(event.kind)

    def _start_turn(self, occurred_at: datetime) -> None:
        self.active = True
        self.open_tools = 0
        self.last_progress = occurred_at

    def _end_turn(self) -> None:
        self.active = False
        self.open_tools = 0
        self.last_progress = None

    def _start_tool(self, occurred_at: datetime) -> None:
        if not self.active:
            return
        if self.open_tools == 0:
            _append_gap(self.gaps, self.last_progress, occurred_at)
        self.open_tools += 1
        self.last_progress = None

    def _finish_tool(self, occurred_at: datetime) -> None:
        if not self.active:
            return
        if self.open_tools > 0:
            self.open_tools -= 1
            if self.open_tools == 0:
                self.last_progress = occurred_at
            return
        self._record_progress(occurred_at)

    def _record_progress(self, occurred_at: datetime) -> None:
        if self.active and self.open_tools == 0:
            _append_gap(self.gaps, self.last_progress, occurred_at)
            self.last_progress = occurred_at


def _progress_gaps(path: Path, byte_limit: int) -> list[timedelta]:
    tracker = _GapTracker()
    for raw in _tail_records(path, byte_limit):
        event = parse_rollout_event(raw)
        if event is not None:
            tracker.observe(event)
    return tracker.gaps


def _append_gap(gaps: list[timedelta], previous: datetime | None, current: datetime) -> None:
    if previous is None or current <= previous:
        return
    gaps.append(current - previous)


def _tail_records(path: Path, byte_limit: int) -> Iterator[JsonRecord]:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - byte_limit)
        _ = handle.seek(start)
        if start:
            _ = handle.readline(_MAX_LINE_BYTES + 1)
        while line := handle.readline(_MAX_LINE_BYTES + 1):
            if len(line) > _MAX_LINE_BYTES:
                message = "rollout line too large"
                raise json.JSONDecodeError(message, "", 0)
            if not line.endswith(b"\n"):
                return
            decoded = _LOAD_JSON(line.decode("utf-8"), parse_constant=_reject_nonfinite)
            if type(decoded) is dict:
                yield decoded


def _reject_nonfinite(token: str) -> Never:
    message = f"non-finite number: {token}"
    raise json.JSONDecodeError(message, token, 0)
