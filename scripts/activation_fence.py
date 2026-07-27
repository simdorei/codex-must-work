"""Fence daemon handoff on one exact activation turn in a rollout."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final, Never, Protocol, override

from scripts.event_source import EventKind, parse_rollout_event
from scripts.watcher_source import (
    ByteOffset,
    DeviceId,
    FileId,
    RolloutCursor,
    read_new_records,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import BinaryIO

    from scripts.event_source import JsonRecord, JsonValue

_MAX_SCAN_BYTES: Final = 16 * 1_048_576
_MAX_LINE_BYTES: Final = 8 * 1_048_576
_TURN_MISSING: Final = "activation_turn_not_observable"
_TURN_AMBIGUOUS: Final = "activation_turn_ambiguous"
_ROLLOUT_INVALID: Final = "activation_rollout_invalid"


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


@unique
class ActivationFenceStatus(StrEnum):
    """One fail-closed observation about the activation turn."""

    PENDING = "pending"
    COMPLETED = "completed"
    ABORTED = "aborted"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ActivationFenceError(ValueError):
    """Report that one exact activation turn could not be identified."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


@dataclass(frozen=True, slots=True)
class ActivationFence:
    """Bind an exact open turn to the next unread rollout byte."""

    turn_id: str
    cursor: RolloutCursor

    def poll(self, rollout: Path) -> tuple[ActivationFenceStatus, ActivationFence]:
        """Read only appended records and classify this exact turn."""
        batch = read_new_records(rollout, self.cursor)
        status = ActivationFenceStatus.PENDING
        for record in batch.records:
            event = parse_rollout_event(record)
            if event is None or event.child_id is not None:
                continue
            if event.kind is EventKind.TURN_STARTED and event.turn_id != self.turn_id:
                status = ActivationFenceStatus.SUPERSEDED
                break
            if event.turn_id != self.turn_id:
                continue
            if event.kind is EventKind.TURN_COMPLETED:
                status = ActivationFenceStatus.COMPLETED
                break
            if event.kind is EventKind.TURN_ABORTED:
                status = ActivationFenceStatus.ABORTED
                break
        return status, ActivationFence(self.turn_id, batch.cursor)


def capture_activation_fence(rollout: Path) -> ActivationFence:
    """Capture the sole open main turn from a bounded rollout tail."""
    cursor, records = _snapshot_tail(rollout)
    open_turns: dict[str, None] = {}
    for record in records:
        event = parse_rollout_event(record)
        if event is None or event.child_id is not None or event.turn_id is None:
            continue
        if event.kind is EventKind.TURN_STARTED:
            open_turns[event.turn_id] = None
        elif event.kind in {EventKind.TURN_COMPLETED, EventKind.TURN_ABORTED}:
            _ = open_turns.pop(event.turn_id, None)
    if not open_turns:
        raise ActivationFenceError(_TURN_MISSING)
    if len(open_turns) != 1:
        raise ActivationFenceError(_TURN_AMBIGUOUS)
    return ActivationFence(next(iter(open_turns)), cursor)


def recover_activation_fence(
    rollout: Path,
    turn_id: str,
) -> tuple[ActivationFenceStatus, ActivationFence]:
    """Recover one persisted activation turn without adopting another turn."""
    cursor, records = _snapshot_tail(rollout)
    status = ActivationFenceStatus.PENDING
    seen = False
    for record in records:
        event = parse_rollout_event(record)
        if event is None or event.child_id is not None:
            continue
        if event.kind is EventKind.TURN_STARTED:
            if event.turn_id == turn_id:
                seen = True
                status = ActivationFenceStatus.PENDING
            elif seen and status is ActivationFenceStatus.PENDING:
                status = ActivationFenceStatus.SUPERSEDED
        elif event.turn_id == turn_id and event.kind is EventKind.TURN_COMPLETED:
            status = ActivationFenceStatus.COMPLETED
        elif event.turn_id == turn_id and event.kind is EventKind.TURN_ABORTED:
            status = ActivationFenceStatus.ABORTED
    if not seen:
        raise ActivationFenceError(_TURN_MISSING)
    return status, ActivationFence(turn_id, cursor)


def _snapshot_tail(rollout: Path) -> tuple[RolloutCursor, tuple[JsonRecord, ...]]:
    records: list[JsonRecord] = []
    with rollout.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
        snapshot_end = metadata.st_size
        start = max(0, snapshot_end - _MAX_SCAN_BYTES)
        _ = handle.seek(start)
        if start:
            _ = handle.readline(_MAX_LINE_BYTES + 1)
        complete_end = handle.tell()
        for line, line_end in _complete_lines(handle, snapshot_end):
            records.append(_decode_record(line))
            complete_end = line_end
    cursor = RolloutCursor(
        DeviceId(metadata.st_dev),
        FileId(metadata.st_ino),
        ByteOffset(complete_end),
    )
    return cursor, tuple(records)


def _complete_lines(handle: BinaryIO, snapshot_end: int) -> Iterator[tuple[bytes, int]]:
    while handle.tell() < snapshot_end:
        remaining = snapshot_end - handle.tell()
        line = handle.readline(min(_MAX_LINE_BYTES + 1, remaining))
        if not line or len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n"):
            return
        yield line, handle.tell()


def _decode_record(line: bytes) -> JsonRecord:
    try:
        decoded = _LOAD_JSON(line.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, _NonFiniteNumberError) as error:
        raise ActivationFenceError(_ROLLOUT_INVALID) from error
    if type(decoded) is not dict:
        raise ActivationFenceError(_ROLLOUT_INVALID)
    return decoded


@dataclass(frozen=True, slots=True)
class _NonFiniteNumberError(ValueError):
    token: str


def _reject_constant(token: str) -> Never:
    raise _NonFiniteNumberError(token)
