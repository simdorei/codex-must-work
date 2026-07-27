"""Run admitted stop and completion mutations against one daemon lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from scripts import daemon_keys, setup
from scripts.daemon_errors import SERVICE_ERRORS, error_reason
from scripts.daemon_models import DaemonServiceError, SessionRequest, ToolResult
from scripts.daemon_scheduler import SchedulerError

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.daemon_lifecycle import DaemonLifecycle
    from scripts.daemon_scheduler import DeadlineScheduler


@dataclass(frozen=True, slots=True)
class DaemonControlDependencies:
    """Bind mutating controls to their state, lifecycle, and scheduler owners."""

    root: Path
    lifecycle: DaemonLifecycle
    scheduler: DeadlineScheduler


@final
class DaemonControls:
    """Coordinate stop and completion with shutdown admission."""

    def __init__(self, dependencies: DaemonControlDependencies) -> None:
        """Retain the owners used by every mutating control."""
        self._dependencies = dependencies

    def stop(self, request: SessionRequest) -> ToolResult:
        """Request manual shutdown before daemon close may proceed."""
        dependencies = self._dependencies
        try:
            with dependencies.lifecycle.admission(required=True):
                setup.request_session_shutdown(
                    dependencies.root,
                    request.session_id,
                    interrupt_active=True,
                )
                dependencies.scheduler.wake(
                    daemon_keys.MANAGER_KEY,
                    dependencies.lifecycle.drive_managers,
                )
        except DaemonServiceError:
            raise
        except SchedulerError as error:
            reason = "scheduler_unavailable"
            raise DaemonServiceError(reason) from error
        except SERVICE_ERRORS as error:
            raise DaemonServiceError(error_reason(error)) from error
        return ToolResult(request.session_id, "stopping")

    def complete(self, request: SessionRequest) -> ToolResult:
        """Publish verified completion before daemon close may proceed."""
        dependencies = self._dependencies
        try:
            with dependencies.lifecycle.admission(required=True):
                deferred = setup.request_verified_completion(
                    dependencies.root,
                    request.session_id,
                    datetime.now(UTC),
                )
                dependencies.scheduler.wake(
                    daemon_keys.MANAGER_KEY,
                    dependencies.lifecycle.drive_managers,
                )
        except DaemonServiceError:
            raise
        except SchedulerError as error:
            reason = "scheduler_unavailable"
            raise DaemonServiceError(reason) from error
        except SERVICE_ERRORS as error:
            raise DaemonServiceError(error_reason(error)) from error
        return ToolResult(
            request.session_id,
            "completion_requested" if deferred else "completed",
        )
