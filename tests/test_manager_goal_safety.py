from pathlib import Path

import pytest

from scripts.goal_control import GoalControlError
from scripts.manager_engine import ManagerEngine
from tests.test_manager_engine_goal import FakeGoalAppServer, runtime_fixture


def test_manager_goal_policy_precedes_native_goal_safety_transitions(tmp_path: Path) -> None:
    # Given a legacy runtime that would previously enter native Goal transitions.
    root, path = runtime_fixture(tmp_path, goal_companion=True)
    before = path.read_bytes()
    client = FakeGoalAppServer()
    engine = ManagerEngine(root, path.name, client, pid=123)

    # When manager initialization evaluates native Goal companionship.
    with pytest.raises(
        GoalControlError,
        match=r"^goal_companion_atomic_update_unavailable$",
    ):
        engine.initialize()

    # Then policy rejection precedes every native call and persisted mutation.
    assert client.calls == []
    assert path.read_bytes() == before
