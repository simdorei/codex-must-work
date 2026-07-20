from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.setup import complete_session, disable_session, enable_session
from scripts.setup_cli import main
from scripts.state import StateDocument, load_state, runtime_path, save_state
from tests.rollout_fixture import SESSION_ID, write_session_meta
from tests.test_setup import managed_report, request

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_managed_restart_without_goal_companion_is_enabled(tmp_path: Path) -> None:
    # Given: automatic restart is available for a task without a native Goal.
    root = tmp_path / "state"

    # When: the user explicitly enables managed restart without a Goal companion.
    result = enable_session(
        root,
        request(root, observe_only=False),
        managed_report(),
    )

    # Then: managed restart is active and the runtime remains Goal-less.
    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert result.effective_auto_restart is True
    assert runtime["managed_mode"] is True
    assert runtime["goal_companion"] is False


def test_managed_completion_waits_for_owned_turn_before_runtime_removal(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _ = enable_session(
        root,
        request(root, observe_only=False, goal_companion=True),
        managed_report(),
    )
    path = runtime_path(root, SESSION_ID)
    document = load_state(root, path)
    values = dict(document.values)
    values["managed_turn_id"] = "turn-owned"
    values["manager_ready"] = True
    save_state(root, path, StateDocument(values=values))

    complete_session(root, SESSION_ID, datetime(2026, 7, 18, tzinfo=UTC))

    runtime = load_state(root, path).values
    assert runtime["shutdown_requested"] is True
    assert runtime["shutdown_interrupt"] is False
    assert runtime["managed_turn_id"] == "turn-owned"


def test_managed_disable_removes_runtime_when_manager_never_became_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _ = enable_session(
        root,
        request(root, observe_only=False, goal_companion=True),
        managed_report(),
    )
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values["managed_turn_id"] = "turn-unowned"
    values["manager_ready"] = False
    save_state(root, path, StateDocument(values=values))

    disable_session(root, SESSION_ID)

    assert not path.exists()


def test_cli_enables_managed_restart_without_native_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a valid current thread and approval-free managed controls.
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    executable = codex_home / ".sandbox-bin" / ("codex.exe" if os.name == "nt" else "codex")
    write_session_meta(transcript)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def launch(_root: Path, _runtime_file: Path) -> int:
        return 123

    monkeypatch.setattr("scripts.setup_cli.launch_manager", launch)

    # When: automatic restart is enabled without `--goal-companion`.
    exit_code = main(
        [
            "enable",
            "--session-id",
            SESSION_ID,
            "--transcript-path",
            str(transcript),
            "--warning",
            "90s",
            "--restart",
            "5m",
            "--message-preset",
            "cleanup",
            "--auto-restart",
            "--permission-mode",
            "bypassPermissions",
        ]
    )

    # Then: the CLI activates a Goal-less resident manager.
    assert exit_code == 0
    assert capsys.readouterr().err == ""
    root = codex_home / "codex-must-work"
    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert runtime["managed_mode"] is True
    assert runtime["goal_companion"] is False
