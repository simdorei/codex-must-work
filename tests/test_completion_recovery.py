from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from scripts.diagnostics import DiagnosticCode, MonitorState
from scripts.hook_event import process_hook
from scripts.state import JsonValue, StateDocument, load_state, runtime_path, save_state
from scripts.state_io import atomic_json_write as actual_atomic_json_write
from scripts.watcher_engine import WatcherEngine
from scripts.watcher_source import load_cursor

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_WALL_TIME = datetime(2026, 7, 17, tzinfo=UTC)


class _RuntimeWriteError(OSError):
    pass


def _child(*, terminal: bool = False) -> dict[str, JsonValue]:
    return {
        "status": "terminal" if terminal else "running",
        "generation": 1,
        "open_tool_count": 0,
        "waiting_for_approval": False,
        "waiting_for_user": False,
    }


def _state(
    tmp_path: Path,
    *,
    children: int = 1,
    parent_complete: bool = False,
    children_terminal: bool = False,
) -> tuple[Path, Path, Path]:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    rollout = codex_home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.touch()
    save_state(
        root,
        root / "config.json",
        StateDocument(
            values={
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": False,
            }
        ),
    )
    runtime = runtime_path(root, "session-secret")
    save_state(
        root,
        runtime,
        StateDocument(
            values={
                "session_id": "session-secret",
                "enabled": True,
                "observe_only": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": False,
                "parent_turn_id": "turn-parent",
                "parent_complete": parent_complete,
                "transcript_path": "sessions/rollout.jsonl",
                "children": {
                    f"child-{index}": _child(terminal=children_terminal)
                    for index in range(1, children + 1)
                },
            }
        ),
    )
    return root, rollout, runtime


def _append_terminal(
    rollout: Path,
    child_id: str | None,
    *,
    event_type: str = "task_complete",
) -> None:
    payload: dict[str, JsonValue] = {
        "type": event_type,
        "turn_id": "turn-parent",
    }
    if child_id is not None:
        payload["agent_id"] = child_id
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "event_msg",
                    "payload": payload,
                }
            )
            + "\n"
        )


def _append_final_answer(rollout: Path) -> None:
    record = {
        "timestamp": "2026-07-17T00:00:00.500Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "turn_id": "turn-parent",
        },
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def _diagnostics(root: Path) -> list[dict[str, JsonValue]]:
    path = root / "logs" / "diagnostic.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _completion_rows(root: Path) -> list[dict[str, JsonValue]]:
    return [
        row
        for row in _diagnostics(root)
        if row.get("code") == DiagnosticCode.WATCHER_COMPLETED.value
    ]


def test_child_rollout_terminal_persists_across_engine_restart(tmp_path: Path) -> None:
    root, rollout, runtime = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    _append_terminal(rollout, "child-1")

    assert engine.tick(1.0, _WALL_TIME) is False
    children = load_state(root, runtime).values["children"]
    assert isinstance(children, dict)
    child = children["child-1"]
    assert isinstance(child, dict)
    assert child["status"] == "terminal"
    fresh = WatcherEngine(root)
    assert fresh.tick(2.0, _WALL_TIME) is False
    assert fresh.tick(92.0, _WALL_TIME) is False
    assert _completion_rows(root) == []


def test_two_children_complete_only_after_parent_terminal(tmp_path: Path) -> None:
    root, rollout, _ = _state(tmp_path, children=2)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    _append_terminal(rollout, "child-1")
    assert engine.tick(1.0, _WALL_TIME) is True
    _append_terminal(rollout, "child-2")
    assert engine.tick(2.0, _WALL_TIME) is False
    assert _completion_rows(root) == []

    stop = json.dumps(
        {
            "session_id": "session-secret",
            "turn_id": "turn-parent",
            "hook_event_name": "Stop",
        }
    )
    with patch("scripts.hook_event._launch_watcher") as launch:
        _ = process_hook(stop, root=root)
    launch.assert_called_once_with()
    assert WatcherEngine(root).tick(3.0, _WALL_TIME) is False
    rows = _completion_rows(root)
    assert len(rows) == 1
    assert rows[0]["child_hash"] is None
    assert rows[0]["state"] == MonitorState.COMPLETED.value
    assert isinstance(rows[0].get("event_id"), str)


def test_final_answer_followed_by_parent_abort_never_records_completion(tmp_path: Path) -> None:
    root, rollout, runtime = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    _append_final_answer(rollout)
    _append_terminal(rollout, None, event_type="turn_aborted")

    assert engine.tick(1.0, _WALL_TIME) is True
    assert load_state(root, runtime).values["parent_complete"] is False
    assert _completion_rows(root) == []


def test_completion_commit_failure_retries_without_duplicate_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, runtime = _state(
        tmp_path,
        parent_complete=True,
        children_terminal=True,
    )
    failed = False

    def fail_runtime_once(
        path: Path,
        *,
        schema_version: int,
        values: Mapping[str, JsonValue],
    ) -> None:
        nonlocal failed
        if path == runtime and not failed:
            failed = True
            raise _RuntimeWriteError
        actual_atomic_json_write(path, schema_version=schema_version, values=values)

    monkeypatch.setattr("scripts.state.atomic_json_write", fail_runtime_once)
    with pytest.raises(_RuntimeWriteError):
        _ = WatcherEngine(root).tick(0.0, _WALL_TIME)
    assert len(_completion_rows(root)) == 1

    monkeypatch.setattr("scripts.state.atomic_json_write", actual_atomic_json_write)
    assert WatcherEngine(root).tick(1.0, _WALL_TIME) is False
    assert len(_completion_rows(root)) == 1


def test_runtime_write_failure_does_not_advance_rollout_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, rollout, runtime = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    cursor_before = load_cursor(root, "session-secret")
    assert cursor_before is not None
    _append_terminal(rollout, "child-1")

    def fail_runtime(
        path: Path,
        *,
        schema_version: int,
        values: Mapping[str, JsonValue],
    ) -> None:
        if path == runtime:
            raise _RuntimeWriteError
        actual_atomic_json_write(path, schema_version=schema_version, values=values)

    monkeypatch.setattr("scripts.state.atomic_json_write", fail_runtime)
    with pytest.raises(_RuntimeWriteError):
        _ = engine.tick(1.0, _WALL_TIME)
    assert load_cursor(root, "session-secret") == cursor_before

    monkeypatch.setattr("scripts.state.atomic_json_write", actual_atomic_json_write)
    assert WatcherEngine(root).tick(2.0, _WALL_TIME) is False
    children = load_state(root, runtime).values["children"]
    assert isinstance(children, dict)
    child = children["child-1"]
    assert isinstance(child, dict)
    assert child["status"] == "terminal"
