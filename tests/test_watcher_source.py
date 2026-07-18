from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import pytest

from scripts.state import cursor_path
from scripts.watcher_source import (
    RolloutCorruptError,
    RolloutRotatedError,
    UnsafeRolloutPathError,
    initial_cursor,
    load_cursor,
    read_new_records,
    resolve_rollout_path,
    save_cursor,
)

if TYPE_CHECKING:
    from typing import BinaryIO, TextIO


def _record() -> bytes:
    return (
        json.dumps(
            {
                "timestamp": "2026-07-17T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            }
        )
        + "\n"
    ).encode()


def test_read_new_records_when_line_is_appended_reads_only_new_bytes() -> None:
    # Given: a rollout whose existing history is intentionally skipped.
    with TemporaryDirectory() as temporary_directory:
        rollout = Path(temporary_directory) / "rollout.jsonl"
        _ = rollout.write_bytes(_record())
        cursor = initial_cursor(rollout)

        # When: one new complete record is appended.
        with rollout.open("ab") as handle:
            _ = handle.write(_record())
        batch = read_new_records(rollout, cursor)

        # Then: only that new record is returned and the byte cursor advances.
        assert len(batch.records) == 1
        assert batch.cursor.offset == rollout.stat().st_size


def test_read_new_records_when_line_is_partial_keeps_cursor_before_line() -> None:
    # Given: a cursor at EOF and a newly appended partial JSON line.
    with TemporaryDirectory() as temporary_directory:
        rollout = Path(temporary_directory) / "rollout.jsonl"
        _ = rollout.write_bytes(b"")
        cursor = initial_cursor(rollout)
        _ = rollout.write_bytes(_record()[:-1])

        # When: the incremental reader reaches the incomplete line.
        batch = read_new_records(rollout, cursor)

        # Then: no record is exposed and the cursor remains unchanged.
        assert batch.records == ()
        assert batch.cursor == cursor


def test_read_new_records_when_file_identity_changes_fails_closed() -> None:
    # Given: a persisted cursor for a rollout that is replaced in place.
    with TemporaryDirectory() as temporary_directory:
        rollout = Path(temporary_directory) / "rollout.jsonl"
        _ = rollout.write_bytes(_record())
        cursor = initial_cursor(rollout)
        rollout.unlink()
        _ = rollout.write_bytes(_record())

        # When/Then: rotation is surfaced instead of silently resetting.
        with pytest.raises(RolloutRotatedError):
            _ = read_new_records(rollout, cursor)


def test_read_new_records_checks_identity_of_opened_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    rollout.touch()
    _ = replacement.write_bytes(_record())
    cursor = initial_cursor(rollout)
    actual_open = Path.open
    swapped = False

    def swap_before_open(
        path: Path,
        mode: str = "r",
    ) -> TextIO | BinaryIO:
        nonlocal swapped
        if path == rollout and mode == "rb" and not swapped:
            swapped = True
            rollout.unlink()
            _ = replacement.replace(rollout)
        return cast("TextIO | BinaryIO", actual_open(path, mode))

    monkeypatch.setattr(Path, "open", swap_before_open)

    with pytest.raises(RolloutRotatedError):
        _ = read_new_records(rollout, cursor)


def test_read_new_records_caps_total_source_bytes_per_batch(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch()
    cursor = initial_cursor(rollout)
    large_record = (
        json.dumps(
            {
                "timestamp": "2026-07-17T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "padding": "x" * 1_048_576},
            }
        ).encode()
        + b"\n"
    )
    with rollout.open("ab") as handle:
        for _ in range(12):
            _ = handle.write(large_record)

    batch = read_new_records(rollout, cursor)

    assert batch.cursor.offset <= 8 * 1_048_576
    assert batch.cursor.offset < rollout.stat().st_size


def test_read_new_records_when_json_is_corrupt_fails_closed() -> None:
    # Given: a new complete but malformed JSON line.
    with TemporaryDirectory() as temporary_directory:
        rollout = Path(temporary_directory) / "rollout.jsonl"
        _ = rollout.write_bytes(b"")
        cursor = initial_cursor(rollout)
        _ = rollout.write_bytes(b"{not-json}\n")

        # When/Then: corruption is explicit and no cursor is returned.
        with pytest.raises(RolloutCorruptError):
            _ = read_new_records(rollout, cursor)


def test_cursor_round_trip_uses_hashed_session_filename() -> None:
    # Given: a real rollout cursor and an isolated state root.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        rollout = Path(temporary_directory) / "rollout.jsonl"
        _ = rollout.write_bytes(_record())
        cursor = initial_cursor(rollout)

        # When: the cursor is persisted and loaded.
        save_cursor(root, "private-session-id", cursor)

        # Then: its raw ID is absent from the filename and values round-trip.
        path = cursor_path(root, "private-session-id")
        assert "private-session-id" not in path.name
        assert load_cursor(root, "private-session-id") == cursor


def test_resolve_rollout_path_when_relative_path_escapes_fails() -> None:
    # Given: an isolated CODEX_HOME and a relative traversal.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-home" / "codex-must-work"

        # When/Then: the watcher refuses to leave CODEX_HOME.
        with pytest.raises(UnsafeRolloutPathError):
            _ = resolve_rollout_path(root, "../outside.jsonl")
