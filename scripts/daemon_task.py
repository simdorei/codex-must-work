"""Keep one opted-in session inside the shared CMW daemon process."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, final

from scripts.daemon_models import DaemonServiceError, SessionId, ToolResult
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_lease import (
    ManagerLease,
    acquire_manager_lease,
    release_manager_lease,
)
from scripts.manager_runtime import load_manager_runtime
from scripts.manager_runtime_values import bump_revision
from scripts.state import mutate_existing_state

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.app_server_protocol import ManagedAppServer
    from scripts.state_io import JsonValue


class DeadlineKind(StrEnum):
    """Threshold boundary represented by a task's next deadline."""

    WARNING = "warning"
    RESTART = "restart"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True, slots=True)
class TaskDeadline:
    """One exact monotonic threshold deadline."""

    at: float
    kind: DeadlineKind


@dataclass(frozen=True, slots=True)
class DaemonTaskConfig:
    """Immutable construction values for one daemon-owned task."""

    root: Path
    runtime_name: str
    session_id: SessionId
    pid: int
    warning_seconds: float
    restart_seconds: float
    now: float


@final
class DaemonTask:
    """Adapt the existing exact-turn manager to one shared daemon."""

    def __init__(
        self,
        config: DaemonTaskConfig,
        client: ManagedAppServer | None,
        lease: ManagerLease | None = None,
    ) -> None:
        """Bind persisted state and optional managed engine without spawning."""
        self.root = config.root
        self.runtime_name = config.runtime_name
        self.session_id = config.session_id
        self._pid = config.pid
        self._warning = config.warning_seconds
        self._restart = config.restart_seconds
        self._silence_started = config.now
        self._deadline_kind = DeadlineKind.WARNING
        self._engine = (
            None
            if client is None
            else ManagerEngine(
                config.root,
                config.runtime_name,
                client,
                pid=config.pid,
                callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
            )
        )
        self._initialized = False
        self._closed = False
        self._lease = lease

    @property
    def managed(self) -> bool:
        """Return whether this task owns managed app-server transitions."""
        return self._engine is not None

    def initialize(self) -> None:
        """Publish manager readiness without launching another process."""
        if self._initialized or self._engine is None:
            self._initialized = True
            return
        if self._lease is None:
            lease = acquire_manager_lease(self.root, self.runtime_name)
            if lease is None:
                reason = "session_manager_still_running"
                raise DaemonServiceError(reason)
            self._lease = lease
        initialized = False
        try:
            self._engine.initialize()
            self._initialized = True
            initialized = True
        finally:
            if not initialized:
                self._release_lease()

    def drive(self) -> bool:
        """Advance one exact-turn transition and report continued ownership."""
        if self._closed:
            return False
        if self._engine is None:
            return self.runtime_file().is_file()
        return self._engine.tick()

    def record_activity(self, now: float) -> None:
        """Restart threshold timing after an app-server progress signal."""
        if now < self._silence_started:
            return
        self._silence_started = now
        self._deadline_kind = DeadlineKind.WARNING

    def next_deadline(self) -> TaskDeadline:
        """Return the next warning, restart, or periodic heartbeat boundary."""
        kind = self._deadline_kind
        offsets = {
            DeadlineKind.WARNING: self._warning,
            DeadlineKind.RESTART: self._restart,
            DeadlineKind.HEARTBEAT: self._warning,
        }
        deadline = self._silence_started + offsets[kind]
        return TaskDeadline(deadline, kind)

    def advance_deadline(self, expected: TaskDeadline, now: float) -> bool:
        """Advance one still-current threshold stage without polling."""
        if self.next_deadline() != expected:
            return False
        transitions = {
            DeadlineKind.WARNING: (DeadlineKind.RESTART, False),
            DeadlineKind.RESTART: (DeadlineKind.HEARTBEAT, True),
            DeadlineKind.HEARTBEAT: (DeadlineKind.HEARTBEAT, True),
        }
        self._deadline_kind, reset_silence = transitions[expected.kind]
        if reset_silence:
            self._silence_started = now
        return True

    def runtime_file(self) -> Path:
        """Return the hashed state file already bound to this task."""
        return self.root / "runtime" / self.runtime_name

    def status(self) -> ToolResult:
        """Return privacy-safe lifecycle state for MCP status."""
        runtime = load_manager_runtime(self.root, self.runtime_name) if self.managed else None
        if runtime is None:
            enabled = self.runtime_file().is_file()
            return ToolResult(
                self.session_id,
                "active" if enabled else "inactive",
                enabled=enabled,
                managed=False,
            )
        return ToolResult(
            self.session_id,
            "active" if runtime.view.enabled else "inactive",
            enabled=runtime.view.enabled,
            managed=True,
            manager_ready=runtime.manager_ready,
            managed_turn_id=runtime.view.managed_turn_id,
            shutdown_requested=runtime.shutdown_requested,
            manager_error=runtime.manager_error,
        )

    def close(self) -> None:
        """Release only this daemon's readiness marker; preserve recovery state."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._engine is not None:
                self._engine.close()
            self._mark_stopped_if_owned()
        finally:
            self._release_lease()

    def _mark_stopped_if_owned(self) -> None:
        path = self.runtime_file()

        def update(values: dict[str, JsonValue]) -> None:
            if values.get("manager_pid") != self._pid:
                return
            values["manager_ready"] = False
            values["manager_pid"] = None
            bump_revision(values, path)

        _ = mutate_existing_state(self.root, path, update)

    def _release_lease(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            release_manager_lease(lease)
