from pathlib import Path

import pytest

from scripts.manager import run_manager
from scripts.manager_lease import ManagerLease, acquire_manager_lease
from scripts.private_root import ensure_private_root
from scripts.setup import request_session_shutdown
from scripts.state import load_state
from tests.test_manager_engine_goal import FakeGoalAppServer, runtime_fixture


def test_legacy_goal_companion_records_diagnostic_without_native_calls_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a persisted legacy Goal companion runtime.
    ensure_private_root(tmp_path / "codex-must-work")
    root, runtime_path = runtime_fixture(tmp_path, goal_companion=True)
    client = FakeGoalAppServer()
    lease_attempts: list[str] = []
    timer_waits: list[float] = []

    def client_factory(_digest: str) -> FakeGoalAppServer:
        return client

    def tracked_lease(lease_root: Path, runtime_name: str) -> ManagerLease | None:
        lease_attempts.append(runtime_name)
        return acquire_manager_lease(lease_root, runtime_name)

    monkeypatch.setattr("scripts.manager.state_root", lambda: root)
    monkeypatch.setattr("scripts.manager.ResidentAppServer", client_factory)
    monkeypatch.setattr("scripts.manager.acquire_manager_lease", tracked_lease)
    monkeypatch.setattr("scripts.manager.time.sleep", timer_waits.append)

    # When the resident manager recovers it and the user then requests shutdown.
    exit_code = run_manager(runtime_path.name)
    failed = load_state(root, runtime_path).values
    request_session_shutdown(root, "thread-1", interrupt_active=True)

    # Then recovery is diagnostic-only, performs no native call, and remains stoppable.
    assert exit_code == 1
    assert failed["manager_error"] == "goal_companion_atomic_update_unavailable"
    assert lease_attempts == []
    assert timer_waits == []
    assert client.calls == []
    assert not runtime_path.exists()
