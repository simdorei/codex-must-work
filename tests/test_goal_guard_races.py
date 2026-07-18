from typing import final, override

import pytest

from scripts.app_server_protocol import AppServerProtocolError
from scripts.goal_control import GoalControlError
from scripts.manager_goal import GoalGuard
from scripts.state_io import JsonValue
from tests.test_manager_engine_goal import FakeGoalAppServer


@final
class LostResponseGoalServer(FakeGoalAppServer):
    def __init__(self, lost_status: str) -> None:
        super().__init__()
        self._lost_status: str = lost_status
        self._lost: bool = False

    @override
    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        result = super().request(method, params, timeout_seconds=timeout_seconds)
        if (
            method == "thread/goal/set"
            and params.get("status") == self._lost_status
            and not self._lost
        ):
            self._lost = True
            message = "response_lost"
            raise AppServerProtocolError(message)
        return result


@final
class ReplacementDuringPauseServer(FakeGoalAppServer):
    def __init__(self, *, lose_response: bool = False) -> None:
        super().__init__()
        self._replaced: bool = False
        self._lose_response = lose_response

    @override
    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        replacing = (
            method == "thread/goal/set" and params.get("status") == "paused" and not self._replaced
        )
        if replacing:
            self._replaced = True
            self.goal_created_at += 1
        result = super().request(method, params, timeout_seconds=timeout_seconds)
        if replacing and self._lose_response:
            message = "replacement_response_lost"
            raise AppServerProtocolError(message)
        return result


def test_lost_pause_response_is_reconciled_and_still_restorable() -> None:
    client = LostResponseGoalServer("paused")
    guard = GoalGuard(client, "thread-1")

    guard.initialize()
    assert client.goal_status == "paused"

    _ = guard.restore_initial_active()
    assert client.goal_status == "active"


def test_lost_activation_response_is_reconciled_before_restore_ownership_clears() -> None:
    client = LostResponseGoalServer("active")
    guard = GoalGuard(client, "thread-1")
    guard.initialize()

    guard.activate_for_handoff()

    assert client.goal_status == "active"
    assert guard.restore_active is False


def test_replacement_race_fails_closed_without_a_second_goal_mutation() -> None:
    client = ReplacementDuringPauseServer()
    guard = GoalGuard(client, "thread-1")

    with pytest.raises(GoalControlError, match="goal_identity_changed"):
        guard.initialize()

    assert client.goal_created_at == 11
    assert client.goal_status == "paused"


def test_lost_replacement_response_also_fails_closed_without_compensation() -> None:
    client = ReplacementDuringPauseServer(lose_response=True)
    guard = GoalGuard(client, "thread-1")

    with pytest.raises(GoalControlError, match="goal_identity_changed"):
        guard.initialize()

    assert client.goal_created_at == 11
    assert client.goal_status == "paused"
