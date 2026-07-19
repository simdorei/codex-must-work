from pathlib import Path
from types import SimpleNamespace
from typing import cast, final

import pytest

from scripts.manager_launch import (
    MANAGER_INITIALIZATION_BUDGET_SECONDS,
    MANAGER_READY_TIMEOUT_SECONDS,
    ManagerLaunchError,
    launch_manager,
)
from scripts.manager_runtime import ManagerRuntime


@final
class FakeProcess:
    pid: int = 321

    def __init__(self) -> None:
        self.terminated: bool = False
        self.killed: bool = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, *, timeout: float) -> int:
        _ = timeout
        return 0


def _install_process(monkeypatch: pytest.MonkeyPatch) -> FakeProcess:
    process = FakeProcess()

    def spawn(_command: list[str], _creation_flags: int) -> FakeProcess:
        return process

    monkeypatch.setattr("scripts.manager_launch._spawn_manager", spawn)
    return process


def _runtime(*, error: str | None, ready: bool, pid: int | None = None) -> ManagerRuntime:
    return cast(
        "ManagerRuntime",
        cast(
            "object",
            SimpleNamespace(manager_error=error, manager_ready=ready, manager_pid=pid),
        ),
    )


def test_readiness_timeout_requests_cleanup_without_hard_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _install_process(monkeypatch)
    runtime = _runtime(error=None, ready=False)

    def load(_root: Path, _name: str) -> ManagerRuntime:
        return runtime

    monkeypatch.setattr("scripts.manager_launch.load_manager_runtime", load)
    cancellations: list[Path] = []

    def cancel(_root: Path, path: Path) -> None:
        cancellations.append(path)

    monkeypatch.setattr(
        "scripts.manager_launch.request_manager_startup_cancel",
        cancel,
    )
    moments = iter((0.0, 1.0))
    monkeypatch.setattr("scripts.manager_launch.time.monotonic", lambda: next(moments))

    with pytest.raises(ManagerLaunchError, match="manager_ready_timeout"):
        _ = launch_manager(tmp_path, tmp_path / "runtime.json", timeout_seconds=0.5)

    assert cancellations == [tmp_path / "runtime.json"]
    assert process.terminated is False
    assert process.killed is False


def test_reported_manager_error_is_left_to_manager_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _install_process(monkeypatch)
    runtime = _runtime(error="goal_not_resumable", ready=False)

    def load(_root: Path, _name: str) -> ManagerRuntime:
        return runtime

    monkeypatch.setattr("scripts.manager_launch.load_manager_runtime", load)
    monkeypatch.setattr("scripts.manager_launch.time.monotonic", lambda: 0.0)

    with pytest.raises(ManagerLaunchError, match="goal_not_resumable"):
        _ = launch_manager(tmp_path, tmp_path / "runtime.json", timeout_seconds=0.5)

    assert process.terminated is False


def test_ready_manager_is_left_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _install_process(monkeypatch)
    runtime = _runtime(error=None, ready=True, pid=process.pid)

    def load(_root: Path, _name: str) -> ManagerRuntime:
        return runtime

    def owner(_root: Path, _name: str) -> int:
        return process.pid

    monkeypatch.setattr("scripts.manager_launch.load_manager_runtime", load)
    monkeypatch.setattr(
        "scripts.manager_launch.manager_lease_owner",
        owner,
        raising=False,
    )
    monkeypatch.setattr("scripts.manager_launch.time.monotonic", lambda: 0.0)

    pid = launch_manager(tmp_path, tmp_path / "runtime.json", timeout_seconds=0.5)

    assert pid == process.pid
    assert process.terminated is False


def test_ready_state_from_different_process_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: stale state says a different resident process is ready.
    process = _install_process(monkeypatch)
    stale_pid = process.pid + 1
    runtime = _runtime(error=None, ready=True, pid=stale_pid)

    def load(_root: Path, _name: str) -> ManagerRuntime:
        return runtime

    def owner(_root: Path, _name: str) -> int:
        return stale_pid

    monkeypatch.setattr("scripts.manager_launch.load_manager_runtime", load)
    monkeypatch.setattr(
        "scripts.manager_launch.manager_lease_owner",
        owner,
        raising=False,
    )
    monkeypatch.setattr("scripts.manager_launch.time.monotonic", lambda: 0.0)

    # When/Then: launch rejects readiness that does not belong to its child process.
    with pytest.raises(ManagerLaunchError, match="manager_owner_mismatch"):
        _ = launch_manager(tmp_path, tmp_path / "runtime.json", timeout_seconds=0.5)


def test_default_readiness_timeout_exceeds_all_initialization_request_budgets() -> None:
    assert MANAGER_READY_TIMEOUT_SECONDS > MANAGER_INITIALIZATION_BUDGET_SECONDS
