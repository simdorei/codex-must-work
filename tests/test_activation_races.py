import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.manager_lease import acquire_manager_lease, release_manager_lease
from scripts.manager_reuse import reuse_ready_manager
from scripts.manager_runtime import ManagerRuntime, load_manager_runtime
from scripts.private_root import PrivateRootError, PrivateRootReason
from scripts.setup import ActivationError, enable_session
from scripts.setup_cli import main
from scripts.state import (
    StateDocument,
    UnsafeStatePathError,
    config_path,
    load_state,
    runtime_path,
    save_state,
)
from scripts.state_io import (
    ExclusiveWriteLock,
    StateError,
    prepare_parent_directories,
)
from tests.rollout_fixture import SESSION_ID, write_session_meta
from tests.test_setup import managed_report, request


def test_reuse_rechecks_lifecycle_from_locked_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a previously healthy manager begins shutdown after the initial probe.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values.update({"manager_ready": True, "manager_pid": os.getpid()})
    save_state(root, path, StateDocument(values=values))
    lease = acquire_manager_lease(root, path.name)
    assert lease is not None
    stale_manager = load_manager_runtime(root, path.name)
    assert stale_manager is not None
    values["shutdown_requested"] = True
    save_state(root, path, StateDocument(values=values))

    def return_stale_manager(_root: Path, _path: Path) -> ManagerRuntime:
        return stale_manager

    monkeypatch.setattr("scripts.manager_reuse.active_manager", return_stale_manager)

    # When/Then: the locked current snapshot prevents reuse of the shutting-down manager.
    try:
        assert reuse_ready_manager(root, path, activation) is False
    finally:
        release_manager_lease(lease)


def test_enable_refuses_runtime_creation_while_old_manager_lease_is_held(
    tmp_path: Path,
) -> None:
    # Given: shutdown removed the runtime before the old resident manager released its lease.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    lease = acquire_manager_lease(root, path.name)
    assert lease is not None
    path.unlink()
    config_before = config_path(root).read_bytes()

    # When/Then: a new activation waits for a later retry instead of replacing old state.
    try:
        with pytest.raises(ActivationError, match="session_manager_still_running"):
            _ = enable_session(root, activation, managed_report())
    finally:
        release_manager_lease(lease)
    assert config_path(root).read_bytes() == config_before
    assert not path.exists()


def test_cli_rolls_back_runtime_when_launch_raises_state_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: activation succeeds but the manager readiness path reports unsafe state.
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    executable_name = "codex.exe" if os.name == "nt" else "codex"
    executable = codex_home / ".sandbox-bin" / executable_name
    write_session_meta(transcript)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    failure = StateError()

    def fail_launch(_root: Path, _runtime_file: Path) -> int:
        raise failure

    monkeypatch.setattr("scripts.setup_cli.launch_manager", fail_launch)
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
        "--permission-mode",
        "bypassPermissions",
    ]

    # When: launch fails after runtime creation.
    exit_code = main(arguments)

    # Then: the failed activation leaves no enabled runtime behind.
    root = codex_home / "codex-must-work"
    assert exit_code == 2
    assert not runtime_path(root, SESSION_ID).exists()


def test_reuse_rejects_state_root_without_private_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: plausible runtime and lease files remain under an untrusted state root.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values.update({"manager_ready": True, "manager_pid": os.getpid()})
    save_state(root, path, StateDocument(values=values))
    lease = acquire_manager_lease(root, path.name)
    assert lease is not None
    rejection = PrivateRootError(root, PrivateRootReason.MIGRATION_REQUIRED)

    def reject_private_root(_root: Path) -> None:
        raise rejection

    monkeypatch.setattr(
        "scripts.manager_reuse.ensure_private_root",
        reject_private_root,
        raising=False,
    )

    # When/Then: reuse validates the private-root trust boundary before accepting state.
    try:
        with pytest.raises(PrivateRootError, match="migration_required"):
            _ = reuse_ready_manager(root, path, activation)
    finally:
        release_manager_lease(lease)


def test_enable_rejects_dangling_runtime_link_before_config_write(tmp_path: Path) -> None:
    # Given: the runtime path is a dangling redirect and configuration already exists.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    path.unlink()
    try:
        path.symlink_to(tmp_path / "missing-runtime.json")
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")
    config_before = config_path(root).read_bytes()
    changed = replace(activation, now=activation.now + timedelta(seconds=1))

    # When/Then: activation rejects the redirect without partially updating config.
    with pytest.raises(UnsafeStatePathError):
        _ = enable_session(root, changed, managed_report())
    assert config_path(root).read_bytes() == config_before


def test_runtime_write_failure_does_not_change_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an existing configuration and no active runtime are ready for activation.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    path.unlink()
    config_before = config_path(root).read_bytes()
    changed = replace(activation, now=activation.now + timedelta(seconds=1))

    def fail_runtime_write(
        state_root: Path,
        state_path: Path,
        document: StateDocument,
    ) -> None:
        if state_path == path:
            raise StateError
        save_state(state_root, state_path, document)

    monkeypatch.setattr("scripts.setup.save_state", fail_runtime_write)

    # When/Then: a runtime write failure leaves the prior configuration untouched.
    with pytest.raises(StateError):
        _ = enable_session(root, changed, managed_report())
    assert config_path(root).read_bytes() == config_before
    assert not path.exists()


def test_config_write_failure_removes_new_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: configuration persistence will fail after activation state is prepared.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    path = runtime_path(root, SESSION_ID)
    configuration = config_path(root)

    def fail_config_write(
        state_root: Path,
        state_path: Path,
        document: StateDocument,
    ) -> None:
        if state_path == configuration:
            raise StateError
        save_state(state_root, state_path, document)

    monkeypatch.setattr("scripts.setup.save_state", fail_config_write)

    # When/Then: the failed transaction leaves neither partial runtime nor configuration.
    with pytest.raises(StateError):
        _ = enable_session(root, activation, managed_report())
    assert not path.exists()
    assert not configuration.exists()


def test_enable_during_lease_initialization_fails_without_state_write(tmp_path: Path) -> None:
    # Given: a manager owns its lock but has not yet published the PID marker.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=False)
    _ = enable_session(root, activation, managed_report())
    path = runtime_path(root, SESSION_ID)
    path.unlink()
    config_before = config_path(root).read_bytes()
    lease_path = root / "managers" / f"{path.name}.lease"
    prepare_parent_directories(root, lease_path)
    lock = ExclusiveWriteLock(lease_path, timeout_seconds=0.0)
    lock.__enter__()

    # When/Then: activation fails closed without replacing configuration or runtime state.
    try:
        with pytest.raises(UnsafeStatePathError):
            _ = enable_session(root, activation, managed_report())
    finally:
        lock.__exit__(None, None, None)
    assert config_path(root).read_bytes() == config_before
    assert not path.exists()
