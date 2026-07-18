from typing import final

import pytest

from scripts.goal_control import GoalControlError, GoalStatus, read_goal, read_goal_status
from scripts.state_io import JsonValue


@final
class FakeGoalClient:
    def __init__(self, response: dict[str, JsonValue]) -> None:
        self.response = response

    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        _ = method
        _ = params
        _ = timeout_seconds
        return self.response


def test_read_goal_status_parses_active_goal() -> None:
    client = FakeGoalClient(
        {
            "goal": {
                "status": "active",
                "threadId": "thread-1",
                "createdAt": 10,
                "objective": "finish",
                "tokenBudget": None,
            }
        }
    )

    status = read_goal_status(client, "thread-1")

    assert status is GoalStatus.ACTIVE


def test_read_goal_status_rejects_missing_goal() -> None:
    client = FakeGoalClient({"goal": None})

    with pytest.raises(GoalControlError, match="goal_missing"):
        _ = read_goal_status(client, "thread-1")


def test_read_goal_rejects_cross_thread_goal() -> None:
    client = FakeGoalClient(
        {
            "goal": {
                "status": "paused",
                "threadId": "thread-other",
                "createdAt": 10,
                "objective": "finish",
                "tokenBudget": None,
            }
        }
    )

    with pytest.raises(GoalControlError, match="goal_identity_invalid"):
        _ = read_goal(client, "thread-1")
