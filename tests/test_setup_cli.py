import os
from pathlib import Path

import pytest

from scripts.setup import enable_session
from scripts.setup_cli import main
from scripts.state import StateDocument, config_path, load_state, runtime_path, save_state
from tests.rollout_fixture import SESSION_ID, write_session_meta
from tests.test_setup import managed_report, ready_report, request


def test_cli_manual_disable_requests_owned_turn_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _ = enable_session(root, request(root, observe_only=False), managed_report())
    path = runtime_path(root, SESSION_ID)
    document = load_state(root, path)
    values = dict(document.values)
    values["managed_turn_id"] = "turn-owned"
    values["manager_ready"] = True
    save_state(root, path, StateDocument(values=values))

    exit_code = main(["disable", "--session-id", SESSION_ID])

    runtime = load_state(root, path).values
    assert exit_code == 0
    assert runtime["shutdown_requested"] is True
    assert runtime["shutdown_interrupt"] is True
    assert runtime["managed_turn_id"] == "turn-owned"


def test_cli_completed_disable_records_final_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _ = enable_session(root, request(root, observe_only=False), ready_report())

    exit_code = main(["disable", "--session-id", SESSION_ID, "--completed"])

    captured = capsys.readouterr()
    log = (root / "logs" / "diagnostic.jsonl").read_text(encoding="utf-8")
    assert exit_code == 0
    assert "final heartbeat recorded" in captured.out
    assert log.count('"code":"watcher_completed"') == 1
    assert not runtime_path(root, SESSION_ID).exists()


def test_cli_normal_enable_uses_stop_continuation_without_live_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    write_session_meta(transcript)
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
            "continue",
        ]
    )

    assert exit_code == 0
    root = codex_home / "codex-must-work"
    assert runtime_path(root, SESSION_ID).is_file()


def test_cli_auto_restart_launches_managed_owner_only_with_safe_permission_mode(
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
    launched: list[Path] = []
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def launch(_root: Path, runtime_file: Path) -> int:
        launched.append(runtime_file)
        return 123

    monkeypatch.setattr("scripts.setup_cli.launch_manager", launch)

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

    root = codex_home / "codex-must-work"
    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert exit_code == 0
    assert runtime["managed_mode"] is True
    assert launched == [runtime_path(root, SESSION_ID)]
    assert "stop_continuation=False, restart=True" in capsys.readouterr().out


def test_cli_auto_restart_rejects_permission_mode_that_can_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    write_session_meta(transcript)
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
            "continue",
            "--auto-restart",
            "--permission-mode",
            "default",
        ]
    )

    assert exit_code == 2
    assert not (codex_home / "codex-must-work").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows executable lookup semantics")
def test_cli_never_executes_workspace_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    write_session_meta(transcript)
    marker = tmp_path / "executed.txt"
    fake_codex = tmp_path / "codex.bat"
    _ = fake_codex.write_text(
        f'@echo exploited>"{marker}"\n@echo codex-fake\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
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
            "continue",
        ]
    )

    assert exit_code == 0
    assert not marker.exists()


def test_cli_observe_only_creates_runtime_but_never_enables_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    write_session_meta(transcript)
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
            "--observe-only",
        ]
    )

    root = codex_home / "codex-must-work"
    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert exit_code == 0
    assert runtime["observe_only"] is True
    assert load_state(root, config_path(root)).values["message_preset"] == "cleanup"
