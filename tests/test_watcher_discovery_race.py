from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.setup import disable_session
from scripts.state import StateDocument, cursor_path, runtime_path, save_state
from scripts.watcher_engine import WatcherEngine
from scripts.watcher_state import discover_runtime_files as actual_discover_runtime_files

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _save_runtime(root: Path, session_id: str, child_id: str) -> None:
    save_state(
        root,
        runtime_path(root, session_id),
        StateDocument(
            values={
                "session_id": session_id,
                "enabled": True,
                "observe_only": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
                "parent_turn_id": "turn-parent",
                "parent_complete": False,
                "transcript_path": "sessions/rollout.jsonl",
                "children": {
                    child_id: {
                        "status": "running",
                        "generation": 1,
                        "open_tool_count": 0,
                        "waiting_for_approval": False,
                        "waiting_for_user": False,
                    }
                },
            }
        ),
    )


def test_disable_after_discovery_does_not_stop_other_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                "auto_restart_requested_by_user": True,
            }
        ),
    )
    _save_runtime(root, "session-one", "child-one")
    _save_runtime(root, "session-two", "child-two")

    def remove_session_after_listing(state_root: Path) -> tuple[Path, ...]:
        candidates = actual_discover_runtime_files(state_root)
        disable_session(state_root, "session-one")
        return candidates

    monkeypatch.setattr(
        "scripts.watcher_engine.discover_runtime_files",
        remove_session_after_listing,
    )

    assert WatcherEngine(root).tick(0.0, datetime(2026, 7, 17, tzinfo=UTC)) is True
    assert not runtime_path(root, "session-one").exists()
    assert not cursor_path(root, "session-one").exists()
    assert runtime_path(root, "session-two").exists()
    assert cursor_path(root, "session-two").exists()
