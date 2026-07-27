"""Own daemon task registration, recovery, leases, and the shared client."""
# noqa: SIZE_OK  -- one registry owns one task/client/lease index and its transactions

from __future__ import annotations

import os
from collections import deque
from datetime import UTC, datetime
from threading import Event
from typing import TYPE_CHECKING, Final, final

from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind, JsonObject
from scripts.daemon_activation import (
    activation_request,
    daemon_capabilities,
    require_matching_activation,
    require_recoverable_activation,
)
from scripts.daemon_activation_fence import DaemonActivationFences, PendingActivation
from scripts.daemon_cleanup import CleanupReport, CleanupStep, run_cleanup
from scripts.daemon_client_pool import ClientBorrow, ClientFactory, SharedClientPool
from scripts.daemon_errors import SERVICE_ERRORS, manager_failure_reason
from scripts.daemon_lifecycle import DetachedResources, close_detached
from scripts.daemon_models import DaemonServiceError, StartRequest
from scripts.daemon_recovery import discover_persisted_tasks, recover_activation_fence
from scripts.daemon_root import PrivateRootInitializer
from scripts.daemon_task import DaemonTask, DaemonTaskConfig
from scripts.goal_control import GoalControlError
from scripts.manager_failure import record_manager_failure
from scripts.manager_lease import (
    ManagerLease,
    RecoveryLeaseIdentity,
    acquire_recovery_manager_lease,
    release_manager_lease,
)
from scripts.manager_runtime import (
    clear_pending_turn,
    load_manager_runtime,
    record_turn_started,
)
from scripts.setup import ActivationRequest, disable_session, enable_session
from scripts.state import StateDocument, load_state, runtime_path, save_state
from scripts.watcher_source import initial_cursor, load_cursor, save_cursor

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from scripts.activation_fence import ActivationFenceStatus
_MAX_TURN_CORRELATIONS: Final = 2048
_CORRELATION_EXPIRY_SECONDS: Final = 600.0
_PENDING_GRACE_SECONDS: Final = 60.0
_MAX_ROUTING_DIAGNOSTICS: Final = 20


@final
class DaemonRegistry:
    """Serialize ownership of persisted sessions inside one daemon process."""

    def __init__(
        self,
        root: Path,
        client_factory: ClientFactory,
        activity_listener: Callable[[AppServerActivity], None],
        clock: Callable[[], float],
        lock: threading.RLock,
    ) -> None:
        """Create an empty registry around one lazy client factory."""
        self._root = root
        self._clock = clock
        self._lock = lock
        self._clients = SharedClientPool(client_factory, activity_listener, lock)
        self._root_initializer = PrivateRootInitializer(root, lock)
        self._tasks: dict[str, DaemonTask] = {}
        self._activations = DaemonActivationFences(root)
        self._turn_owners: dict[str, DaemonTask | None] = {}
        self._interrupted_turns: dict[str, float] = {}
        self._interrupt_attempts: dict[str, Event] = {}
        self._routing_diagnostics: deque[str] = deque(maxlen=_MAX_ROUTING_DIAGNOSTICS)

    def start(self, request: StartRequest, fingerprint: str) -> tuple[DaemonTask, bool]:
        """Create or exactly reuse one task with rollback on initialization failure."""
        self._root_initializer.ensure()
        path = runtime_path(self._root, request.session_id)
        existing = self._tasks.get(request.session_id)
        activation = activation_request(request)
        if existing is not None:
            require_matching_activation(self._root, path, activation)
            return existing, True
        created = not path.exists()
        managed = request.auto_restart and not request.observe_only
        pending = (
            self._activations.capture(request.session_id, request.transcript_path)
            if managed
            else None
        )
        task: DaemonTask | None = None
        borrow: ClientBorrow | None = None
        previous: StateDocument | None = None
        try:
            if created:
                _ = enable_session(
                    self._root,
                    activation,
                    daemon_capabilities(fingerprint, managed=managed),
                )
            else:
                require_recoverable_activation(self._root, path, activation)
            previous = None if created else load_state(self._root, path)
            config = DaemonTaskConfig(
                self._root,
                path.name,
                request.session_id,
                os.getpid(),
                int(request.warning_after_ms) / 1000,
                int(request.restart_after_ms) / 1000,
                self._clock(),
            )
            task, borrow = self._new_task(config, fingerprint, managed)
            task.initialize()
            if managed:
                self._bind_activation(pending, path, activation)
                self._resume_thread(request.session_id)
            else:
                self._initialize_cursor(request.session_id, request.transcript_path)
            self._publish(task, borrow)
        except BaseException as error:  # noqa: BROAD_EXCEPT_OK
            report = self._rollback_attempt(
                task,
                borrow,
                request.session_id,
                created=created,
                previous=previous,
            )
            report.annotate(error)
            raise
        else:
            return task, False

    def recover(self) -> tuple[DaemonTask, ...]:  # noqa: PLR0912, PLR0915
        """Rebuild every enabled task not owned by another live manager lease."""
        for saved in discover_persisted_tasks(self._root):
            claim = acquire_recovery_manager_lease(
                self._root,
                saved.runtime_name,
                RecoveryLeaseIdentity(
                    saved.activation_generation,
                    saved.session_id,
                    saved.transcript_path,
                ),
            )
            if claim is None:
                continue
            task: DaemonTask | None = None
            borrow: ClientBorrow | None = None
            previous: StateDocument | None = None
            lease: ManagerLease | None = claim.lease
            try:
                if not recover_activation_fence(self._root, self._activations, saved):
                    continue
                previous = load_state(self._root, runtime_path(self._root, saved.session_id))
                config = DaemonTaskConfig(
                    self._root,
                    saved.runtime_name,
                    saved.session_id,
                    os.getpid(),
                    saved.warning_seconds,
                    saved.restart_seconds,
                    self._clock(),
                )
                task, borrow = self._new_task(
                    config,
                    saved.executable_sha256,
                    saved.managed,
                    lease,
                )
                lease = None
                task.initialize()
                if saved.managed:
                    self._resume_thread(saved.session_id)
                self._publish(task, borrow)
            except GoalControlError as error:
                reason = manager_failure_reason(error)
                try:
                    if error.reason_code == "goal_complete":
                        disable_session(self._root, saved.session_id)
                    else:
                        self._restore_state(
                            runtime_path(self._root, saved.session_id),
                            previous,
                        )
                        record_manager_failure(
                            self._root,
                            runtime_path(self._root, saved.session_id),
                            reason,
                        )
                finally:
                    report = self._rollback_attempt(
                        task,
                        borrow,
                        saved.session_id,
                        created=False,
                        previous=None,
                    )
                report.annotate(error)
            except SERVICE_ERRORS as error:
                try:
                    self._restore_state(
                        runtime_path(self._root, saved.session_id),
                        previous,
                    )
                    record_manager_failure(
                        self._root,
                        runtime_path(self._root, saved.session_id),
                        manager_failure_reason(error),
                    )
                finally:
                    report = self._rollback_attempt(
                        task,
                        borrow,
                        saved.session_id,
                        created=False,
                        previous=None,
                    )
                report.annotate(error)
            except BaseException as error:  # noqa: BROAD_EXCEPT_OK
                try:
                    self._restore_state(
                        runtime_path(self._root, saved.session_id),
                        previous,
                    )
                finally:
                    report = self._rollback_attempt(
                        task,
                        borrow,
                        saved.session_id,
                        created=False,
                        previous=None,
                    )
                report.annotate(error)
                raise
            finally:
                if lease is not None:
                    release_manager_lease(lease)
        return self.tasks()

    def tasks(self) -> tuple[DaemonTask, ...]:
        """Return a stable snapshot of daemon-owned tasks."""
        return tuple(self._tasks.values())

    def get(self, session_id: str) -> DaemonTask | None:
        """Return one task owned by this daemon process."""
        return self._tasks.get(session_id)

    def selected(self, activity: AppServerActivity) -> tuple[DaemonTask, ...]:
        """Route activity by exact thread first, then by a known exact turn."""
        interrupt: tuple[str | None, str] | None = None
        with self._lock:
            self._refresh_turn_owners()
            if activity.thread_id is not None:
                task = self._tasks.get(activity.thread_id)
                if activity.kind is AppServerActivityKind.TURN_STARTED:
                    task, interrupt = self._claim_started(task, activity)
                tasks = () if task is None else (task,)
            elif activity.turn_id is not None:
                task = self._turn_owners.get(activity.turn_id)
                if activity.kind is AppServerActivityKind.TURN_STARTED:
                    task, interrupt = self._claim_started(task, activity)
                elif task is None:
                    self._diagnose("app_server_turn_unowned")
                tasks = () if task is None else (task,)
            else:
                self._diagnose("app_server_activity_identity_missing")
                tasks = ()
        if interrupt is not None:
            owner = self._interrupt_once(*interrupt)
            if owner is not None:
                tasks = (owner,)
        return tasks

    @property
    def routing_diagnostics(self) -> tuple[str, ...]:
        """Return bounded public-safe routing diagnostics."""
        with self._lock:
            return tuple(self._routing_diagnostics)

    def activation_pending(self, session_id: str | None) -> bool:
        """Return whether the activation turn still fences first handoff."""
        return self._activations.is_pending(session_id)

    def activation_turn_id(self, session_id: str) -> str | None:
        """Return the exact activation turn retained for one session."""
        return self._activations.turn_id(session_id)

    def poll_activation(self, session_id: str) -> ActivationFenceStatus:
        """Read the next bounded activation-rollout batch."""
        return self._activations.poll(session_id)

    def complete_activation(self, session_id: str) -> None:
        """Publish first handoff for the exact completed activation turn."""
        self._activations.complete(session_id)

    def remove(self, tasks: tuple[DaemonTask, ...]) -> None:
        """Close only tasks still owned by this registry and release idle client."""
        report = close_detached(self.detach(tasks))
        if report.reason is not None:
            raise DaemonServiceError(report.reason)

    def detach(self, tasks: tuple[DaemonTask, ...]) -> DetachedResources:
        """Remove owned task/client references without closing either resource."""
        detached: list[DaemonTask] = []
        managed = 0
        with self._lock:
            for task in tasks:
                if self._tasks.get(task.session_id) is task:
                    _ = self._tasks.pop(task.session_id, None)
                    self._activations.discard(task.session_id)
                    detached.append(task)
                    managed += int(task.managed)
        self._clients.release_installed(managed)
        return DetachedResources(tuple(detached), None)

    def close(self) -> None:
        """Release every task lease and the shared app-server resource."""
        report = close_detached(self.detach_all())
        if report.reason is not None:
            raise DaemonServiceError(report.reason)

    def detach_all(self) -> DetachedResources:
        """Remove all task/client references without closing either resource."""
        with self._lock:
            tasks = self.tasks()
            self._tasks.clear()
            self._activations.clear()
            self._turn_owners.clear()
            self._interrupted_turns.clear()
            managed = sum(int(task.managed) for task in tasks)
        self._clients.release_installed(managed)
        return DetachedResources(tasks, None)

    def _new_task(
        self,
        config: DaemonTaskConfig,
        fingerprint: str,
        managed: bool,
        lease: ManagerLease | None = None,
    ) -> tuple[DaemonTask, ClientBorrow | None]:
        borrow = self._clients.borrow(fingerprint) if managed else None
        constructed = False
        try:
            task = DaemonTask(config, None if borrow is None else borrow.client, lease)
            constructed = True
            return task, borrow
        finally:
            if not constructed and borrow is not None:
                borrow.release()

    def _publish(self, task: DaemonTask, borrow: ClientBorrow | None) -> None:
        try:
            with self._lock:
                if borrow is not None:
                    borrow.commit()
                self._tasks[task.session_id] = task
        except BaseException as error:  # noqa: BROAD_EXCEPT_OK
            steps: list[CleanupStep] = [("publication_index", lambda: self._discard_index(task))]
            if borrow is not None:
                steps.append(("publication_reference", borrow.rollback))
            report = run_cleanup(*steps)
            report.annotate(error)
            raise

    def _rollback_attempt(
        self,
        task: DaemonTask | None,
        borrow: ClientBorrow | None,
        session_id: str,
        *,
        created: bool,
        previous: StateDocument | None,
    ) -> CleanupReport:
        path = runtime_path(self._root, session_id)
        steps: list[CleanupStep] = []
        if task is not None:
            steps.append(("registry_index", lambda: self._discard_index(task)))
        steps.append(("activation_fence", lambda: self._activations.discard(session_id)))
        if task is not None:
            steps.append(("task_close", task.close))
        if borrow is not None:
            steps.append(("client_reference", borrow.rollback))
        steps.append(
            (
                "runtime_state",
                (lambda: disable_session(self._root, session_id))
                if created
                else (lambda: self._restore_state(path, previous)),
            )
        )
        return run_cleanup(*steps)

    def _discard_index(self, task: DaemonTask) -> None:
        with self._lock:
            if self._tasks.get(task.session_id) is task:
                _ = self._tasks.pop(task.session_id)

    def _restore_state(self, path: Path, previous: StateDocument | None) -> None:
        if previous is not None:
            save_state(self._root, path, previous)

    def _resume_thread(self, session_id: str) -> None:
        client = self._clients.get_existing()
        if client is None:
            reason = "app_server_unavailable"
            raise DaemonServiceError(reason)
        _ = client.request("thread/resume", {"threadId": session_id})

    def _initialize_cursor(self, session_id: str, transcript: Path) -> None:
        if load_cursor(self._root, session_id) is None:
            save_cursor(self._root, session_id, initial_cursor(transcript))

    def _bind_activation(
        self,
        pending: PendingActivation | None,
        runtime_file: Path,
        request: ActivationRequest,
    ) -> None:
        if pending is None:
            reason = "activation_fence_missing"
            raise DaemonServiceError(reason)
        self._activations.bind(pending, runtime_file, request.now)

    def _refresh_turn_owners(self) -> None:
        owners: dict[str, DaemonTask | None] = {}
        for task in self._tasks.values():
            if not task.managed:
                continue
            runtime = load_manager_runtime(self._root, task.runtime_name)
            if runtime is None:
                continue
            for turn_id in (runtime.pending_turn_id, runtime.view.managed_turn_id):
                if turn_id is None:
                    continue
                if turn_id not in owners:
                    owners[turn_id] = task
                else:
                    owners[turn_id] = None
        if len(owners) > _MAX_TURN_CORRELATIONS:
            reason = "app_server_correlation_capacity"
            raise DaemonServiceError(reason)
        self._turn_owners = owners

    def _claim_started(
        self,
        task: DaemonTask | None,
        activity: AppServerActivity,
    ) -> tuple[DaemonTask | None, tuple[str | None, str] | None]:
        turn_id = activity.turn_id
        interrupt: tuple[str | None, str] | None = None
        if turn_id is None:
            self._diagnose("app_server_started_turn_missing")
            task = None
        elif task is None:
            interrupt = (activity.thread_id, turn_id)
        else:
            runtime = load_manager_runtime(self._root, task.runtime_name)
            if runtime is None:
                task = None
                interrupt = (activity.thread_id, turn_id)
            elif runtime.view.managed_turn_id != turn_id:
                if (
                    self._activations.is_pending(task.session_id)
                    and self._activations.turn_id(task.session_id) == turn_id
                ) or (
                    runtime.view.enabled
                    and not runtime.view.goal_companion
                    and runtime.view.handoff_requested
                    and runtime.pending_turn_id is None
                    and not runtime.shutdown_requested
                    and self._activations.adopt_started(
                        task.session_id,
                        runtime.rollout_file,
                        turn_id,
                        datetime.now(UTC),
                    )
                ):
                    self._turn_owners[turn_id] = task
                elif runtime.pending_turn_id != turn_id:
                    interrupt = (activity.thread_id or task.session_id, turn_id)
                    task = None
                else:
                    timed_out_at = runtime.pending_turn_timed_out_at
                    if (
                        timed_out_at is not None
                        and self._clock() - timed_out_at > _PENDING_GRACE_SECONDS
                    ):
                        task, interrupt = self._expire_pending_turn(
                            task,
                            runtime.runtime_file,
                            turn_id,
                        )
                    else:
                        record_turn_started(self._root, runtime.runtime_file, turn_id)
                        self._turn_owners[turn_id] = task
        return task, interrupt

    def _expire_pending_turn(
        self,
        task: DaemonTask,
        runtime_file: Path,
        turn_id: str,
    ) -> tuple[DaemonTask | None, tuple[str | None, str] | None]:
        """Claim one expired turn, or retain its exact durable owner after a lost race."""
        if clear_pending_turn(self._root, runtime_file, turn_id):
            _ = self._turn_owners.pop(turn_id, None)
            return None, (task.session_id, turn_id)
        self._refresh_turn_owners()
        if turn_id in self._turn_owners:
            return self._turn_owners[turn_id], None
        return None, (task.session_id, turn_id)

    def _interrupt_once(self, thread_id: str | None, turn_id: str) -> DaemonTask | None:
        while True:
            with self._lock:
                self._refresh_turn_owners()
                if turn_id in self._turn_owners:
                    return self._turn_owners[turn_id]
                attempt, claimed = self._claim_interrupt_attempt(turn_id)
            if attempt is None:
                return None
            if claimed:
                break
            _ = attempt.wait()

        succeeded = False
        try:
            client = self._clients.get_existing()
            if client is None:
                reason = "app_server_unavailable"
                raise DaemonServiceError(reason)
            params: JsonObject = {"turnId": turn_id}
            if thread_id is not None:
                params["threadId"] = thread_id
            _ = client.request("turn/interrupt", params, timeout_seconds=10.0)
            succeeded = True
        finally:
            with self._lock:
                if self._interrupt_attempts.get(turn_id) is attempt:
                    if succeeded:
                        self._interrupted_turns[turn_id] = self._clock()
                    _ = self._interrupt_attempts.pop(turn_id, None)
                    attempt.set()

        with self._lock:
            self._refresh_turn_owners()
            return self._turn_owners.get(turn_id)

    def _claim_interrupt_attempt(self, turn_id: str) -> tuple[Event | None, bool]:
        now = self._clock()
        expired = tuple(
            known_turn
            for known_turn, interrupted_at in self._interrupted_turns.items()
            if now - interrupted_at > _CORRELATION_EXPIRY_SECONDS
        )
        for known_turn in expired:
            _ = self._interrupted_turns.pop(known_turn, None)
        if turn_id in self._interrupted_turns:
            return None, False
        existing = self._interrupt_attempts.get(turn_id)
        if existing is not None:
            return existing, False
        if len(self._interrupted_turns) + len(self._interrupt_attempts) >= _MAX_TURN_CORRELATIONS:
            reason = "app_server_correlation_capacity"
            raise DaemonServiceError(reason)
        attempt = Event()
        self._interrupt_attempts[turn_id] = attempt
        return attempt, True

    def _diagnose(self, code: str) -> None:
        self._routing_diagnostics.append(code)
