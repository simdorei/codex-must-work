"""Guard every Goal mutation with one captured Goal identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never, final

from scripts.app_server_protocol import AppServerProtocolError
from scripts.goal_control import (
    GoalControlError,
    GoalIdentity,
    GoalSnapshot,
    GoalStatus,
    read_goal,
    set_goal_status,
)

if TYPE_CHECKING:
    from scripts.app_server_protocol import ManagedAppServer

_NOT_RESUMABLE = "goal_not_resumable"
_COMPLETE = "goal_complete"
_IDENTITY_CHANGED = "goal_identity_changed"
_IDENTITY_MISSING = "goal_identity_missing"
_HANDOFF_CHANGED = "goal_handoff_changed"


def fence_goal_handoff(
    client: ManagedAppServer,
    guard: GoalGuard,
    thread_id: str,
    observed_turn: str,
) -> str:
    """Pause Goal scheduling and select the last turn started in that active window."""
    guard.pause_for_interrupt()
    active = client.active_turn(thread_id)
    if active == observed_turn:
        return observed_turn
    if not client.turn_completed(observed_turn):
        raise GoalControlError(_HANDOFF_CHANGED)
    latest = client.latest_started_turn(thread_id)
    if active is not None:
        if active == latest:
            return active
        raise GoalControlError(_HANDOFF_CHANGED)
    if latest is None or latest == observed_turn:
        return observed_turn
    if client.turn_completed(latest):
        return latest
    raise GoalControlError(_HANDOFF_CHANGED)


def require_goal_guard(guard: GoalGuard | None) -> GoalGuard:
    """Return the initialized Goal guard or fail closed."""
    if guard is None:
        raise GoalControlError(_IDENTITY_MISSING)
    return guard


@final
class GoalGuard:
    """Restrict one manager to the exact Goal present at initialization."""

    def __init__(self, client: ManagedAppServer, thread_id: str) -> None:
        """Bind control to one app-server connection and thread."""
        self.client = client
        self.thread_id = thread_id
        self.identity: GoalIdentity | None = None
        self.restore_active: bool = False

    def initialize(self) -> None:
        """Capture the initial identity and pause only an active Goal."""
        goal = read_goal(self.client, self.thread_id)
        self.identity = goal.identity
        match goal.status:
            case GoalStatus.ACTIVE:
                self.restore_active = True
                self._set(GoalStatus.PAUSED)
            case GoalStatus.PAUSED:
                pass
            case GoalStatus.BLOCKED | GoalStatus.USAGE_LIMITED | GoalStatus.BUDGET_LIMITED:
                raise GoalControlError(_NOT_RESUMABLE)
            case GoalStatus.COMPLETE:
                raise GoalControlError(_COMPLETE)
            case _:
                assert_never(goal.status)

    def activate_for_handoff(self) -> None:
        """Reactivate only the captured resumable Goal."""
        goal = self._read_bound()
        match goal.status:
            case GoalStatus.PAUSED:
                self._set(GoalStatus.ACTIVE)
            case GoalStatus.ACTIVE:
                pass
            case GoalStatus.BLOCKED | GoalStatus.USAGE_LIMITED | GoalStatus.BUDGET_LIMITED:
                raise GoalControlError(_NOT_RESUMABLE)
            case GoalStatus.COMPLETE:
                raise GoalControlError(_COMPLETE)
            case _:
                assert_never(goal.status)
        self.restore_active = False

    def pause_for_interrupt(self) -> None:
        """Pause Goal scheduling without interrupting its current turn."""
        goal = self._read_bound()
        match goal.status:
            case GoalStatus.ACTIVE:
                self.restore_active = True
                self._set(GoalStatus.PAUSED)
            case GoalStatus.PAUSED:
                pass
            case GoalStatus.BLOCKED | GoalStatus.USAGE_LIMITED | GoalStatus.BUDGET_LIMITED:
                raise GoalControlError(_NOT_RESUMABLE)
            case GoalStatus.COMPLETE:
                raise GoalControlError(_COMPLETE)
            case _:
                assert_never(goal.status)

    def restore_initial_active(self) -> bool:
        """Undo only the temporary pause made during initialization."""
        if not self.restore_active:
            return False
        goal = self._read_bound()
        if goal.status is GoalStatus.PAUSED:
            self._set(GoalStatus.ACTIVE)
        self.restore_active = False
        return True

    def keep_paused_on_exit(self) -> None:
        """Prevent fatal cleanup from opening an unowned continuation window."""
        self.restore_active = False

    def status_after_turn(self) -> GoalStatus:
        """Classify the exact bound Goal without mutating its status."""
        return self._read_bound().status

    def _read_bound(self) -> GoalSnapshot:
        goal = read_goal(self.client, self.thread_id)
        if self.identity is None or goal.identity != self.identity:
            raise GoalControlError(_IDENTITY_CHANGED)
        return goal

    def _set(self, status: GoalStatus) -> None:
        if self.identity is None:
            raise GoalControlError(_IDENTITY_MISSING)
        try:
            goal = set_goal_status(self.client, self.thread_id, status)
        except (AppServerProtocolError, GoalControlError):
            goal = read_goal(self.client, self.thread_id)
            if goal.identity == self.identity and goal.status is status:
                return
            if goal.identity != self.identity:
                raise GoalControlError(_IDENTITY_CHANGED) from None
            raise
        if goal.identity != self.identity:
            raise GoalControlError(_IDENTITY_CHANGED)
