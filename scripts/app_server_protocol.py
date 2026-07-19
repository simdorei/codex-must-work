"""Track the minimum verified Codex app-server protocol state."""

from __future__ import annotations

import json
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Protocol, cast, final

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

type JsonObject = dict[str, JsonValue]


@unique
class TurnOutcome(StrEnum):
    """Status classifications observed at the `turn/completed` boundary."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    IN_PROGRESS = "inProgress"
    INVALID = "invalid"


class ManagedAppServer(Protocol):
    """Describe the resident app-server operations required by the manager."""

    @property
    def pending_server_request(self) -> str | None:
        """Return the unsupported server request currently blocking control."""
        ...

    def start(self) -> None:
        """Start the resident app-server connection."""
        ...

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = 10.0,
    ) -> JsonObject:
        """Send one app-server request and return its object result."""
        ...

    def active_turn(self, thread_id: str) -> str | None:
        """Return the active turn observed on this resident connection."""
        ...

    def turn_completed(self, turn_id: str) -> bool:
        """Return whether this connection observed exact-turn completion."""
        ...

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        """Return the exact status classification observed for one turn."""
        ...

    def latest_started_turn(self, thread_id: str) -> str | None:
        """Return the latest turn start retained even after completion."""
        ...

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        """Wait until an exact turn start is observed."""
        ...

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        """Wait until an exact turn completion is observed."""
        ...

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        """Wait for a distinct later turn start on the owned connection."""
        ...


class _JsonLoader(Protocol):
    def __call__(self, value: str) -> JsonValue: ...


_LOAD_JSON = cast("_JsonLoader", json.loads)


class AppServerProtocolError(RuntimeError):
    """Report a malformed or failed app-server response."""


@final
class AppServerEventState:
    """Retain responses, exact active turns, and unsupported server requests."""

    def __init__(self) -> None:
        """Create empty protocol state for one resident connection."""
        self.responses: dict[str, JsonObject] = {}
        self.active_turns: dict[str, str] = {}
        self.latest_started_turns: dict[str, str] = {}
        self.started_turns: set[str] = set()
        self.turn_outcomes: dict[str, TurnOutcome] = {}
        self.pending_server_request: str | None = None

    def record(self, message: JsonObject) -> None:
        """Classify one decoded app-server line without dropping control state."""
        message_id = _string(message, "id")
        method = _string(message, "method")
        if message_id is not None and ("result" in message or "error" in message):
            self.responses[message_id] = message
            return
        if message_id is not None and method is not None:
            self.pending_server_request = method
            return
        params = message.get("params")
        if method is None or not isinstance(params, dict):
            return
        thread_id = _thread_id(params)
        turn_id = _turn_id(params)
        if method == "turn/started" and turn_id is not None:
            self.started_turns.add(turn_id)
            if thread_id is not None:
                self.active_turns[thread_id] = turn_id
                self.latest_started_turns[thread_id] = turn_id
        elif method == "turn/completed" and turn_id is not None:
            outcome = _turn_outcome(params)
            if thread_id is not None and self.active_turns.get(thread_id) == turn_id:
                _ = self.active_turns.pop(thread_id, None)
            elif thread_id is None:
                self.active_turns = {
                    active_thread: active_turn
                    for active_thread, active_turn in self.active_turns.items()
                    if active_turn != turn_id
                }
            _ = self.turn_outcomes.setdefault(turn_id, outcome)

    def take_response(self, request_id: str) -> JsonObject | None:
        """Remove and return one response matched by request id."""
        return self.responses.pop(request_id, None)

    def active_turn(self, thread_id: str) -> str | None:
        """Return the active turn observed on this resident connection."""
        return self.active_turns.get(thread_id)

    def latest_started_turn(self, thread_id: str) -> str | None:
        """Return the newest turn start without losing fast completions."""
        return self.latest_started_turns.get(thread_id)

    def bind_started_turn(self, thread_id: str, turn_id: str) -> bool:
        """Bind a thread-less start notification to its initiating request."""
        if turn_id in self.turn_outcomes:
            return True
        if turn_id not in self.started_turns:
            return False
        self.active_turns[thread_id] = turn_id
        self.latest_started_turns[thread_id] = turn_id
        return True

    def was_started(self, turn_id: str) -> bool:
        """Return whether this connection observed the turn start."""
        return turn_id in self.started_turns

    def was_completed(self, turn_id: str) -> bool:
        """Return whether this connection observed the turn completion."""
        return turn_id in self.turn_outcomes

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        """Return one validated status classification without collapsing its meaning."""
        return self.turn_outcomes.get(turn_id)


def _thread_id(params: JsonObject) -> str | None:
    direct = _string(params, "threadId") or _string(params, "conversationId")
    if direct is not None:
        return direct
    thread = params.get("thread")
    if isinstance(thread, dict):
        direct = _string(thread, "id")
        if direct is not None:
            return direct
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


def decode_object(raw_line: str) -> JsonObject | None:
    """Decode one JSON object while ignoring non-protocol stdout lines."""
    try:
        decoded = _LOAD_JSON(raw_line)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _turn_outcome(params: JsonObject) -> TurnOutcome:
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return TurnOutcome.INVALID
    status = _string(turn, "status")
    if status is None:
        return TurnOutcome.INVALID
    try:
        return TurnOutcome(status)
    except ValueError:
        return TurnOutcome.INVALID


def response_result(method: str, response: JsonObject) -> JsonObject:
    """Return an object result or raise the app-server's exact failure."""
    error = response.get("error")
    if error is not None:
        message = f"{method}_failed:{_error_detail(error)}"
        raise AppServerProtocolError(message)
    result = response.get("result")
    if result is None:
        return {}
    if not isinstance(result, dict):
        message = f"{method}_invalid_result"
        raise AppServerProtocolError(message)
    return result


def _error_detail(error: JsonValue) -> str:
    if isinstance(error, dict):
        detail = error.get("message")
        if isinstance(detail, str) and detail:
            return detail[:500]
    return str(error)[:500]
