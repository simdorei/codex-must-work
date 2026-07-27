import pytest

from scripts.goal_control import GoalControlError
from scripts.manager_goal import GoalGuard
from tests.test_manager_engine_goal import FakeGoalAppServer

_UNAVAILABLE = "goal_companion_atomic_update_unavailable"


def test_replacement_goal_race_is_blocked_before_any_native_request() -> None:
    # Given a Goal identity that could be replaced after an initial read.
    client = FakeGoalAppServer()
    original_identity = client.goal_created_at
    guard = GoalGuard(client, "thread-1")

    # When the direct Goal guard attempts initialization.
    with pytest.raises(GoalControlError, match=f"^{_UNAVAILABLE}$"):
        guard.initialize()

    # Then no read or mutation opened a race window against another Goal.
    assert client.calls == []
    assert client.goal_created_at == original_identity
    assert client.goal_status == "active"
