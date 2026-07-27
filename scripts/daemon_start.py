"""Run daemon start as one admitted, session-keyed publication transaction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, final

from scripts import daemon_keys
from scripts.daemon_activation import validate_daemon_start
from scripts.daemon_errors import SERVICE_ERRORS, error_reason
from scripts.daemon_models import DaemonServiceError
from scripts.daemon_reservation import KeyedReservations
from scripts.daemon_scheduler import SchedulerError

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from scripts.daemon_lifecycle import DaemonLifecycle
    from scripts.daemon_models import StartRequest, ToolResult
    from scripts.daemon_registry import DaemonRegistry
    from scripts.daemon_scheduler import DeadlineScheduler
    from scripts.daemon_task import DaemonTask


type FingerprintProvider = Callable[[], str]


@dataclass(frozen=True, slots=True)
class DaemonStartDependencies:
    """Bind the owners and callbacks needed for an atomic start publication."""

    lifecycle: DaemonLifecycle
    registry: DaemonRegistry
    scheduler: DeadlineScheduler
    fingerprint_provider: FingerprintProvider
    schedule_monitor: Callable[[DaemonTask], None]
    schedule_activation: Callable[[str], None]
    reconcile: Callable[[], None]
    lock: threading.RLock


@final
class DaemonStarter:
    """Create or reuse one task and publish all of its scheduler callbacks."""

    def __init__(self, dependencies: DaemonStartDependencies) -> None:
        """Create session reservations around the supplied daemon owners."""
        self._dependencies = dependencies
        self._sessions = KeyedReservations(dependencies.lock)

    def start(self, request: StartRequest) -> ToolResult:
        """Run one start transaction without serializing unrelated sessions."""
        dependencies = self._dependencies
        try:
            with (
                dependencies.lifecycle.admission(required=True),
                self._sessions.claim(request.session_id),
            ):
                validate_daemon_start(request)
                task, reused = dependencies.registry.start(
                    request,
                    dependencies.fingerprint_provider(),
                )
                self._publish(task)
                return replace(task.status(), reused=reused)
        except DaemonServiceError:
            raise
        except SERVICE_ERRORS as error:
            raise DaemonServiceError(error_reason(error)) from error

    def _publish(self, task: DaemonTask) -> None:
        dependencies = self._dependencies
        try:
            dependencies.schedule_monitor(task)
            dependencies.schedule_activation(task.session_id)
            dependencies.scheduler.wake(
                daemon_keys.RECONCILE_KEY,
                dependencies.reconcile,
            )
        except SchedulerError as error:
            _ = dependencies.lifecycle.remove_tasks((task,))
            reason = "scheduler_unavailable"
            raise DaemonServiceError(reason) from error
