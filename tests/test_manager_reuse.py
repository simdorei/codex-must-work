import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.manager_lease import ManagerLease, acquire_manager_lease, release_manager_lease
from scripts.setup import ActivationError, enable_session
from scripts.setup_cli import main
from scripts.state import StateDocument, config_path, load_state, runtime_path, save_state
from tests.rollout_fixture import SESSION_ID, write_session_meta
from tests.test_setup import managed_report, request


@dataclass(frozen=True, slots=True)
class ReadyManager:
    root: Path
    runtime_file: Path
    arguments: list[str]
    launched: list[Path]
    lease: ManagerLease


@pytest.fixture
def ready_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ReadyManager:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    executable_name = "codex.exe" if os.name == "nt" else "codex"
    executable = codex_home / ".sandbox-bin" / executable_name
    write_session_meta(transcript)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    launched: list[Path] = []

    def launch(_root: Path, runtime_file: Path) -> int:
        launched.append(runtime_file)
        return 123

    monkeypatch.setattr("scripts.setup_cli.launch_manager", launch)
    arguments = [
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
        "--goal-companion",
        "--permission-mode",
        "bypassPermissions",
    ]
    assert main(arguments) == 0
    root = codex_home / "codex-must-work"
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values.update(
        {
            "manager_ready": True,
            "manager_pid": os.getpid(),
            "handoff_requested": True,
            "restart_count": 2,
        }
    )
    save_state(root, path, StateDocument(values=values))
    lease = acquire_manager_lease(root, path.name)
    assert lease is not None
    return ReadyManager(root, path, arguments, launched, lease)


def test_enable_session_refuses_existing_runtime_without_mutation(tmp_path: Path) -> None:
    # Given: an existing managed runtime contains resident-manager progress.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False, goal_companion=True)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values.update(
        {
            "manager_ready": True,
            "manager_pid": 123,
            "handoff_requested": True,
            "restart_count": 2,
        }
    )
    save_state(root, path, StateDocument(values=values))
    config_before = config_path(root).read_bytes()
    runtime_before = path.read_bytes()

    # When: activation is requested again before the existing runtime is disabled.
    with pytest.raises(ActivationError, match="session_already_enabled"):
        _ = enable_session(root, activation, managed_report())

    # Then: neither configuration nor resident progress is overwritten.
    assert config_path(root).read_bytes() == config_before
    assert path.read_bytes() == runtime_before


def test_cli_repeated_auto_restart_reuses_ready_manager(
    ready_manager: ReadyManager,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: the same session has a healthy PID-bound resident manager.
    runtime_before = ready_manager.runtime_file.read_bytes()

    # When: Work-on is invoked again with identical effective configuration.
    try:
        exit_code = main(ready_manager.arguments)
    finally:
        release_manager_lease(ready_manager.lease)

    # Then: the manager is reused without overwriting its progress state.
    assert exit_code == 0
    assert ready_manager.launched == [ready_manager.runtime_file]
    assert ready_manager.runtime_file.read_bytes() == runtime_before
    assert "already enabled" in capsys.readouterr().out


def test_cli_rejects_observe_only_change_while_manager_is_running(
    ready_manager: ReadyManager,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: active mode is already managed by a resident process.
    runtime_before = ready_manager.runtime_file.read_bytes()

    # When: the repeated request changes only diagnostics-only behavior.
    try:
        exit_code = main([*ready_manager.arguments, "--observe-only"])
    finally:
        release_manager_lease(ready_manager.lease)

    # Then: reuse is refused and the active state remains byte-for-byte unchanged.
    assert exit_code == 2
    assert ready_manager.runtime_file.read_bytes() == runtime_before
    assert "managed_reconfiguration_requires_work_off" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shutdown_requested", True),
        ("manager_error", "app_server_disconnected"),
        ("manager_pid", os.getpid() + 1),
    ],
)
def test_cli_does_not_reuse_unhealthy_or_differently_owned_manager(
    ready_manager: ReadyManager,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: bool | int | str,
) -> None:
    # Given: the persisted manager is shutting down, failed, or owned by another PID.
    values = dict(load_state(ready_manager.root, ready_manager.runtime_file).values)
    values[field] = value
    save_state(ready_manager.root, ready_manager.runtime_file, StateDocument(values=values))
    runtime_before = ready_manager.runtime_file.read_bytes()

    # When: the same activation request is repeated.
    try:
        exit_code = main(ready_manager.arguments)
    finally:
        release_manager_lease(ready_manager.lease)

    # Then: activation fails closed without replacing the existing runtime.
    assert exit_code == 2
    assert ready_manager.runtime_file.read_bytes() == runtime_before
    assert "session_already_enabled" in capsys.readouterr().err


def test_cli_rejects_runtime_whose_session_id_does_not_match_filename(
    ready_manager: ReadyManager,
) -> None:
    # Given: persisted state claims a session identity that hashes to another runtime file.
    values = dict(load_state(ready_manager.root, ready_manager.runtime_file).values)
    values["session_id"] = "different-session"
    save_state(
        ready_manager.root,
        ready_manager.runtime_file,
        StateDocument(values=values),
    )
    runtime_before = ready_manager.runtime_file.read_bytes()

    # When/Then: the identity check fails closed without reusing or replacing the runtime.
    try:
        assert main(ready_manager.arguments) == 2
    finally:
        release_manager_lease(ready_manager.lease)
    assert ready_manager.runtime_file.read_bytes() == runtime_before
