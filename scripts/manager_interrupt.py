"""Perform one exact-turn interrupt behind all restart freshness gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from scripts.app_server_protocol import TurnOutcome
from scripts.manager_restart_guard import (
    claim_restart_request,
    clear_restart_request,
    restart_request_is_fresh,
)
from scripts.manager_runtime import ManagerRuntime, record_restart_performed, record_turn_finished

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.app_server_protocol import ManagedAppServer
    from scripts.manager_goal import GoalGuard


@dataclass(frozen=True, slots=True)
class InterruptResult:
    """Report whether interruption restarted the task or failed closed."""

    restarted: bool = False
    failure_reason: str | None = None


@final
class InterruptController:
    """Own one exact interrupt behind persisted freshness evidence."""

    def __init__(self, root: Path, client: ManagedAppServer) -> None:
        """Bind restart state and exact-turn control to one owner connection."""
        self._root = root
        self._client = client

    def execute(
        self,
        runtime: ManagerRuntime,
        turn_id: str,
        goal_guard: GoalGuard | None,
    ) -> InterruptResult:
        """Claim, revalidate, and interrupt one exactly owned turn."""
        if not claim_restart_request(self._root, runtime):
            return InterruptResult()
        if goal_guard is not None:
            goal_guard.pause_for_interrupt()
        if not restart_request_is_fresh(self._root, runtime):
            return self._cancel(runtime, turn_id)
        active_turn = self._client.active_turn(runtime.session_id)
        if active_turn is None:
            return self._cancel(runtime, turn_id)
        if active_turn != turn_id:
            return InterruptResult(failure_reason="active_turn_mismatch")
        _ = self._client.request(
            "turn/interrupt",
            {"threadId": runtime.session_id, "turnId": turn_id},
            timeout_seconds=10.0,
        )
        if not self._client.wait_turn_completed(turn_id):
            return InterruptResult(failure_reason="interrupt_timeout")
        return self._finish_interrupt(runtime, turn_id)

    def _finish_interrupt(
        self,
        runtime: ManagerRuntime,
        turn_id: str,
    ) -> InterruptResult:
        outcome = self._client.turn_outcome(turn_id)
        if outcome is TurnOutcome.FAILED:
            return InterruptResult(failure_reason="turn_failed")
        if outcome is TurnOutcome.COMPLETED:
            return InterruptResult()
        if outcome is TurnOutcome.IN_PROGRESS or outcome is TurnOutcome.INVALID:
            return InterruptResult(failure_reason="turn_status_invalid")
        if outcome is not TurnOutcome.INTERRUPTED:
            return InterruptResult(failure_reason="turn_interrupted_external")
        if runtime.message_preset == "cleanup":
            _ = self._client.request(
                "thread/backgroundTerminals/clean",
                {"threadId": runtime.session_id},
                timeout_seconds=10.0,
            )
        record_restart_performed(self._root, runtime.runtime_file, turn_id)
        return InterruptResult(restarted=True)

    def _cancel(
        self,
        runtime: ManagerRuntime,
        turn_id: str,
    ) -> InterruptResult:
        if self._client.turn_completed(turn_id):
            record_turn_finished(self._root, runtime.runtime_file, turn_id)
        else:
            clear_restart_request(self._root, runtime)
        return InterruptResult()
