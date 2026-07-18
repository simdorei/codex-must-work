"""Finish or interrupt a managed runtime during explicit shutdown."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.manager_runtime import ManagerRuntime
from scripts.setup import disable_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.app_server_protocol import ManagedAppServer
    from scripts.manager_goal import GoalGuard

type FailureHandler = Callable[[ManagerRuntime, str], bool]


def handle_shutdown(
    root: Path,
    runtime: ManagerRuntime,
    client: ManagedAppServer,
    goal_guard: GoalGuard | None,
    fail: FailureHandler,
) -> bool:
    """Wait for normal completion or interrupt only the exact owned turn."""
    turn_id = runtime.view.managed_turn_id
    if turn_id is None:
        disable_session(root, runtime.session_id)
        return False
    if not runtime.shutdown_interrupt:
        return True
    if client.active_turn(runtime.session_id) != turn_id:
        return fail(runtime, "active_turn_mismatch")
    if runtime.view.goal_companion:
        if goal_guard is None:
            return fail(runtime, "goal_identity_missing")
        goal_guard.pause_for_interrupt()
    _ = client.request(
        "turn/interrupt",
        {"threadId": runtime.session_id, "turnId": turn_id},
        timeout_seconds=10.0,
    )
    if not client.wait_turn_completed(turn_id):
        return fail(runtime, "interrupt_timeout")
    if runtime.message_preset == "cleanup":
        _ = client.request(
            "thread/backgroundTerminals/clean",
            {"threadId": runtime.session_id},
            timeout_seconds=10.0,
        )
    disable_session(root, runtime.session_id)
    return False
