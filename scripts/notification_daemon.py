"""Run passive CMW monitoring inside the resident MCP process."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, final

from scripts.daemon_scheduler import (
    DeadlineScheduler,
    SchedulerHealth,
    SchedulerKey,
)
from scripts.discord_notifications import notification_sink_from_configuration
from scripts.monitor_models import (
    DaemonServiceError,
    SessionRequest,
    StartRequest,
    ToolResult,
)
from scripts.monitor_state import discover_runtime_files
from scripts.notification_session import (
    mark_notification_session_complete,
    notification_session_active,
    remove_notification_session,
    start_notification_session,
)
from scripts.private_root import ensure_private_root
from scripts.state import StateError, state_root
from scripts.watcher_engine import WatcherEngine

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.notifications import NotificationSink

_TICK_KEY: Final = SchedulerKey("notification-watcher")
_POLL_SECONDS: Final = 1.0
_STATE_UNAVAILABLE: Final = "monitoring_state_unavailable"
_DAEMON_CLOSED: Final = "daemon_closed"


@final
class NotificationDaemonService:
    """Own passive rollout diagnosis without launching or controlling Codex."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        notification_plugin_data: Path | None = None,
        notification_sink: NotificationSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start one sleeping scheduler for persisted notification sessions."""
        self._root = state_root() if root is None else root
        ensure_private_root(self._root)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._watcher = WatcherEngine(
            self._root,
            notification_sink=(
                notification_sink
                if notification_sink is not None
                else notification_sink_from_configuration(notification_plugin_data)
            ),
        )
        self._scheduler = DeadlineScheduler(clock=clock)
        if discover_runtime_files(self._root):
            self._schedule_tick(immediate=True)

    def start(self, request: StartRequest) -> ToolResult:
        """Create or exactly reuse one passive notification session."""
        try:
            with self._lock:
                self._require_open()
                reused = start_notification_session(self._root, request, ensure_root=False)
                self._schedule_tick(immediate=True)
            return ToolResult(
                request.session_id,
                "active",
                enabled=True,
                reused=reused,
            )
        except DaemonServiceError:
            raise
        except (OSError, StateError, ValueError) as error:
            raise DaemonServiceError(_STATE_UNAVAILABLE) from error

    def stop(self, request: SessionRequest) -> ToolResult:
        """Stop one passive monitor without interrupting the Codex task."""
        try:
            with self._lock:
                self._require_open()
                remove_notification_session(self._root, request.session_id)
            return ToolResult(request.session_id, "stopped", enabled=False)
        except DaemonServiceError:
            raise
        except (OSError, StateError) as error:
            raise DaemonServiceError(_STATE_UNAVAILABLE) from error

    def complete(self, request: SessionRequest) -> ToolResult:
        """Emit verified completion once, then remove the passive monitor."""
        try:
            with self._lock:
                self._require_open()
                if mark_notification_session_complete(self._root, request.session_id):
                    _ = self._watcher.tick(self._clock(), datetime.now(UTC))
                remove_notification_session(self._root, request.session_id)
            return ToolResult(request.session_id, "completed", enabled=False)
        except DaemonServiceError:
            raise
        except (OSError, StateError, ValueError) as error:
            raise DaemonServiceError(_STATE_UNAVAILABLE) from error

    def status(self, request: SessionRequest) -> ToolResult:
        """Return whether one passive notification session is active."""
        try:
            with self._lock:
                self._require_open()
                enabled = notification_session_active(self._root, request.session_id)
                scheduler = self._scheduler.status()
            if enabled and scheduler.health is not SchedulerHealth.HEALTHY:
                return ToolResult(
                    request.session_id,
                    ("degraded" if scheduler.health is SchedulerHealth.DEGRADED else "unavailable"),
                    enabled=True,
                    daemon_error=scheduler.last_callback_error,
                )
            return ToolResult(
                request.session_id,
                "active" if enabled else "inactive",
                enabled=enabled,
            )
        except DaemonServiceError:
            raise
        except (OSError, StateError) as error:
            raise DaemonServiceError(_STATE_UNAVAILABLE) from error

    def observe(self, now: float, wall_time: datetime) -> bool:
        """Process one explicit rollout snapshot without controlling the task."""
        with self._lock:
            self._require_open()
            return self._watcher.tick(now, wall_time)

    def close(self) -> None:
        """Stop only the resident scheduler; persisted monitors remain recoverable."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._scheduler.close()

    def _reconcile(self) -> None:
        with self._lock:
            if self._closed:
                return
            active = True
            try:
                active = self._watcher.tick(self._clock(), datetime.now(UTC))
            finally:
                if active:
                    self._schedule_tick(immediate=False)

    def _schedule_tick(self, *, immediate: bool) -> None:
        deadline = self._clock() + (0.0 if immediate else _POLL_SECONDS)
        self._scheduler.schedule(_TICK_KEY, deadline, self._reconcile)

    def _require_open(self) -> None:
        if self._closed:
            raise DaemonServiceError(_DAEMON_CLOSED)
