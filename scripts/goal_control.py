"""Parse and update the persisted Goal attached to one Codex thread."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Protocol, override

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

type JsonObject = dict[str, JsonValue]


class GoalClient(Protocol):
    """Describe the app-server request used by Goal control."""

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = 10.0,
    ) -> JsonObject:
        """Send one Goal app-server request and return its object result."""
        ...


@unique
class GoalStatus(StrEnum):
    """Published app-server Goal states."""

    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usageLimited"
    BUDGET_LIMITED = "budgetLimited"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class GoalControlError(RuntimeError):
    """Report a missing or malformed Goal control response."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


@dataclass(frozen=True, slots=True)
class GoalIdentity:
    """Immutable fields used to reject a replacement Goal."""

    thread_id: str
    created_at: int
    objective: str
    token_budget: int | None


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    """Validated Goal identity and current mutable status."""

    identity: GoalIdentity
    status: GoalStatus


def read_goal(client: GoalClient, thread_id: str) -> GoalSnapshot:
    """Read and bind one Goal to the requested thread."""
    result = client.request("thread/goal/get", {"threadId": thread_id})
    return _goal_from_result(result, thread_id)


def read_goal_status(client: GoalClient, thread_id: str) -> GoalStatus:
    """Read one required Goal status through the supported app-server API."""
    return read_goal(client, thread_id).status


def set_goal_status(
    client: GoalClient,
    thread_id: str,
    status: GoalStatus,
) -> GoalSnapshot:
    """Set one Goal status and return the response identity for caller validation."""
    result = client.request(
        "thread/goal/set",
        {"threadId": thread_id, "status": status.value},
    )
    goal = _goal_from_result(result, thread_id)
    if goal.status is not status:
        message = "goal_status_mismatch"
        raise GoalControlError(message)
    return goal


def _goal_from_result(result: JsonObject, requested_thread_id: str) -> GoalSnapshot:
    goal = result.get("goal")
    if not isinstance(goal, dict):
        message = "goal_missing"
        raise GoalControlError(message)
    raw_status = goal.get("status")
    if not isinstance(raw_status, str):
        message = "goal_status_invalid"
        raise GoalControlError(message)
    try:
        status = GoalStatus(raw_status)
    except ValueError as error:
        message = "goal_status_invalid"
        raise GoalControlError(message) from error
    thread_id = goal.get("threadId")
    created_at = goal.get("createdAt")
    objective = goal.get("objective")
    token_budget = goal.get("tokenBudget")
    valid_budget = token_budget is None or (type(token_budget) is int and token_budget >= 0)
    if (
        not isinstance(thread_id, str)
        or thread_id != requested_thread_id
        or type(created_at) is not int
        or created_at < 0
        or not isinstance(objective, str)
        or not valid_budget
    ):
        message = "goal_identity_invalid"
        raise GoalControlError(message)
    validated_budget = token_budget if type(token_budget) is int else None
    return GoalSnapshot(
        GoalIdentity(thread_id, created_at, objective, validated_budget),
        status,
    )
