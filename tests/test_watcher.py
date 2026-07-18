from __future__ import annotations

from threading import Event, Thread
from typing import TYPE_CHECKING

from scripts.diagnostics import DiagnosticCode
from scripts.setup import disable_session
from scripts.state import (
    JsonValue,
    StateDocument,
    cursor_path,
    load_state,
    runtime_path,
    save_state,
)
from scripts.watcher_engine import WatcherEngine
from scripts.watcher_source import (
    RecordBatch,
    RolloutCursor,
)
from scripts.watcher_source import (
    read_new_records as actual_read_new_records,
)
from tests.watcher_fixture import WALL_TIME as _WALL_TIME
from tests.watcher_fixture import append_progress as _append_progress
from tests.watcher_fixture import child as _child
from tests.watcher_fixture import diagnostic_codes as _codes
from tests.watcher_fixture import state as _state

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_watcher_warns_once_then_records_restart_unavailable(tmp_path: Path) -> None:
    root, rollout, _ = _state(tmp_path)
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    _append_progress(rollout, "child-1", "PRIVATE-BODY")
    assert engine.tick(1.0, _WALL_TIME) is True
    assert engine.tick(91.0, _WALL_TIME) is True
    assert engine.tick(92.0, _WALL_TIME) is True
    assert engine.tick(301.0, _WALL_TIME) is True

    codes = _codes(root)
    assert codes.count(DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value) == 1
    assert codes.count(DiagnosticCode.RESTART_UNAVAILABLE.value) == 1
    persisted = "".join(
        path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()
    )
    assert "PRIVATE-BODY" not in persisted


def test_watcher_tracks_main_turn_progress_without_subagent(tmp_path: Path) -> None:
    root, rollout, _ = _state(tmp_path, children=0, parent=True)
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    _append_progress(rollout, None)
    assert engine.tick(1.0, _WALL_TIME) is True
    assert engine.tick(90.0, _WALL_TIME) is True
    assert DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value not in _codes(root)
    assert engine.tick(91.0, _WALL_TIME) is True
    assert _codes(root).count(DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value) == 1


def test_watcher_records_periodic_healthy_heartbeat(tmp_path: Path) -> None:
    root, rollout, _ = _state(tmp_path)
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    _append_progress(rollout, "child-1")
    assert engine.tick(1.0, _WALL_TIME) is True
    _append_progress(rollout, "child-1")
    assert engine.tick(80.0, _WALL_TIME) is True
    assert engine.tick(90.0, _WALL_TIME) is True
    assert engine.tick(91.0, _WALL_TIME) is True

    assert _codes(root).count(DiagnosticCode.HEARTBEAT_ACTIVE.value) == 1


def test_watcher_excludes_confirmed_tool_wait_from_silence(tmp_path: Path) -> None:
    root, _, path = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    values: dict[str, JsonValue] = {
        "session_id": "session-secret",
        "enabled": True,
        "observe_only": True,
        "warning_after_ms": 90_000,
        "restart_after_ms": 300_000,
        "auto_restart_requested_by_user": True,
        "parent_turn_id": "turn-parent",
        "parent_complete": False,
        "transcript_path": "sessions/rollout.jsonl",
        "children": {"child-1": {**_child(), "open_tool_count": 1}},
    }
    save_state(root, path, StateDocument(values=values))

    assert engine.tick(1_200.0, _WALL_TIME) is True
    assert DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value not in _codes(root)


def test_watcher_marks_completion_once_and_stops(tmp_path: Path) -> None:
    root, _, path = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": "session-secret",
                "enabled": True,
                "observe_only": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
                "parent_turn_id": "turn-parent",
                "parent_complete": True,
                "transcript_path": "sessions/rollout.jsonl",
                "children": {"child-1": {**_child(), "status": "terminal"}},
            }
        ),
    )

    assert engine.tick(10.0, _WALL_TIME) is False
    assert WatcherEngine(root).tick(11.0, _WALL_TIME) is False
    assert _codes(root).count(DiagnosticCode.WATCHER_COMPLETED.value) == 1


def test_disable_completes_while_rollout_read_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, rollout, _ = _state(tmp_path)
    engine = WatcherEngine(root)
    assert engine.tick(0.0, _WALL_TIME) is True
    _append_progress(rollout, "child-1")
    entered = Event()
    release = Event()
    watcher_done = Event()
    disabled = Event()
    results: list[bool] = []

    def blocking_read(path: Path, cursor: RolloutCursor) -> RecordBatch:
        entered.set()
        if not release.wait(1.0):
            raise AssertionError
        return actual_read_new_records(path, cursor)

    def tick() -> None:
        try:
            results.append(engine.tick(1.0, _WALL_TIME))
        finally:
            watcher_done.set()

    def disable() -> None:
        disable_session(root, "session-secret")
        disabled.set()

    monkeypatch.setattr("scripts.watcher_batch.read_new_records", blocking_read)
    watcher_thread = Thread(target=tick)
    disable_thread = Thread(target=disable)
    watcher_thread.start()
    try:
        assert entered.wait(1.0)
        disable_thread.start()
        assert disabled.wait(0.5)
    finally:
        release.set()
        watcher_thread.join(2.0)
        if disable_thread.ident is not None:
            disable_thread.join(2.0)

    assert watcher_done.is_set()
    assert disabled.is_set()
    assert results == [False]
    assert not runtime_path(root, "session-secret").exists()
    assert not cursor_path(root, "session-secret").exists()


def test_systemic_sibling_silence_never_auto_restarts(tmp_path: Path) -> None:
    root, _, _ = _state(tmp_path, children=2)
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    assert engine.tick(90.0, _WALL_TIME) is True
    assert engine.tick(2_380.0, _WALL_TIME) is True

    assert _codes(root).count(DiagnosticCode.RESTART_UNAVAILABLE.value) == 2


def test_one_silent_managed_target_requests_exact_owned_turn_restart(tmp_path: Path) -> None:
    root, _, path = _state(tmp_path, children=0, parent=True)
    document = load_state(root, path)
    values = dict(document.values)
    values.update(
        {
            "observe_only": False,
            "managed_mode": True,
            "manager_ready": True,
            "managed_turn_id": "turn-parent",
            "handoff_requested": False,
            "restart_request": None,
        }
    )
    save_state(root, path, StateDocument(values=values))
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    assert engine.tick(90.0, _WALL_TIME) is True
    assert engine.tick(301.0, _WALL_TIME) is True

    runtime = load_state(root, path).values
    request = runtime["restart_request"]
    assert isinstance(request, dict)
    assert request["turn_id"] == "turn-parent"
    assert request["target_generation"] == 1
    assert _codes(root).count(DiagnosticCode.RESTART_REQUESTED.value) == 1


def test_active_child_suppresses_whole_parent_turn_restart(tmp_path: Path) -> None:
    root, rollout, path = _state(tmp_path, children=1, parent=True)
    document = load_state(root, path)
    values = dict(document.values)
    values.update(
        {
            "observe_only": False,
            "managed_mode": True,
            "manager_ready": True,
            "managed_turn_id": "turn-parent",
            "handoff_requested": False,
            "restart_request": None,
        }
    )
    save_state(root, path, StateDocument(values=values))
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    _append_progress(rollout, "child-1")
    assert engine.tick(290.0, _WALL_TIME) is True
    assert engine.tick(301.0, _WALL_TIME) is True

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert _codes(root).count(DiagnosticCode.RESTART_REQUESTED.value) == 0


def test_managed_auto_restart_times_out_a_tool_that_never_returns(tmp_path: Path) -> None:
    root, _, path = _state(tmp_path, children=0, parent=True)
    document = load_state(root, path)
    values = dict(document.values)
    parent = values["parent"]
    assert isinstance(parent, dict)
    parent["open_tool_count"] = 1
    values.update(
        {
            "observe_only": False,
            "managed_mode": True,
            "manager_ready": True,
            "managed_turn_id": "turn-parent",
            "handoff_requested": False,
            "restart_request": None,
            "parent": parent,
        }
    )
    save_state(root, path, StateDocument(values=values))
    engine = WatcherEngine(root)

    assert engine.tick(0.0, _WALL_TIME) is True
    assert engine.tick(90.0, _WALL_TIME) is True
    assert engine.tick(301.0, _WALL_TIME) is True

    request = load_state(root, path).values["restart_request"]
    assert isinstance(request, dict)
    assert request["turn_id"] == "turn-parent"
