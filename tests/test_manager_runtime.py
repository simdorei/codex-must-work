from pathlib import Path

from scripts.manager_runtime import (
    load_manager_runtime,
    mark_manager_ready,
    record_restart_performed,
    record_turn_finished,
    record_turn_started,
)
from scripts.state import StateDocument, load_state, runtime_path, save_state
from scripts.watcher_source import initial_cursor, save_cursor
from tests.rollout_fixture import write_session_meta


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "codex-must-work"
    path = runtime_path(root, "thread-1")
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    write_session_meta(rollout, "thread-1")
    save_cursor(root, "thread-1", initial_cursor(rollout))
    save_state(
        root,
        root / "config.json",
        StateDocument(values={"message_preset": "cleanup"}),
    )
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": "thread-1",
                "enabled": True,
                "observe_only": False,
                "managed_mode": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
                "message_preset": "cleanup",
                "executable_sha256": "digest",
                "transcript_path": "sessions/rollout.jsonl",
                "parent_turn_id": None,
                "parent_complete": False,
                "parent": None,
                "children": {},
                "manager_ready": False,
                "manager_pid": None,
                "manager_error": None,
                "handoff_requested": True,
                "managed_turn_id": None,
                "restart_request": None,
                "restart_claimed": False,
                "restart_count": 0,
                "shutdown_requested": False,
                "shutdown_interrupt": False,
                "revision": 0,
            }
        ),
    )
    return root, path


def test_manager_runtime_records_owned_turn_lifecycle(tmp_path: Path) -> None:
    root, path = _runtime(tmp_path)

    mark_manager_ready(root, path, pid=123)
    record_turn_started(root, path, "turn-1")
    active = load_manager_runtime(root, path.name)

    assert active is not None
    assert active.view.managed_turn_id == "turn-1"
    assert active.view.handoff_requested is False
    record_turn_finished(root, path, "turn-1")
    finished = load_manager_runtime(root, path.name)
    assert finished is not None
    assert finished.view.managed_turn_id is None
    assert finished.view.handoff_requested is True


def test_restart_completion_clears_only_exact_request_and_requeues_handoff(
    tmp_path: Path,
) -> None:
    root, path = _runtime(tmp_path)
    record_turn_started(root, path, "turn-1")
    document = load_state(root, path)
    values = dict(document.values)
    values["restart_request"] = {
        "request_id": "request-1",
        "turn_id": "turn-1",
        "target_id": None,
        "target_generation": 2,
        "progress_epoch": 0,
    }
    values["restart_claimed"] = True
    save_state(root, path, StateDocument(values=values))

    record_restart_performed(root, path, "turn-1")

    runtime = load_state(root, path).values
    assert runtime["restart_request"] is None
    assert runtime["managed_turn_id"] is None
    assert runtime["handoff_requested"] is True
    assert runtime["restart_count"] == 1
