"""Coordinate CMW requests and event-driven reconciliation."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, assert_never, final

from scripts import daemon_keys
from scripts.activation_fence import ActivationFenceStatus
from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind
from scripts.daemon_controls import DaemonControlDependencies, DaemonControls
from scripts.daemon_errors import (
    SERVICE_ERRORS,
    error_reason,
)
from scripts.daemon_factory import codex_fingerprint, resident_client
from scripts.daemon_lifecycle import DaemonLifecycle
from scripts.daemon_models import DaemonServiceError, SessionRequest, StartRequest, ToolResult
from scripts.daemon_registry import DaemonRegistry
from scripts.daemon_scheduler import DeadlineScheduler
from scripts.daemon_start import DaemonStartDependencies, DaemonStarter, FingerprintProvider
from scripts.discord_notifications import notification_sink_from_configuration
from scripts.manager_runtime import load_manager_runtime
from scripts.state import runtime_path, state_root
from scripts.watcher_engine import WatcherEngine

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.daemon_client_pool import ClientFactory
    from scripts.daemon_task import DaemonTask, TaskDeadline


_ACTIVATION_INTERVAL_SECONDS = 0.25


@final
class DaemonService:
    """Own the event-driven control loop for all tasks in one Codex process."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        client_factory: ClientFactory | None = None,
        fingerprint_provider: FingerprintProvider | None = None,
        notification_plugin_data: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start one sleeping scheduler and recover unowned persisted tasks."""
        self._root = state_root() if root is None else root
        self._clock = clock
        self._fingerprint_provider = fingerprint_provider or codex_fingerprint
        self._lock = threading.RLock()
        self._watcher = WatcherEngine(
            self._root,
            notification_sink=notification_sink_from_configuration(notification_plugin_data),
        )
        self._scheduler = DeadlineScheduler(clock=clock)
        self._registry = DaemonRegistry(
            self._root,
            client_factory or resident_client,
            self.app_server_activity,
            clock,
            self._lock,
        )
        self._lifecycle = DaemonLifecycle(self._root, self._lock, self._registry, self._scheduler)
        self._starter = DaemonStarter(
            DaemonStartDependencies(
                self._lifecycle,
                self._registry,
                self._scheduler,
                self._fingerprint_provider,
                self._schedule_monitor,
                self._schedule_activation,
                self._reconcile,
                self._lock,
            )
        )
        self._controls = DaemonControls(
            DaemonControlDependencies(self._root, self._lifecycle, self._scheduler)
        )
        try:
            recovered = self._registry.recover()
        except SERVICE_ERRORS as error:
            report = self._lifecycle.close()
            if report.reason is not None:
                raise DaemonServiceError(report.reason) from error
            raise DaemonServiceError(error_reason(error)) from error
        for task in recovered:
            self._schedule_monitor(task)
            self._schedule_activation(task.session_id)
        if recovered:
            self._scheduler.wake(daemon_keys.RECONCILE_KEY, self._reconcile)

    def start(self, request: StartRequest) -> ToolResult:
        """Enable or exactly reuse one session without spawning subprocess owners."""
        return self._starter.start(request)

    def stop(self, request: SessionRequest) -> ToolResult:
        """Request manual shutdown and exact-owned-turn interruption."""
        return self._controls.stop(request)

    def complete(self, request: SessionRequest) -> ToolResult:
        """Fence completion intent until the exact owned turn ends normally."""
        return self._controls.complete(request)

    def status(self, request: SessionRequest) -> ToolResult:
        """Return one privacy-safe session and daemon lifecycle snapshot."""
        try:
            self._lifecycle.require_open()
            with self._lock:
                task = self._registry.get(request.session_id)
            last_error = self._lifecycle.last_error()
            if task is None:
                path = runtime_path(self._root, request.session_id)
                runtime = load_manager_runtime(self._root, path.name) if path.is_file() else None
                enabled = path.is_file()
                return ToolResult(
                    request.session_id,
                    "active" if enabled else "inactive",
                    enabled=enabled,
                    manager_error=None if runtime is None else runtime.manager_error,
                    daemon_error=last_error,
                )
            return replace(task.status(), daemon_error=last_error)
        except DaemonServiceError:
            raise
        except SERVICE_ERRORS as error:
            raise DaemonServiceError(error_reason(error)) from error

    def app_server_activity(self, activity: AppServerActivity) -> None:
        """Queue content-free activity without blocking the app-server reader."""
        with self._lifecycle.admission(required=False) as admitted:
            if admitted:
                self._scheduler.wake(
                    daemon_keys.activity_key(activity),
                    lambda: self._process_activity(activity),
                )

    def _process_activity(self, activity: AppServerActivity) -> None:
        tasks: tuple[DaemonTask, ...] = ()
        try:
            tasks = self._registry.selected(activity)
            if activity.kind in {
                AppServerActivityKind.TURN_STARTED,
                AppServerActivityKind.TURN_PROGRESS,
                AppServerActivityKind.TURN_COMPLETED,
            }:
                now = self._clock()
                for task in tasks:
                    task.record_activity(now)
                    self._schedule_monitor(task)
            if (
                activity.kind is AppServerActivityKind.TURN_COMPLETED
                and activity.thread_id is not None
            ):
                self._schedule_activation(activity.thread_id, immediate=True)
            self._scheduler.wake(daemon_keys.RECONCILE_KEY, self._reconcile)
        except SERVICE_ERRORS as error:
            self._lifecycle.fail_tasks(tasks, error)

    def close(self) -> None:
        """Release leases and child processes while retaining persisted task state."""
        report = self._lifecycle.close()
        if report.reason is not None:
            raise DaemonServiceError(report.reason)

    def _reconcile(self) -> None:
        try:
            _ = self._watcher.tick(self._clock(), datetime.now(UTC))
        except SERVICE_ERRORS as error:
            with self._lock:
                tasks = self._registry.tasks()
            self._lifecycle.fail_tasks(tasks, error)
            return
        self._lifecycle.drive_managers()

    def _poll_activation(self, session_id: str) -> None:
        with self._lock:
            task = self._registry.get(session_id)
            pending = self._registry.activation_pending(session_id)
        if task is None or not pending:
            return
        try:
            status = self._registry.poll_activation(session_id)
            match status:
                case ActivationFenceStatus.COMPLETED | ActivationFenceStatus.ABORTED:
                    self._registry.complete_activation(session_id)
                case ActivationFenceStatus.PENDING:
                    self._schedule_activation(session_id)
                    return
                case ActivationFenceStatus.SUPERSEDED:
                    reason = f"activation_turn_{status.value}"
                    raise DaemonServiceError(reason)
                case _:
                    assert_never(status)
        except SERVICE_ERRORS as error:
            self._lifecycle.fail_tasks((task,), error)
            return
        self._scheduler.wake(daemon_keys.MANAGER_KEY, self._lifecycle.drive_managers)

    def _schedule_activation(self, session_id: str, *, immediate: bool = False) -> None:
        with self._lock:
            pending = self._registry.activation_pending(session_id)
        if not pending:
            return
        deadline = self._clock()
        if not immediate:
            deadline += _ACTIVATION_INTERVAL_SECONDS
        self._scheduler.schedule(
            daemon_keys.activation_key(session_id),
            deadline,
            lambda: self._poll_activation(session_id),
        )

    def _monitor_due(self, task: DaemonTask, expected: TaskDeadline) -> None:
        with self._lock:
            current = self._registry.get(task.session_id)
        if current is not task or task.next_deadline() != expected:
            return
        self._reconcile()
        with self._lock:
            current = self._registry.get(task.session_id)
        if current is task and task.advance_deadline(expected, self._clock()):
            self._schedule_monitor(task)

    def _schedule_monitor(self, task: DaemonTask) -> None:
        deadline = task.next_deadline()
        self._scheduler.schedule(
            daemon_keys.monitor_key(task.session_id),
            deadline.at,
            lambda: self._monitor_due(task, deadline),
        )
