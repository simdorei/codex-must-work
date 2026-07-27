"""Publish bounded, privacy-safe observations from one app-server connection."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NewType, final

from scripts.app_server_protocol import (
    AppServerActivity,
    AppServerActivityKind,
    AppServerEventState,
    JsonObject,
    TurnOutcome,
    decode_object,
)

__all__ = (
    "INITIAL_ACTIVITY_SEQUENCE",
    "ActivityListener",
    "ActivityObservation",
    "ActivitySequence",
    "AppServerActivity",
    "AppServerActivityKind",
    "AppServerActivityStream",
    "ConnectionGeneration",
)

if TYPE_CHECKING:
    from typing import TextIO

ActivitySequence = NewType("ActivitySequence", int)
ConnectionGeneration = NewType("ConnectionGeneration", int)
INITIAL_ACTIVITY_SEQUENCE: Final = ActivitySequence(0)
_MAX_OBSERVATIONS: Final = 512
type ActivityListener = Callable[[AppServerActivity], None]


@dataclass(frozen=True, slots=True)
class ActivityObservation:
    """Bind one activity signal to a connection generation and sequence."""

    generation: ConnectionGeneration
    sequence: ActivitySequence
    activity: AppServerActivity


@final
class AppServerActivityStream:
    """Accumulate exact protocol state and bounded wake signals for one client."""

    def __init__(self, listener: ActivityListener | None = None) -> None:
        """Create an inactive first-generation stream."""
        self.condition = threading.Condition(threading.RLock())
        self._events = AppServerEventState()
        self._stderr: deque[str] = deque(maxlen=20)
        self._observations: deque[ActivityObservation] = deque(maxlen=_MAX_OBSERVATIONS)
        self._sequence = INITIAL_ACTIVITY_SEQUENCE
        self._generation = ConnectionGeneration(0)
        self._closed_error: str | None = None
        self._closed_activity_emitted = False
        self._listener = listener

    def reset(self) -> ConnectionGeneration:
        """Start a new generation while preserving the monotonic wake sequence."""
        with self.condition:
            self._generation = ConnectionGeneration(self._generation + 1)
            self._events = AppServerEventState()
            self._stderr.clear()
            self._closed_error = None
            self._closed_activity_emitted = False
            return self._generation

    def record(self, message: JsonObject) -> None:
        """Update exact state and publish metadata for one decoded message."""
        with self.condition:
            activity = self._record_current(message, self._generation)
        self._deliver(activity)

    def read_stdout(self, stdout: TextIO | None, generation: ConnectionGeneration) -> None:
        """Consume one generation's protocol output until it closes."""
        if stdout is not None:
            for raw_line in stdout:
                decoded = decode_object(raw_line)
                if decoded is not None:
                    with self.condition:
                        activity = self._record_current(decoded, generation)
                    self._deliver(activity)
        self.mark_closed("resident_app_server_exited", generation)

    def read_stderr(self, stderr: TextIO | None, generation: ConnectionGeneration) -> None:
        """Retain bounded diagnostics only for the current connection generation."""
        if stderr is None:
            return
        for raw_line in stderr:
            with self.condition:
                if generation == self._generation:
                    self._stderr.append(raw_line.rstrip()[:500])

    def mark_closed(
        self,
        reason: str,
        generation: ConnectionGeneration | None = None,
    ) -> None:
        """Publish one connection-loss signal without treating it as progress."""
        with self.condition:
            target = self._generation if generation is None else generation
            if target != self._generation or self._closed_activity_emitted:
                return
            self._closed_error = reason
            self._closed_activity_emitted = True
            activity = AppServerActivity(AppServerActivityKind.CONNECTION_CLOSED)
            self._publish(activity)
        self._deliver(activity)

    def wait_activity(
        self,
        after_sequence: ActivitySequence,
        timeout_seconds: float,
    ) -> ActivityObservation | None:
        """Block without polling until a later activity signal or timeout."""
        deadline = time.monotonic() + timeout_seconds
        with self.condition:
            while True:
                observation = self._after(after_sequence)
                if observation is not None:
                    return observation
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                _ = self.condition.wait(remaining)

    @property
    def activity_sequence(self) -> ActivitySequence:
        """Return the newest published sequence."""
        with self.condition:
            return self._sequence

    @property
    def closed_error(self) -> str | None:
        """Return the current generation's connection-close reason."""
        with self.condition:
            return self._closed_error

    @property
    def pending_server_request(self) -> str | None:
        """Return an unsupported server request awaiting handling."""
        with self.condition:
            return self._events.pending_server_request

    def stderr_tail(self) -> tuple[str, ...]:
        """Return bounded diagnostics for the current generation."""
        with self.condition:
            return tuple(self._stderr)

    def take_response(self, request_id: str) -> JsonObject | None:
        """Remove and return one response by request identifier."""
        return self._events.take_response(request_id)

    def active_turn(self, thread_id: str) -> str | None:
        """Return the active turn observed for one thread."""
        return self._events.active_turn(thread_id)

    def latest_started_turn(self, thread_id: str) -> str | None:
        """Return the newest started turn retained for one thread."""
        return self._events.latest_started_turn(thread_id)

    def bind_started_turn(self, thread_id: str, turn_id: str) -> bool:
        """Bind a thread-less observed start to its request owner."""
        return self._events.bind_started_turn(thread_id, turn_id)

    def correlate_turn(self, thread_id: str, turn_id: str) -> None:
        """Retain one accepted turn response for thread-less notification routing."""
        with self.condition:
            self._events.correlate_turn(thread_id, turn_id)

    def was_started(self, turn_id: str) -> bool:
        """Return whether an exact turn start was observed."""
        return self._events.was_started(turn_id)

    def was_completed(self, turn_id: str) -> bool:
        """Return whether an exact turn completion was observed."""
        return self._events.was_completed(turn_id)

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        """Return an exact turn's retained completion outcome."""
        return self._events.turn_outcome(turn_id)

    def _record_current(
        self,
        message: JsonObject,
        generation: ConnectionGeneration,
    ) -> AppServerActivity | None:
        if generation != self._generation:
            return None
        self._events.record(message)
        activity = _classify(message, self._events)
        if activity is not None:
            self._publish(activity)
        else:
            self.condition.notify_all()
        return activity

    def _publish(self, activity: AppServerActivity) -> None:
        self._sequence = ActivitySequence(self._sequence + 1)
        self._observations.append(ActivityObservation(self._generation, self._sequence, activity))
        self.condition.notify_all()

    def _after(self, sequence: ActivitySequence) -> ActivityObservation | None:
        return next(
            (item for item in self._observations if item.sequence > sequence),
            None,
        )

    def _deliver(self, activity: AppServerActivity | None) -> None:
        if activity is not None and self._listener is not None:
            self._listener(activity)


def _classify(
    message: JsonObject,
    events: AppServerEventState,
) -> AppServerActivity | None:
    message_id = _string(message, "id")
    method = _string(message, "method")
    params = message.get("params")
    if message_id is not None and method is not None and not _is_response(message):
        ownership = params if isinstance(params, dict) else {}
        return AppServerActivity(
            AppServerActivityKind.SERVER_REQUEST,
            _thread_id(ownership),
            _turn_id(ownership),
        )
    if method is None or not isinstance(params, dict):
        return None
    turn_id = _turn_id(params)
    thread_id = _thread_id(params)
    if thread_id is None and turn_id is not None:
        thread_id = events.thread_for_turn(turn_id)
    if method == "turn/started" and turn_id is not None:
        return AppServerActivity(AppServerActivityKind.TURN_STARTED, thread_id, turn_id)
    if method == "turn/completed" and turn_id is not None:
        return AppServerActivity(
            AppServerActivityKind.TURN_COMPLETED,
            thread_id,
            turn_id,
            events.turn_outcome(turn_id),
        )
    if turn_id is not None:
        return AppServerActivity(AppServerActivityKind.TURN_PROGRESS, thread_id, turn_id)
    return None


def _is_response(message: JsonObject) -> bool:
    return "result" in message or "error" in message


def _thread_id(params: JsonObject) -> str | None:
    direct = _string(params, "threadId") or _string(params, "conversationId")
    if direct is not None:
        return direct
    thread = params.get("thread")
    if isinstance(thread, dict):
        return _string(thread, "id")
    turn = params.get("turn")
    if isinstance(turn, dict):
        return _string(turn, "threadId") or _string(turn, "conversationId")
    return None


def _turn_id(params: JsonObject) -> str | None:
    direct = _string(params, "turnId")
    if direct is not None:
        return direct
    turn = params.get("turn")
    return _string(turn, "id") if isinstance(turn, dict) else None


def _string(values: JsonObject, key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) and value else None
