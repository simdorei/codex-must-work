"""Coordinate runtime discovery, rollout reads, and locked snapshot commits."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from scripts.monitor_diagnostics import DiagnosticCode
from scripts.monitor_state import (
    discover_runtime_files,
    runtime_target_from_values,
)
from scripts.state import mutate_existing_state
from scripts.watcher_batch import read_target_batch
from scripts.watcher_commit import commit_runtime_snapshot
from scripts.watcher_failure import record_rollout_failure
from scripts.watcher_source import RolloutCorruptError, RolloutRotatedError

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.monitor_target import RuntimeTarget
    from scripts.state_io import JsonValue
    from scripts.watcher_commit import TargetProcessor
    from scripts.watcher_context import TickContext


@final
class RuntimeFileMonitor:
    """Maintain process-local failure quarantine while scanning runtime files."""

    def __init__(self, root: Path) -> None:
        """Bind runtime discovery and failure diagnostics to one state root."""
        self._root = root
        self._failed_sessions: set[str] = set()

    def tick(self, context: TickContext, processor: TargetProcessor) -> bool:
        """Process one bounded snapshot for every active runtime file."""
        active = False
        for runtime_file in discover_runtime_files(self._root):

            def snapshot(
                values: dict[str, JsonValue],
                runtime_file: Path = runtime_file,
            ) -> RuntimeTarget | None:
                if values.get("enabled") is not True:
                    return None
                return runtime_target_from_values(self._root, runtime_file, values)

            target = mutate_existing_state(self._root, runtime_file, snapshot)
            if target is None or target.session_id in self._failed_sessions:
                continue
            if target.parent_complete:
                active = commit_runtime_snapshot(self._root, target, None, processor) or active
                continue
            if not any(not monitor.terminal for monitor in target.targets):
                continue
            try:
                batch = read_target_batch(self._root, target)
            except RolloutCorruptError:
                record_rollout_failure(
                    self._root,
                    target,
                    DiagnosticCode.ROLLOUT_CORRUPT,
                    context.wall_time,
                    self._failed_sessions,
                )
                continue
            except RolloutRotatedError:
                record_rollout_failure(
                    self._root,
                    target,
                    DiagnosticCode.ROLLOUT_ROTATED,
                    context.wall_time,
                    self._failed_sessions,
                )
                continue
            active = commit_runtime_snapshot(self._root, target, batch, processor) or active
        return active
