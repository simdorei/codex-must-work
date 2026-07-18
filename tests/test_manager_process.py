from __future__ import annotations

from typing import TYPE_CHECKING, final

import pytest

from scripts.goal_control import GoalControlError
from scripts.manager import run_manager
from scripts.manager_lease import ManagerLease
from scripts.state import load_state
from scripts.state_io import ExclusiveWriteLock
from tests.test_manager_engine_goal import runtime_fixture

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.app_server_protocol import ManagedAppServer

_GOAL_NOT_RESUMABLE = "goal_not_resumable"


@final
class FakeClient:
    def __init__(self) -> None:
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True


@final
class FailingEngine:
    latest: FailingEngine | None = None

    def __init__(self) -> None:
        self.closed: bool = False
        FailingEngine.latest = self

    def initialize(self) -> None:
        raise GoalControlError(_GOAL_NOT_RESUMABLE)

    def tick(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


@final
class UnexpectedCleanupError(LookupError):
    pass


@final
class UnexpectedCloseEngine:
    def initialize(self) -> None:
        raise GoalControlError(_GOAL_NOT_RESUMABLE)

    def tick(self) -> bool:
        return False

    def close(self) -> None:
        raise UnexpectedCleanupError


def test_manager_initialization_failure_preserves_reason_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeClient()
    released: list[ManagerLease] = []
    lease = ManagerLease(tmp_path / "lease", ExclusiveWriteLock(tmp_path / "lease"))

    def acquire(_root: Path, _runtime_name: str) -> ManagerLease:
        return lease

    def release(value: ManagerLease) -> None:
        released.append(value)

    def make_client(_digest: str) -> FakeClient:
        return client

    def make_engine(
        _root: Path,
        _runtime_name: str,
        _client: ManagedAppServer,
        *,
        pid: int,
    ) -> FailingEngine:
        _ = pid
        return FailingEngine()

    def secure(_root: Path) -> None:
        return

    monkeypatch.setattr("scripts.manager.state_root", lambda: root)
    monkeypatch.setattr("scripts.manager.ensure_private_root", secure)
    monkeypatch.setattr("scripts.manager.acquire_manager_lease", acquire)
    monkeypatch.setattr("scripts.manager.release_manager_lease", release)
    monkeypatch.setattr("scripts.manager.ResidentAppServer", make_client)
    monkeypatch.setattr("scripts.manager.ManagerEngine", make_engine)

    exit_code = run_manager(path.name)

    runtime = load_state(root, path).values
    assert exit_code == 1
    assert runtime["manager_error"] == "goal_not_resumable"
    assert client.closed is True
    assert FailingEngine.latest is not None
    assert FailingEngine.latest.closed is True
    assert released == [lease]


def test_unexpected_engine_cleanup_failure_still_closes_client_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeClient()
    released: list[ManagerLease] = []
    lease = ManagerLease(tmp_path / "lease", ExclusiveWriteLock(tmp_path / "lease"))

    def secure(_root: Path) -> None:
        return

    def acquire(_root: Path, _name: str) -> ManagerLease:
        return lease

    def make_client(_digest: str) -> FakeClient:
        return client

    def make_engine(
        _root: Path,
        _name: str,
        _client: ManagedAppServer,
        *,
        pid: int,
    ) -> UnexpectedCloseEngine:
        _ = pid
        return UnexpectedCloseEngine()

    monkeypatch.setattr("scripts.manager.state_root", lambda: root)
    monkeypatch.setattr("scripts.manager.ensure_private_root", secure)
    monkeypatch.setattr("scripts.manager.acquire_manager_lease", acquire)
    monkeypatch.setattr("scripts.manager.release_manager_lease", released.append)
    monkeypatch.setattr("scripts.manager.ResidentAppServer", make_client)
    monkeypatch.setattr("scripts.manager.ManagerEngine", make_engine)

    with pytest.raises(UnexpectedCleanupError):
        _ = run_manager(path.name)

    assert client.closed is True
    assert released == [lease]
