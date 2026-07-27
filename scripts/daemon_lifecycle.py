"""Detach daemon resources under lock and close them after releasing it."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol, final

from scripts import daemon_keys
from scripts.daemon_errors import SERVICE_ERRORS, ServiceError, error_reason, manager_failure_reason
from scripts.daemon_models import DaemonServiceError
from scripts.manager_failure import record_manager_failure
from scripts.manager_runtime import load_manager_runtime

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from scripts.daemon_scheduler import SchedulerKey
    from scripts.daemon_task import DaemonTask

_MAX_DIAGNOSTICS: Final = 3
_MAX_ERROR_TYPE_LENGTH: Final = 64


class ClosableTask(Protocol):
    """Describe a task reference detached from the registry."""

    def close(self) -> None:
        """Release task-owned resources."""
        ...


class ClosableClient(Protocol):
    """Describe a shared-client reference detached from the registry."""

    def close(self) -> None:
        """Release the shared app-server resource."""
        ...


class LifecycleRegistry(Protocol):
    """Expose only registry operations that are safe under the lifecycle lock."""

    def tasks(self) -> tuple[DaemonTask, ...]:
        """Return an immutable task snapshot."""
        ...

    def detach(self, tasks: tuple[DaemonTask, ...]) -> DetachedResources:
        """Remove selected task references and any newly idle client."""
        ...

    def detach_all(self) -> DetachedResources:
        """Remove all task references and the shared client."""
        ...


class LifecycleScheduler(Protocol):
    """Expose scheduler operations needed during lifecycle transitions."""

    def cancel(self, key: SchedulerKey) -> None:
        """Cancel one keyed callback without waiting for user code."""
        ...

    def close(self) -> None:
        """Join the scheduler worker after it stops accepting callbacks."""
        ...


@dataclass(frozen=True, slots=True)
class DetachedResources:
    """Resources removed from all indexes and therefore safe to close."""

    tasks: tuple[ClosableTask, ...]
    client: ClosableClient | None


@dataclass(frozen=True, slots=True)
class CloseReport:
    """Bounded public-safe diagnostics from one complete close pass."""

    diagnostics: tuple[str, ...]

    @property
    def reason(self) -> str | None:
        """Return one bounded aggregate suitable for a daemon error."""
        return ";".join(self.diagnostics) if self.diagnostics else None


@final
class _Admission:
    def __init__(self, lifecycle: DaemonLifecycle, *, required: bool) -> None:
        self._lifecycle = lifecycle
        self._required = required
        self._admitted = False

    def __enter__(self) -> bool:
        self._admitted = self._lifecycle.begin_admission()
        if not self._admitted and self._required:
            reason = "daemon_closed"
            raise DaemonServiceError(reason)
        return self._admitted

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        _ = error_type, traceback
        if not self._admitted:
            return False
        if self._lifecycle.end_admission():
            report = self._lifecycle.wait_closed()
            if error is None and (self._required or report.reason is not None):
                reason = report.reason or "daemon_closed"
                raise DaemonServiceError(reason)
        return False


def close_detached(resources: DetachedResources) -> CloseReport:
    """Close every detached task, then the shared client, without short-circuiting."""
    diagnostics: list[str] = []
    for task in resources.tasks:
        try:
            task.close()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            _append_diagnostic(diagnostics, "task", error)
    if resources.client is not None:
        try:
            resources.client.close()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            _append_diagnostic(diagnostics, "client", error)
    return CloseReport(tuple(diagnostics))


@final
class DaemonLifecycle:
    """Coordinate task detachment, failure cleanup, and idempotent shutdown."""

    def __init__(
        self,
        root: Path,
        lock: threading.RLock,
        registry: LifecycleRegistry,
        scheduler: LifecycleScheduler,
    ) -> None:
        """Bind lifecycle coordination to one service lock and its resource owners."""
        self._root = root
        self._lock = lock
        self._registry = registry
        self._scheduler = scheduler
        self._condition = threading.Condition(lock)
        self._last_error: str | None = None
        self._active_admissions = 0
        self._close_started = False
        self._close_finished = False
        self._close_owner: int | None = None
        self._close_report = CloseReport(())

    def require_open(self) -> None:
        """Reject new requests after shutdown begins."""
        with self._lock:
            if self._close_started:
                reason = "daemon_closed"
                raise DaemonServiceError(reason)

    def is_closed(self) -> bool:
        """Return whether shutdown has begun."""
        with self._lock:
            return self._close_started

    def begin_admission(self) -> bool:
        """Reserve one operation that close must wait to finish."""
        with self._condition:
            if self._close_started:
                return False
            self._active_admissions += 1
            return True

    def admission(self, *, required: bool) -> _Admission:
        """Keep close behind one lock-free operation and share its final result."""
        return _Admission(self, required=required)

    def end_admission(self) -> bool:
        """Release one reservation and report whether close started during it."""
        with self._condition:
            self._active_admissions -= 1
            closing = self._close_started
            if self._active_admissions == 0:
                self._condition.notify_all()
            return closing

    def wait_closed(self) -> CloseReport:
        """Wait for the close owner and return its shared bounded result."""
        owner = threading.get_ident()
        with self._condition:
            if self._close_owner == owner and not self._close_finished:
                return CloseReport(())
            while not self._close_finished:
                _ = self._condition.wait()
            return self._close_report

    def last_error(self) -> str | None:
        """Return the latest bounded lifecycle diagnostic."""
        with self._lock:
            return self._last_error

    def drive_managers(self) -> None:
        """Advance tasks and detach every task that no longer owns work."""
        with self._lock:
            tasks = self._registry.tasks()
        finished: list[DaemonTask] = []
        for task in tasks:
            try:
                if not task.drive():
                    finished.append(task)
            except SERVICE_ERRORS as error:
                self.fail_tasks((task,), error)
        _ = self.remove_tasks(tuple(finished))

    def fail_tasks(self, tasks: tuple[DaemonTask, ...], error: ServiceError) -> None:
        """Persist a bounded manager failure and close each failed task."""
        self._set_error(error_reason(error))
        for task in tasks:
            runtime = load_manager_runtime(self._root, task.runtime_name)
            if runtime is not None and task.managed:
                record_manager_failure(
                    self._root,
                    runtime.runtime_file,
                    manager_failure_reason(error),
                )
        _ = self.remove_tasks(tasks)

    def remove_tasks(self, tasks: tuple[DaemonTask, ...]) -> CloseReport:
        """Detach selected tasks under lock, then close outside every lock."""
        with self._lock:
            for task in tasks:
                self._scheduler.cancel(daemon_keys.monitor_key(task.session_id))
                self._scheduler.cancel(daemon_keys.activation_key(task.session_id))
        detached = self._registry.detach(tasks)
        report = close_detached(detached)
        if report.reason is not None:
            self._set_error(report.reason)
        return report

    def close(self) -> CloseReport:
        """Stop scheduling, detach all tasks, and close them exactly once."""
        owner = threading.get_ident()
        with self._condition:
            if self._close_started:
                if self._close_owner == owner and not self._close_finished:
                    return CloseReport(())
                while not self._close_finished:
                    _ = self._condition.wait()
                return self._close_report
            self._close_started = True
            self._close_owner = owner
            while self._active_admissions:
                _ = self._condition.wait()
        report = self._close_owned_resources()
        with self._condition:
            self._close_report = report
            self._close_finished = True
            self._close_owner = None
            self._condition.notify_all()
        return report

    def _close_owned_resources(self) -> CloseReport:
        diagnostics: list[str] = []
        try:
            self._scheduler.close()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            _append_diagnostic(diagnostics, "scheduler", error)
        try:
            detached = self._registry.detach_all()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            _append_diagnostic(diagnostics, "registry", error)
            detached = DetachedResources((), None)
        for diagnostic in close_detached(detached).diagnostics:
            _append_reason(diagnostics, diagnostic)
        return CloseReport(tuple(diagnostics))

    def _set_error(self, reason: str) -> None:
        with self._lock:
            self._last_error = reason


def _append_diagnostic(diagnostics: list[str], kind: str, error: BaseException) -> None:
    if len(diagnostics) >= _MAX_DIAGNOSTICS:
        diagnostics[-1] = "additional_close_failures"
        return
    error_type = type(error).__name__[:_MAX_ERROR_TYPE_LENGTH]
    diagnostics.append(f"{kind}_close_failed:{error_type}")


def _append_reason(diagnostics: list[str], reason: str) -> None:
    if len(diagnostics) >= _MAX_DIAGNOSTICS:
        diagnostics[-1] = "additional_close_failures"
        return
    diagnostics.append(reason)
