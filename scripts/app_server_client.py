"""Own one persistent Codex app-server connection for managed turns."""
# noqa: SIZE_OK  -- one transport owns one process, request stream, and activity state

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Self, final
from uuid import uuid4

from scripts.app_server_activity import (
    ActivityListener,
    AppServerActivityStream,
)
from scripts.app_server_protocol import (
    AppServerProtocolError,
    JsonObject,
    TurnOutcome,
    response_result,
)
from scripts.codex_executable import resolve_codex_executable
from scripts.manager_messages import result_turn_id

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType


class AppServerError(AppServerProtocolError):
    """Report a resident transport or app-server response failure."""


@final
class ResidentAppServer:
    """Start turns and interrupt only turns observed on this connection."""

    def __init__(
        self,
        expected_executable_sha256: str | None = None,
        activity_listener: ActivityListener | None = None,
    ) -> None:
        """Create an inactive client with bounded in-memory diagnostics."""
        self._process: subprocess.Popen[str] | None = None
        self._activity = AppServerActivityStream(activity_listener)
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._expected_executable_sha256 = expected_executable_sha256

    def __enter__(self) -> Self:
        """Start the resident process for a context-managed client."""
        self.start()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the resident process when leaving its context."""
        self.close()

    def start(self) -> None:
        """Launch and initialize one app-server process exactly once."""
        with self._lifecycle_lock:
            if self._running():
                return
            executable = resolve_codex_executable(self._expected_executable_sha256)
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process = subprocess.Popen(  # noqa: S603
                    [str(executable), "app-server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )
            except OSError as error:
                message = "resident_app_server_start_failed"
                raise AppServerError(message) from error
            generation = self._activity.reset()
            with self._activity.condition:
                self._process = process
            threading.Thread(
                target=self._activity.read_stdout,
                args=(process.stdout, generation),
                daemon=True,
            ).start()
            threading.Thread(
                target=self._activity.read_stderr,
                args=(process.stderr, generation),
                daemon=True,
            ).start()
            try:
                _ = self._request_started(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex-must-work",
                            "title": "Codex Must Work",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout_seconds=8.0,
                )
                self.notify("initialized", {})
            except AppServerError:
                self.close()
                raise

    def close(self) -> None:
        """Terminate the owned app-server child without touching Codex Desktop."""
        with self._lifecycle_lock:
            process = self._process
            self._process = None
            if process is None:
                return
            self._activity.mark_closed("resident_app_server_closed")
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    _ = process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.wait(timeout=2.0)

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = 10.0,
    ) -> JsonObject:
        """Send one serialized request and return its object result."""
        self.start()
        return self._request_started(method, params, timeout_seconds=timeout_seconds)

    def notify(self, method: str, params: JsonObject) -> None:
        """Send one notification on the initialized connection."""
        self._write({"method": method, "params": params})

    def active_turn(self, thread_id: str) -> str | None:
        """Return only an active turn observed on this owned connection."""
        with self._activity.condition:
            return self._activity.active_turn(thread_id)

    def turn_completed(self, turn_id: str) -> bool:
        """Return whether this connection observed exact turn completion."""
        with self._activity.condition:
            return self._activity.was_completed(turn_id)

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        """Return the exact status classification observed on this connection."""
        with self._activity.condition:
            return self._activity.turn_outcome(turn_id)

    def latest_started_turn(self, thread_id: str) -> str | None:
        """Return the latest start seen for a thread on this connection."""
        with self._activity.condition:
            return self._activity.latest_started_turn(thread_id)

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        """Wait until the exact turn starts or completes too quickly to remain active."""
        observed = self._wait_for(
            lambda: self._activity.was_started(turn_id) or self._activity.was_completed(turn_id),
            timeout_seconds,
        )
        if not observed:
            return False
        with self._activity.condition:
            return self._activity.bind_started_turn(thread_id, turn_id)

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        """Wait until the exact turn emits its completion notification."""
        return self._wait_for(lambda: self._activity.was_completed(turn_id), timeout_seconds)

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        """Wait for a distinct later start, including a fast-completed turn."""
        observed = self._wait_for(
            lambda: self._activity.latest_started_turn(thread_id) != previous_turn_id,
            timeout_seconds,
        )
        if not observed:
            return None
        with self._activity.condition:
            return self._activity.latest_started_turn(thread_id)

    @property
    def pending_server_request(self) -> str | None:
        """Expose approval or input requests the manager cannot safely answer."""
        return self._activity.pending_server_request

    @property
    def is_alive(self) -> bool:
        """Return whether the owned app-server child is currently alive."""
        return self._running()

    def stderr_tail(self) -> tuple[str, ...]:
        """Return bounded in-memory stderr for immediate error reporting."""
        return self._activity.stderr_tail()

    def _request_started(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float,
    ) -> JsonObject:
        with self._request_lock:
            request_id = uuid4().hex
            self._write({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout_seconds
            with self._activity.condition:
                while True:
                    response = self._activity.take_response(request_id)
                    if response is not None:
                        result = response_result(method, response)
                        self._correlate_start_response(method, params, result)
                        return result
                    self._raise_if_closed(method)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        message = f"{method}_response_timeout"
                        raise AppServerError(message)
                    _ = self._activity.condition.wait(remaining)

    def _correlate_start_response(
        self,
        method: str,
        params: JsonObject,
        result: JsonObject,
    ) -> None:
        if method != "turn/start":
            return
        thread_id = params.get("threadId")
        turn_id = result_turn_id(result)
        if not isinstance(thread_id, str) or not thread_id or turn_id is None:
            return
        try:
            self._activity.correlate_turn(thread_id, turn_id)
        except AppServerProtocolError:
            self._write(
                {
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
            raise

    def _write(self, payload: JsonObject) -> None:
        process = self._process
        stdin = process.stdin if process is not None else None
        if stdin is None or stdin.closed:
            message = "resident_app_server_stdin_unavailable"
            raise AppServerError(message)
        with self._write_lock:
            _ = stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdin.flush()

    def _wait_for(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._activity.condition:
            while not predicate():
                self._raise_if_closed("notification")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _ = self._activity.condition.wait(remaining)
        return True

    def _raise_if_closed(self, method: str) -> None:
        closed_error = self._activity.closed_error
        if closed_error is not None and not self._running():
            message = f"{method}_failed:{closed_error}"
            raise AppServerError(message)

    def _running(self) -> bool:
        return self._process is not None and self._process.poll() is None
