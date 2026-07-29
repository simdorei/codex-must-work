from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _command(tmp_path: Path) -> list[str]:
    return [
        sys.executable,
        str(Path("tests/cmw_process_probe.py").resolve()),
        "--daemon-pid",
        "123",
        "--rollout",
        str(tmp_path / "missing.jsonl"),
        "--session-id",
        "session-a",
        "--activation-turn-id",
        "turn-a",
        "--lifecycle",
        "start-stop",
        "--duration-seconds",
        "1",
        "--cycle-count",
        "1",
        "--max-cpu-seconds",
        "0.25",
        "--max-handle-growth",
        "4",
        "--max-thread-growth",
        "0",
        "--max-heartbeat-gap-seconds",
        "15",
        "--max-descendant-starts",
        "0",
        "--max-wmi-operations",
        "0",
        "--require-zero-event-loss",
        "--output",
        str((tmp_path / "evidence.json").resolve()),
    ]


def test_cli_rejects_malformed_contract_before_live_actions(tmp_path: Path) -> None:
    # Given
    command = _command(tmp_path)
    command[command.index("--cycle-count") + 1] = "-1"

    # When
    result = subprocess.run(  # noqa: S603 - exact local interpreter and tracked script.
        command, check=False, capture_output=True, text=True
    )

    # Then
    assert result.returncode == 2
    assert "nonnegative" in result.stderr
    assert not (tmp_path / "evidence.json").exists()


def test_cli_rejects_missing_rollout_without_creating_output(tmp_path: Path) -> None:
    # Given
    command = _command(tmp_path)

    # When
    result = subprocess.run(  # noqa: S603 - exact local interpreter and tracked script.
        command, check=False, capture_output=True, text=True
    )

    # Then
    assert result.returncode == 2
    assert not (tmp_path / "evidence.json").exists()
    assert "control_capability" not in result.stderr


def test_cli_help_is_a_no_side_effect_real_surface() -> None:
    # Given
    command = [sys.executable, str(Path("tests/cmw_process_probe.py").resolve()), "--help"]

    # When
    result = subprocess.run(  # noqa: S603 - exact local interpreter and tracked script.
        command, check=False, capture_output=True, text=True
    )

    # Then
    assert result.returncode == 0
    assert "--daemon-pid" in result.stdout
