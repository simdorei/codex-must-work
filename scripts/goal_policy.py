"""Fail closed while native Goal mutation cannot be made atomic."""

from dataclasses import dataclass
from typing import Final, override

GOAL_COMPANION_ATOMIC_UPDATE_UNAVAILABLE: Final = "goal_companion_atomic_update_unavailable"


@dataclass(slots=True)
class GoalControlError(RuntimeError):
    """Expose one fixed public Goal policy or protocol reason."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


def enforce_goal_companion_policy(*, requested: bool) -> None:
    """Reject requests that would require unsafe native Goal mutation."""
    if requested:
        raise GoalControlError(GOAL_COMPANION_ATOMIC_UPDATE_UNAVAILABLE)
