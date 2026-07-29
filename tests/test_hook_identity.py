import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from scripts.hook_event import process_hook
from scripts.state import StateDocument, load_state, save_state
from tests.hook_fixture import enabled_runtime, hook_event


def test_process_hook_when_session_is_opted_out_creates_zero_artifacts() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        raw = hook_event(
            "SubagentStart",
            agent_id="child-1",
            prompt="PRIVATE-PROMPT",
            tool_input="PRIVATE-INPUT",
            tool_output="PRIVATE-OUTPUT",
        )

        with patch("scripts.hook_event._launch_watcher") as launch:
            _ = process_hook(raw, root=root)

        assert not root.exists()
        launch.assert_not_called()


def test_process_hook_when_enabled_verifies_private_root() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        _ = enabled_runtime(root)

        with patch("scripts.hook_event.ensure_private_root") as secure:
            _ = process_hook(hook_event("SubagentStop", agent_id="missing-child"), root=root)

        secure.assert_called_once_with(root)


def test_process_hook_rejects_mismatched_transcript_before_state_mutation() -> None:
    with TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        root = temporary / "codex-must-work"
        path = enabled_runtime(root)
        values = dict(load_state(root, path).values)
        values["transcript_path"] = "sessions/expected.jsonl"
        values["revision"] = 4
        save_state(root, path, StateDocument(values=values))
        unexpected = temporary / "sessions" / "unexpected.jsonl"
        unexpected.parent.mkdir(parents=True)
        unexpected.touch()
        before = load_state(root, path).values

        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(
                hook_event("UserPromptSubmit", transcript_path=str(unexpected)),
                root=root,
            )

        assert result is None
        assert load_state(root, path).values == before
        launch.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths")
def test_managed_stop_accepts_windows_extended_rollout_identity(tmp_path: Path) -> None:
    # Given: a managed runtime stores the rollout relative to CODEX_HOME.
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    path = enabled_runtime(root)
    transcript = codex_home / "sessions" / "rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()
    values = dict(load_state(root, path).values)
    values.update(
        {
            "managed_mode": True,
            "handoff_requested": False,
            "shutdown_requested": False,
            "transcript_path": "sessions/rollout.jsonl",
            "revision": 0,
        }
    )
    save_state(root, path, StateDocument(values=values))

    # When: Codex sends Stop with the equivalent Windows extended path.
    _ = process_hook(
        hook_event("Stop", transcript_path="\\\\?\\" + str(transcript)),
        root=root,
    )

    # Then: the resident manager receives the continuation handoff.
    assert load_state(root, path).values["handoff_requested"] is True
