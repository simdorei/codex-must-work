"""Persist and poll the exact activation-turn fence owned by the daemon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from scripts.activation_fence import (
    ActivationFence,
    ActivationFenceStatus,
    capture_activation_fence,
    recover_activation_fence,
)
from scripts.activity_epoch import advance_turn_activity_epoch
from scripts.daemon_models import DaemonServiceError
from scripts.hook_state import start_managed_parent
from scripts.manager_runtime_values import bump_revision
from scripts.state import mutate_existing_state, runtime_path
from scripts.watcher_source import initial_cursor, save_cursor
from scripts.watcher_state import mark_target_terminal

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


@dataclass(frozen=True, slots=True)
class PendingActivation:
    """Bind one exact activation turn to its canonical rollout."""

    session_id: str
    rollout: Path
    fence: ActivationFence


@final
class DaemonActivationFences:
    """Own short-lived incremental transcript fences for managed activation."""

    def __init__(self, root: Path) -> None:
        """Create an empty activation-fence registry for one private state root."""
        self._root = root
        self._pending: dict[str, PendingActivation] = {}

    def capture(self, session_id: str, rollout: Path) -> PendingActivation:
        """Capture the current open turn before activation mutates runtime state."""
        return PendingActivation(session_id, rollout, capture_activation_fence(rollout))

    def bind(self, pending: PendingActivation, runtime_file: Path, now: datetime) -> None:
        """Persist exact activation ownership and its initial incremental cursor."""
        timestamp = now.astimezone(UTC).isoformat().replace("+00:00", "Z")

        def update(values: dict[str, JsonValue]) -> None:
            start_managed_parent(
                values,
                pending.fence.turn_id,
                timestamp,
                runtime_file,
            )
            bump_revision(values, runtime_file)

        _ = mutate_existing_state(self._root, runtime_file, update)
        save_cursor(self._root, pending.session_id, pending.fence.cursor)
        self._pending[pending.session_id] = pending

    def recover(
        self,
        session_id: str,
        rollout: Path,
        turn_id: str,
    ) -> ActivationFenceStatus:
        """Rebuild one persisted exact-turn fence without adopting a newer turn."""
        status, fence = recover_activation_fence(rollout, turn_id)
        self._pending[session_id] = PendingActivation(session_id, rollout, fence)
        return status

    def poll(self, session_id: str) -> ActivationFenceStatus:
        """Read only bytes appended after the last activation-fence check."""
        pending = self._require(session_id)
        status, fence = pending.fence.poll(pending.rollout)
        self._pending[session_id] = PendingActivation(session_id, pending.rollout, fence)
        return status

    def complete(self, session_id: str) -> None:
        """Publish first handoff only for the exact completed activation turn."""
        pending = self._require(session_id)
        runtime_file = runtime_path(self._root, session_id)
        save_cursor(self._root, session_id, pending.fence.cursor)

        def update(values: dict[str, JsonValue]) -> None:
            if values.get("parent_turn_id") != pending.fence.turn_id:
                reason = "activation_turn_ownership_changed"
                raise DaemonServiceError(reason)
            if values.get("managed_turn_id") is not None:
                reason = "activation_turn_ownership_changed"
                raise DaemonServiceError(reason)
            _ = mark_target_terminal(values, None, runtime_file)
            values["handoff_requested"] = True
            values["parent_complete"] = False
            bump_revision(values, runtime_file)

        _ = mutate_existing_state(self._root, runtime_file, update)
        _ = self._pending.pop(session_id, None)

    def adopt_started(
        self,
        session_id: str,
        rollout: Path,
        turn_id: str,
        now: datetime,
    ) -> bool:
        """Fence one exact user turn that started before a queued handoff."""
        runtime_file = runtime_path(self._root, session_id)
        timestamp = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        pending = PendingActivation(
            session_id,
            rollout,
            ActivationFence(turn_id, initial_cursor(rollout)),
        )

        def update(values: dict[str, JsonValue]) -> bool:
            if (
                values.get("enabled") is not True
                or values.get("managed_mode") is not True
                or values.get("goal_companion", False) is True
                or values.get("shutdown_requested") is True
                or values.get("handoff_requested") is not True
                or values.get("managed_turn_id") is not None
                or values.get("pending_turn_id") is not None
            ):
                return False
            start_managed_parent(values, turn_id, timestamp, runtime_file)
            advance_turn_activity_epoch(values, runtime_file)
            values["handoff_requested"] = False
            bump_revision(values, runtime_file)
            return True

        adopted = mutate_existing_state(self._root, runtime_file, update) is True
        if adopted:
            self._pending[session_id] = pending
        return adopted

    def is_pending(self, session_id: str | None) -> bool:
        """Return whether one session is still fenced on activation completion."""
        return session_id in self._pending

    def turn_id(self, session_id: str) -> str | None:
        """Return the exact activation turn retained for one session."""
        pending = self._pending.get(session_id)
        return None if pending is None else pending.fence.turn_id

    def discard(self, session_id: str) -> None:
        """Forget only the in-memory fence for a removed task."""
        _ = self._pending.pop(session_id, None)

    def clear(self) -> None:
        """Forget all in-memory fences during daemon shutdown."""
        self._pending.clear()

    def _require(self, session_id: str) -> PendingActivation:
        pending = self._pending.get(session_id)
        if pending is None:
            reason = "activation_fence_missing"
            raise DaemonServiceError(reason)
        return pending
