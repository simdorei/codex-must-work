from pathlib import Path

import pytest

from scripts.goal_control import GoalControlError
from scripts.manager_engine import ManagerEngine
from scripts.manager_goal import GoalGuard
from scripts.setup import enable_session
from tests.test_manager_engine_goal import FakeGoalAppServer, runtime_fixture
from tests.test_setup import managed_report, request

_UNAVAILABLE = "goal_companion_atomic_update_unavailable"


def test_trusted_setup_rejects_goal_companion_before_state_mutation(tmp_path: Path) -> None:
    # Given a trusted local activation request for native Goal companionship.
    root = tmp_path / "state"
    activation = request(root, observe_only=False, goal_companion=True)

    # When setup evaluates the request.
    with pytest.raises(GoalControlError, match=f"^{_UNAVAILABLE}$"):
        _ = enable_session(root, activation, managed_report())

    # Then no state root or runtime artifact has been created.
    assert not root.exists()


def test_legacy_goal_runtime_fails_before_app_server_or_state_mutation(tmp_path: Path) -> None:
    # Given a persisted legacy runtime that requested native Goal companionship.
    root, runtime_path = runtime_fixture(tmp_path, goal_companion=True)
    before = runtime_path.read_bytes()
    client = FakeGoalAppServer()
    engine = ManagerEngine(root, runtime_path.name, client, pid=123)

    # When manager recovery evaluates the persisted request.
    with pytest.raises(GoalControlError, match=f"^{_UNAVAILABLE}$"):
        engine.initialize()

    # Then recovery made no app-server request and changed no persisted state.
    assert client.calls == []
    assert runtime_path.read_bytes() == before


def test_direct_goal_guard_rejects_before_native_goal_request() -> None:
    # Given a direct Python Goal guard entrypoint.
    client = FakeGoalAppServer()
    guard = GoalGuard(client, "thread-1")

    # When native Goal initialization is requested.
    with pytest.raises(GoalControlError, match=f"^{_UNAVAILABLE}$"):
        guard.initialize()

    # Then even the read-only native Goal request was not sent.
    assert client.calls == []
