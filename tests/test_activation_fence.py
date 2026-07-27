from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts.activation_fence import (
    ActivationFenceError,
    ActivationFenceStatus,
    capture_activation_fence,
    recover_activation_fence,
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

if TYPE_CHECKING:
    from pathlib import Path


def test_capture_and_poll_exact_activation_completion(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _write(
        rollout,
        _event("task_started", "old-turn"),
        _event("task_complete", "old-turn"),
        _event("task_started", "activation-turn"),
    )
    fence = capture_activation_fence(rollout)

    # When
    _append(rollout, _event("task_complete", "activation-turn"))
    status, advanced = fence.poll(rollout)

    # Then
    assert fence.turn_id == "activation-turn"
    assert status is ActivationFenceStatus.COMPLETED
    assert advanced.cursor.offset > fence.cursor.offset


def test_poll_rejects_aborted_activation_turn(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _write(rollout, _event("task_started", "activation-turn"))
    fence = capture_activation_fence(rollout)

    # When
    _append(rollout, _event("turn_aborted", "activation-turn"))
    status, _advanced = fence.poll(rollout)

    # Then
    assert status is ActivationFenceStatus.ABORTED


def test_poll_rejects_new_main_turn_before_completion(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _write(rollout, _event("task_started", "activation-turn"))
    fence = capture_activation_fence(rollout)

    # When
    _append(rollout, _event("task_started", "other-turn"))
    status, _advanced = fence.poll(rollout)

    # Then
    assert status is ActivationFenceStatus.SUPERSEDED


def test_capture_rejects_missing_or_ambiguous_open_turn(tmp_path: Path) -> None:
    # Given
    completed = tmp_path / "completed.jsonl"
    ambiguous = tmp_path / "ambiguous.jsonl"
    _write(completed, _event("task_started", "done"), _event("task_complete", "done"))
    _write(ambiguous, _event("task_started", "one"), _event("task_started", "two"))

    # When
    # Then
    with pytest.raises(ActivationFenceError, match="activation_turn_not_observable"):
        _ = capture_activation_fence(completed)
    with pytest.raises(ActivationFenceError, match="activation_turn_ambiguous"):
        _ = capture_activation_fence(ambiguous)


def test_recovery_classifies_persisted_terminal_turn(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _write(
        rollout,
        _event("task_started", "activation-turn"),
        _event("task_complete", "activation-turn"),
    )

    # When
    status, fence = recover_activation_fence(rollout, "activation-turn")

    # Then
    assert status is ActivationFenceStatus.COMPLETED
    assert fence.turn_id == "activation-turn"


def test_capture_keeps_partial_line_for_next_poll(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _write(rollout, _event("task_started", "activation-turn"))
    completion = json.dumps(_event("task_complete", "activation-turn"), separators=(",", ":"))
    with rollout.open("a", encoding="utf-8", newline="") as handle:
        _ = handle.write(completion[:-1])
    fence = capture_activation_fence(rollout)

    # When
    with rollout.open("a", encoding="utf-8", newline="") as handle:
        _ = handle.write(completion[-1:] + "\n")
    status, _advanced = fence.poll(rollout)

    # Then
    assert status is ActivationFenceStatus.COMPLETED


def _event(kind: str, turn_id: str) -> JsonObject:
    return {
        "timestamp": "2026-07-22T00:00:00.000Z",
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": turn_id},
    }


def _write(path: Path, *records: JsonObject) -> None:
    _ = path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
        newline="",
    )


def _append(path: Path, record: JsonObject) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        _ = handle.write(json.dumps(record, separators=(",", ":")) + "\n")
