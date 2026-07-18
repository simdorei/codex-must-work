from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.hook_event import process_hook
from scripts.hook_payload import StopContinuation
from scripts.state import (
    JsonValue,
    StateDocument,
    load_state,
    save_state,
)
from tests.hook_fixture import enabled_runtime as _enabled_runtime
from tests.hook_fixture import hook_event as _event


def test_process_hook_when_subagent_starts_saves_allowlist_before_launch() -> None:
    # Given: an enabled session and secret fields outside the allowlist.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        raw = _event(
            "SubagentStart",
            agent_id="child-1",
            agent_type="worker",
            permission_mode="default",
            cwd="PRIVATE-CWD",
            model="PRIVATE-MODEL",
            prompt="PRIVATE-PROMPT",
            tool_input="PRIVATE-INPUT",
            tool_output="PRIVATE-OUTPUT",
            body="PRIVATE-BODY",
        )
        observed_status: list[str] = []

        def observe_saved_state() -> None:
            document = load_state(root, path)
            children = document.values["children"]
            assert isinstance(children, dict)
            child = children["child-1"]
            assert isinstance(child, dict)
            status = child["status"]
            assert isinstance(status, str)
            observed_status.append(status)

        # When: the first child is recorded.
        with patch("scripts.hook_event._launch_watcher", side_effect=observe_saved_state):
            _ = process_hook(raw, root=root)

        # Then: persisted state precedes launch and contains no private body.
        serialized = path.read_text(encoding="utf-8")
        assert observed_status == ["running"]
        assert "PRIVATE" not in serialized
        assert "agent_type" not in serialized
        assert "permission_mode" not in serialized
        assert "unknown_future_key" in serialized


def test_process_hook_when_another_child_starts_does_not_launch_duplicate() -> None:
    # Given: an enabled session already has a nonterminal child.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True, approval_wait=True)

        # When: another child starts while the watcher should already exist.
        with patch("scripts.hook_event._launch_watcher") as launch:
            _ = process_hook(_event("SubagentStart", agent_id="child-2"), root=root)

        # Then: state is saved, but no second watcher process is requested.
        children = load_state(root, path).values["children"]
        assert isinstance(children, dict)
        assert "child-2" in children
        launch.assert_not_called()


def test_process_hook_when_user_submits_clears_wait_and_resumes_parent() -> None:
    # Given: an enabled runtime paused for user input after parent completion.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True)

        # When: a new user turn begins.
        with patch("scripts.hook_event._launch_watcher") as launch:
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        # Then: the parent and existing child are marked runnable again.
        values = load_state(root, path).values
        children = values["children"]
        assert isinstance(children, dict)
        child = children["child-1"]
        assert isinstance(child, dict)
        assert values["parent_complete"] is False
        assert values["parent_turn_id"] == "turn-1"
        assert isinstance(values["parent"], dict)
        assert child["waiting_for_user"] is False
        assert child["waiting_for_approval"] is False
        launch.assert_called_once_with()


def test_process_hook_when_main_turn_starts_saves_parent_before_launch() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        observed_parent: list[JsonValue] = []

        def observe_saved_state() -> None:
            observed_parent.append(load_state(root, path).values["parent"])

        with patch("scripts.hook_event._launch_watcher", side_effect=observe_saved_state):
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        assert len(observed_parent) == 1
        assert isinstance(observed_parent[0], dict)
        assert observed_parent[0]["status"] == "running"


def test_process_hook_routes_main_tool_and_permission_events_to_parent() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        with patch("scripts.hook_event._launch_watcher"):
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        _ = process_hook(_event("PreToolUse"), root=root)
        _ = process_hook(_event("PermissionRequest"), root=root)
        values = load_state(root, path).values
        parent = values["parent"]
        assert isinstance(parent, dict)
        assert parent["open_tool_count"] == 1
        assert parent["waiting_for_approval"] is True

        _ = process_hook(_event("PostToolUse"), root=root)
        parent = load_state(root, path).values["parent"]
        assert isinstance(parent, dict)
        assert parent["open_tool_count"] == 0
        assert parent["waiting_for_approval"] is False
        assert isinstance(parent["last_tool_result_at"], str)


def test_process_hook_when_next_main_turn_starts_increments_generation() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)

        with patch("scripts.hook_event._launch_watcher") as launch:
            _ = process_hook(_event("UserPromptSubmit"), root=root)
            _ = process_hook(_event("UserPromptSubmit", turn_id="turn-2"), root=root)

        values = load_state(root, path).values
        parent = values["parent"]
        assert isinstance(parent, dict)
        assert values["parent_turn_id"] == "turn-2"
        assert parent["generation"] == 2
        assert launch.call_count == 2


def test_process_hook_when_child_tool_starts_increments_open_count() -> None:
    # Given: an enabled child with no open tool.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True)

        # When: that child starts a tool.
        _ = process_hook(_event("PreToolUse", agent_id="child-1"), root=root)

        # Then: the child's open-tool count increments.
        values = load_state(root, path).values
        children = values["children"]
        assert isinstance(children, dict)
        child = children["child-1"]
        assert isinstance(child, dict)
        assert child["open_tool_count"] == 1


def test_process_hook_when_child_tool_finishes_floors_count_and_records_time() -> None:
    # Given: an enabled child whose open count is already zero.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True)

        # When: a tool result arrives.
        _ = process_hook(_event("PostToolUse", agent_id="child-1"), root=root)

        # Then: the count stays nonnegative and result time is recorded.
        values = load_state(root, path).values
        children = values["children"]
        assert isinstance(children, dict)
        child = children["child-1"]
        assert isinstance(child, dict)
        assert child["open_tool_count"] == 0
        assert isinstance(child["last_tool_result_at"], str)


def test_process_hook_when_child_requests_permission_marks_approval_wait() -> None:
    # Given: an enabled running child.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True)

        # When: permission is requested by that child.
        _ = process_hook(_event("PermissionRequest", agent_id="child-1"), root=root)

        # Then: only the child's approval wait flag is set.
        values = load_state(root, path).values
        children = values["children"]
        assert isinstance(children, dict)
        child = children["child-1"]
        assert isinstance(child, dict)
        assert child["waiting_for_approval"] is True


def test_process_hook_when_subagent_stops_marks_only_child_terminal() -> None:
    # Given: an enabled running child.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root, child=True)

        # When: the child stop event arrives.
        _ = process_hook(_event("SubagentStop", agent_id="child-1"), root=root)

        # Then: the child becomes terminal without launching a watcher.
        values = load_state(root, path).values
        children = values["children"]
        assert isinstance(children, dict)
        child = children["child-1"]
        assert isinstance(child, dict)
        assert child["status"] == "terminal"


def test_process_hook_when_parent_stops_returns_same_task_continuation() -> None:
    # Given: an enabled active parent runtime.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        path = _enabled_runtime(root)
        with patch("scripts.hook_event._launch_watcher"):
            _ = process_hook(_event("UserPromptSubmit"), root=root)

        # When: the parent tries to stop while monitoring is still enabled.
        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(_event("Stop"), root=root)

        # Then: Codex receives a supported Stop continuation instead of completion.
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
