"""Drive same-thread handoff, exact interrupt, and replacement turns."""
# noqa: SIZE_OK  -- one state machine owns one manager tick's exact turn transitions

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, assert_never, final

from scripts.goal_control import GoalControlError
from scripts.goal_policy import enforce_goal_companion_policy
from scripts.goal_turn_source import require_native_goal_turn
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_decision import ManagerAction, decide_manager_action
from scripts.manager_failure import record_manager_failure
from scripts.manager_goal import (
    GoalGuard,
    fence_goal_handoff,
    persisted_goal_guard,
    require_goal_guard,
)
from scripts.manager_interrupt import InterruptController
from scripts.manager_messages import continuation_prompt, result_turn_id
from scripts.manager_outcome import resolve_turn_outcome
from scripts.manager_runtime import (
    ManagerRuntime,
    load_manager_runtime,
    mark_manager_ready,
    mark_pending_turn_timed_out,
    record_pending_turn,
    record_turn_started,
)
from scripts.manager_shutdown import handle_shutdown
from scripts.setup import disable_session
from scripts.watcher_source import RolloutCursor, initial_cursor

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.app_server_protocol import ManagedAppServer

_GOAL_IDENTITY_CHANGED = "goal_identity_changed"
_PENDING_RECONCILIATION_GRACE_SECONDS = 60.0


@final
class ManagerEngine:
    """Execute at most one guarded control transition per tick."""

    def __init__(
        self,
        root: Path,
        runtime_name: str,
        client: ManagedAppServer,
        *,
        pid: int,
        callbacks: ManagerCallbacks | None = None,
    ) -> None:
        """Bind one hashed runtime to one resident owner connection."""
        self._root = root
        self._runtime_name = runtime_name
        self._client = client
        self._pid = pid
        self._callbacks = callbacks if callbacks is not None else ManagerCallbacks()
        self._thread_loaded = False
        self._restart_prompt_pending = False
        self._goal_guard: GoalGuard | None = None
        self._interrupts = InterruptController(root, client)

    def initialize(self) -> None:
        """Initialize app-server before publishing manager readiness."""
        runtime = load_manager_runtime(self._root, self._runtime_name)
        if runtime is None:
            return
        enforce_goal_companion_policy(requested=runtime.view.goal_companion)
        initialized = False
        try:
            self._client.start()
            if runtime.view.goal_companion:
                self._load_thread(runtime)
                self._goal_guard = persisted_goal_guard(self._client, self._root, runtime)
                self._goal_guard.initialize()
            mark_manager_ready(self._root, runtime.runtime_file, self._pid)
            initialized = True
        finally:
            if not initialized:
                self.close()

    def close(self) -> None:
        """Restore the captured Goal's original active scheduling on exit."""
        if self._goal_guard is None:
            return
        runtime = load_manager_runtime(self._root, self._runtime_name)
        if (
            runtime is not None
            and runtime.view.goal_companion
            and runtime.view.managed_turn_id is not None
            and not runtime.shutdown_requested
        ):
            self._goal_guard.keep_paused_on_exit()
        try:
            _ = self._goal_guard.restore_initial_active()
        except GoalControlError as error:
            if error.reason_code != _GOAL_IDENTITY_CHANGED:
                raise

    def tick(self) -> bool:
        """Advance one ownership, interruption, or restart transition."""
        runtime = load_manager_runtime(self._root, self._runtime_name)
        if runtime is None:
            return False
        return self._tick_runtime(runtime)

    def _tick_runtime(self, runtime: ManagerRuntime) -> bool:
        try:
            if self._client.pending_server_request is not None:
                keep_running = self._fail(runtime, "server_request_unhandled")
            elif runtime.pending_turn_id is not None:
                keep_running = self._reconcile_pending_turn(runtime)
            elif (owned_turn := runtime.view.managed_turn_id) is not None and (
                outcome := self._client.turn_outcome(owned_turn)
            ) is not None:
                resolution = resolve_turn_outcome(
                    self._root, runtime, self._goal_guard, owned_turn, outcome
                )
                if resolution.failure_reason is not None:
                    keep_running = self._fail(runtime, resolution.failure_reason)
                else:
                    self._restart_prompt_pending = resolution.restart_prompt_pending
                    keep_running = resolution.keep_running
            elif runtime.shutdown_requested:
                owned_turn = runtime.view.managed_turn_id
                if owned_turn is None:
                    self.close()
                keep_running = handle_shutdown(
                    self._root,
                    runtime,
                    self._client,
                    self._goal_guard,
                    self._fail,
                )
            else:
                keep_running = self._execute_decision(runtime)
        except GoalControlError as error:
            if error.reason_code == "goal_complete":
                disable_session(self._root, runtime.session_id)
                keep_running = False
            else:
                keep_running = self._fail(runtime, error.reason_code)
        return keep_running

    def _execute_decision(self, runtime: ManagerRuntime) -> bool:
        decision = decide_manager_action(runtime.view, self._client.active_turn(runtime.session_id))
        match decision.action:
            case ManagerAction.START:
                keep_running = self._start_turn(runtime)
            case ManagerAction.RESUME_GOAL:
                keep_running = self._resume_goal(runtime)
            case ManagerAction.INTERRUPT:
                if decision.turn_id is None:
                    keep_running = self._fail(runtime, "app_server_failed")
                else:
                    keep_running = self._interrupt_turn(runtime, decision.turn_id)
            case ManagerAction.FAIL_CLOSED:
                keep_running = self._fail(
                    runtime,
                    decision.reason_code or "app_server_failed",
                )
            case ManagerAction.WAIT:
                keep_running = True
            case ManagerAction.STOP:
                keep_running = False
            case _:
                assert_never(decision.action)
        return keep_running

    def _start_turn(self, runtime: ManagerRuntime) -> bool:
        self._load_thread(runtime)
        result = self._client.request(
            "turn/start",
            {
                "threadId": runtime.session_id,
                "input": [
                    {
                        "type": "text",
                        "text": continuation_prompt(
                            runtime.message_preset,
                            restarted=self._restart_prompt_pending,
                        ),
                        "text_elements": [],
                    }
                ],
            },
            timeout_seconds=12.0,
        )
        turn_id = result_turn_id(result)
        if turn_id is None:
            return self._fail(runtime, "start_timeout")
        self._persist_accepted_turn(runtime, turn_id)
        if not self._client.wait_turn_started(runtime.session_id, turn_id):
            mark_pending_turn_timed_out(
                self._root,
                runtime.runtime_file,
                turn_id,
                time.monotonic(),
            )
            return True
        self._record_turn_started(runtime, turn_id)
        self._restart_prompt_pending = False
        return True

    def _reconcile_pending_turn(self, runtime: ManagerRuntime) -> bool:
        turn_id = runtime.pending_turn_id
        if turn_id is None:
            return True
        timed_out_at = runtime.pending_turn_timed_out_at
        if (
            timed_out_at is not None
            and time.monotonic() - timed_out_at > _PENDING_RECONCILIATION_GRACE_SECONDS
        ):
            self._interrupts.expire_pending(runtime, turn_id)
            return self._fail(runtime, "start_timeout")
        if (
            self._client.active_turn(runtime.session_id) == turn_id
            or self._client.turn_outcome(turn_id) is not None
        ):
            self._record_turn_started(runtime, turn_id)
            self._restart_prompt_pending = False
            return True
        return True

    def _persist_accepted_turn(self, runtime: ManagerRuntime, turn_id: str) -> None:
        """Persist ownership or interrupt an accepted turn without masking cancellation."""
        try:
            record_pending_turn(self._root, runtime.runtime_file, turn_id)
        finally:
            original = sys.exception()
            if original is not None:
                traceback = original.__traceback__
                try:
                    self._recover_accepted_turn(runtime, turn_id)
                finally:
                    raise original.with_traceback(traceback)

    def _recover_accepted_turn(self, runtime: ManagerRuntime, turn_id: str) -> None:
        """Keep a durable exact owner; otherwise interrupt the accepted turn once."""
        durable_owner = False
        try:
            persisted = load_manager_runtime(self._root, self._runtime_name)
            durable_owner = persisted is not None and (
                turn_id
                in {
                    persisted.pending_turn_id,
                    persisted.view.managed_turn_id,
                }
            )
        finally:
            if not durable_owner:
                self._interrupts.interrupt_unowned_accepted(runtime, turn_id)

    def _resume_goal(self, runtime: ManagerRuntime) -> bool:
        guard = require_goal_guard(self._goal_guard)
        if self._client.active_turn(runtime.session_id) is not None:
            return self._fail(runtime, "unexpected_active_turn")
        previous_turn = self._client.latest_started_turn(runtime.session_id)
        source_cursor = initial_cursor(runtime.rollout_file)
        guard.activate_for_handoff()
        turn_id = self._client.wait_next_turn_started(runtime.session_id, previous_turn)
        if turn_id is not None:
            return self._adopt_turn(runtime, turn_id, source_cursor)
        guard.pause_for_interrupt()
        return self._fail(runtime, "goal_resume_timeout")

    def _adopt_turn(
        self,
        runtime: ManagerRuntime,
        turn_id: str,
        source_cursor: RolloutCursor,
    ) -> bool:
        require_native_goal_turn(
            self._callbacks.goal_turn_verifier, runtime.rollout_file, source_cursor, turn_id
        )
        observed_turn = turn_id
        turn_id = fence_goal_handoff(
            self._client,
            require_goal_guard(self._goal_guard),
            runtime.session_id,
            turn_id,
        )
        if turn_id != observed_turn:
            require_native_goal_turn(
                self._callbacks.goal_turn_verifier, runtime.rollout_file, source_cursor, turn_id
            )
        self._record_turn_started(runtime, turn_id)
        if self._client.turn_outcome(turn_id) is None and self._restart_prompt_pending:
            _ = self._client.request(
                "turn/steer",
                {
                    "threadId": runtime.session_id,
                    "expectedTurnId": turn_id,
                    "input": [
                        {
                            "type": "text",
                            "text": continuation_prompt(runtime.message_preset, restarted=True),
                            "text_elements": [],
                        }
                    ],
                },
                timeout_seconds=10.0,
            )
        self._restart_prompt_pending = False
        return True

    def _record_turn_started(self, runtime: ManagerRuntime, turn_id: str) -> None:
        record_turn_started(self._root, runtime.runtime_file, turn_id)
        self._callbacks.watcher_launcher()

    def _interrupt_turn(self, runtime: ManagerRuntime, turn_id: str) -> bool:
        guard = require_goal_guard(self._goal_guard) if runtime.view.goal_companion else None
        result = self._interrupts.execute(runtime, turn_id, guard)
        if result.failure_reason is not None:
            return self._fail(runtime, result.failure_reason)
        if result.restarted:
            self._restart_prompt_pending = True
        return True

    def _load_thread(self, runtime: ManagerRuntime) -> None:
        if self._thread_loaded:
            return
        _ = self._client.request(
            "thread/resume",
            {"threadId": runtime.session_id},
            timeout_seconds=10.0,
        )
        self._thread_loaded = True

    def _fail(self, runtime: ManagerRuntime, reason_code: str) -> bool:
        self.close()
        record_manager_failure(self._root, runtime.runtime_file, reason_code)
        return False
