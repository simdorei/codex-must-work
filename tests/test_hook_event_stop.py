from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.hook_event import process_hook
from scripts.hook_payload import StopContinuation
from scripts.state import StateDocument, load_state, save_state
from tests.hook_fixture import enabled_runtime as _enabled_runtime
from tests.hook_fixture import hook_event as _event


def test_process_hook_when_parent_stops_returns_same_task_continuation() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        with patch("scripts.hook_event._launch_watcher"):
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(_event("Stop"), root=root)

        assert isinstance(result, StopContinuation)
        assert "$work-off" in result.reason
        assert load_state(root, path).values["parent_complete"] is False
        launch.assert_not_called()


def test_process_hook_when_observe_only_parent_stops_does_not_continue() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        document = load_state(root, path)
        values = dict(document.values)
        values["observe_only"] = True
        save_state(root, path, StateDocument(values=values))

        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(_event("Stop"), root=root)

        assert result is None
        assert load_state(root, path).values["parent_complete"] is True
        launch.assert_called_once_with()


def test_process_hook_when_managed_parent_stops_requests_owner_handoff() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        document = load_state(root, path)
        values = dict(document.values)
        values.update(
            {
                "managed_mode": True,
                "handoff_requested": False,
                "managed_turn_id": None,
                "restart_request": None,
            }
        )
        save_state(root, path, StateDocument(values=values))
        with patch("scripts.hook_event._launch_watcher"):
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(_event("Stop"), root=root)

        runtime = load_state(root, path).values
        parent = runtime["parent"]
        assert result is None
        assert runtime["handoff_requested"] is True
        assert runtime["parent_complete"] is False
        assert isinstance(parent, dict)
        assert parent["status"] == "terminal"
        launch.assert_not_called()
