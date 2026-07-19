"""Own one persistent Codex app-server connection for managed turns."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Self, cast, final
from uuid import uuid4

from scripts.app_server_protocol import (
    AppServerEventState,
    AppServerProtocolError,
    JsonObject,
    TurnOutcome,
    decode_object,
    response_result,
)
from scripts.codex_executable import resolve_codex_executable

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from typing import TextIO


class AppServerError(AppServerProtocolError):
    """Report a resident transport or app-server response failure."""


@final
class ResidentAppServer:
    """Start turns and interrupt only turns observed on this connection."""

    def __init__(self, expected_executable_sha256: str | None = None) -> None:
        """Create an inactive client with bounded in-memory diagnostics."""
        self._process: subprocess.Popen[str] | None = None
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._events = AppServerEventState()
        self._closed_error: str | None = None
        self._stderr: deque[str] = deque(maxlen=20)
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
        self._process = process
        self._closed_error = None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
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

    def close(self) -> None:
        """Terminate the owned app-server child without touching Codex Desktop."""
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                _ = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                _ = process.wait(timeout=2.0)
        with self._condition:
            self._closed_error = "resident_app_server_closed"
            self._condition.notify_all()

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
        with self._condition:
            return self._events.active_turn(thread_id)

    def turn_completed(self, turn_id: str) -> bool:
        """Return whether this connection observed exact turn completion."""
        with self._condition:
            return self._events.was_completed(turn_id)

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        """Return the exact status classification observed on this connection."""
        with self._condition:
            return self._events.turn_outcome(turn_id)

    def latest_started_turn(self, thread_id: str) -> str | None:
        """Return the latest start seen for a thread on this connection."""
        with self._condition:
            return self._events.latest_started_turn(thread_id)

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        """Wait until the exact turn starts or completes too quickly to remain active."""
        observed = self._wait_for(
            lambda: self._events.was_started(turn_id) or self._events.was_completed(turn_id),
            timeout_seconds,
        )
        if not observed:
            return False
        with self._condition:
            return self._events.bind_started_turn(thread_id, turn_id)

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        """Wait until the exact turn emits its completion notification."""
        return self._wait_for(lambda: self._events.was_completed(turn_id), timeout_seconds)

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        """Wait for a distinct later start, including a fast-completed turn."""
        observed = self._wait_for(
            lambda: self._events.latest_started_turn(thread_id) != previous_turn_id,
            timeout_seconds,
        )
        if not observed:
            return None
        with self._condition:
            return self._events.latest_started_turn(thread_id)

    @property
    def pending_server_request(self) -> str | None:
        """Expose approval or input requests the manager cannot safely answer."""
        with self._condition:
            return self._events.pending_server_request

    def stderr_tail(self) -> tuple[str, ...]:
        """Return bounded in-memory stderr for immediate error reporting."""
        with self._condition:
            return tuple(self._stderr)

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
            with self._condition:
                while True:
                    response = self._events.take_response(request_id)
                    if response is not None:
                        return response_result(method, response)
                    self._raise_if_closed(method)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        message = f"{method}_response_timeout"
                        raise AppServerError(message)
                    _ = self._condition.wait(min(remaining, 0.5))

    def _write(self, payload: JsonObject) -> None:
        process = self._process
        stdin = process.stdin if process is not None else None
        if stdin is None or stdin.closed:
            message = "resident_app_server_stdin_unavailable"
            raise AppServerError(message)
        with self._write_lock:
            _ = stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdin.flush()

    def _read_stdout(self) -> None:
        process = self._process
        stdout = process.stdout if process is not None else None
        if stdout is None:
            return
        text_stdout = cast("TextIO", stdout)
        try:
            for raw_line in text_stdout:
                decoded = decode_object(raw_line)
                if decoded is not None:
                    with self._condition:
                        self._events.record(decoded)
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._closed_error = "resident_app_server_exited"
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        process = self._process
        stderr = process.stderr if process is not None else None
        if stderr is None:
            return
        text_stderr = cast("TextIO", stderr)
        for raw_line in text_stderr:
            with self._condition:
                self._stderr.append(raw_line.rstrip()[:500])

    def _wait_for(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while not predicate():
                self._raise_if_closed("notification")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _ = self._condition.wait(min(remaining, 0.5))
        return True

    def _raise_if_closed(self, method: str) -> None:
        if self._closed_error is not None and not self._running():
            message = f"{method}_failed:{self._closed_error}"
            raise AppServerError(message)

    def _running(self) -> bool:
        return self._process is not None and self._process.poll() is None
