"""Discover persisted CMW tasks for one newly started daemon."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.activation_fence import ActivationFenceStatus
from scripts.daemon_models import SessionId
from scripts.manager_runtime import load_manager_runtime
from scripts.manager_runtime_values import int_value, string_value
from scripts.setup import disable_session
from scripts.state import load_state
from scripts.watcher_state import discover_runtime_files, runtime_target_from_values

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.daemon_activation_fence import DaemonActivationFences


@dataclass(frozen=True, slots=True)
class PersistedTask:
    """Validated state required to rebuild one in-process task owner."""

    session_id: SessionId
    runtime_name: str
    executable_sha256: str
    warning_seconds: float
    restart_seconds: float
    managed: bool
    rollout_file: Path
    activation_generation: int
    transcript_path: str
    activation_pending: bool
    activation_turn_id: str | None


def discover_persisted_tasks(root: Path) -> tuple[PersistedTask, ...]:
    """Return only enabled tasks after applying existing state validators."""
    recovered: list[PersistedTask] = []
    for path in discover_runtime_files(root):
        values = load_state(root, path).values
        if values.get("enabled") is not True:
            continue
        target = runtime_target_from_values(root, path, values)
        runtime = load_manager_runtime(root, path.name)
        if runtime is not None and runtime.manager_error is not None:
            continue
        activation_pending = (
            target.managed_mode
            and runtime is not None
            and not runtime.view.handoff_requested
            and runtime.view.managed_turn_id is None
        )
        activation_turn_id = target.parent_turn_id if activation_pending else None
        recovered.append(
            PersistedTask(
                session_id=SessionId(target.session_id),
                runtime_name=path.name,
                executable_sha256=string_value(values, "executable_sha256", path),
                warning_seconds=target.thresholds.warning,
                restart_seconds=target.thresholds.restart,
                managed=target.managed_mode,
                rollout_file=target.rollout_file,
                activation_generation=int_value(values, "revision", path, minimum=0),
                transcript_path=string_value(values, "transcript_path", path),
                activation_pending=activation_pending,
                activation_turn_id=activation_turn_id,
            )
        )
    return tuple(recovered)


def recover_activation_fence(
    root: Path,
    fences: DaemonActivationFences,
    saved: PersistedTask,
) -> bool:
    """Recover or safely discard one exact activation-turn fence."""
    if not saved.activation_pending:
        return True
    if saved.activation_turn_id is None:
        disable_session(root, saved.session_id)
        return False
    status = fences.recover(
        saved.session_id,
        saved.rollout_file,
        saved.activation_turn_id,
    )
    if status is ActivationFenceStatus.SUPERSEDED:
        fences.discard(saved.session_id)
        disable_session(root, saved.session_id)
        return False
    if status in {ActivationFenceStatus.COMPLETED, ActivationFenceStatus.ABORTED}:
        fences.complete(saved.session_id)
    return True
