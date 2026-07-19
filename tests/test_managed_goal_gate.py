from __future__ import annotations

import os
from typing import TYPE_CHECKING

from scripts.setup_cli import main
from scripts.state import runtime_path
from tests.rollout_fixture import SESSION_ID, write_session_meta

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_cli_rejects_managed_restart_without_native_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    executable = codex_home / ".sandbox-bin" / ("codex.exe" if os.name == "nt" else "codex")
    write_session_meta(transcript)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

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

    assert exit_code == 2
    assert "managed_restart_requires_native_goal" in capsys.readouterr().err
    root = codex_home / "codex-must-work"
    assert not runtime_path(root, SESSION_ID).exists()
