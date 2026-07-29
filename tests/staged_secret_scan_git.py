from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, final

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

GIT: Final[str | None] = shutil.which("git")
GIT_TIMEOUT_SECONDS: Final[float] = 5.0
MAX_GIT_OUTPUT_BYTES: Final[int] = 4 * 1024 * 1024
GIT_UNAVAILABLE: Final[str] = "GIT_UNAVAILABLE"
GIT_TIMEOUT: Final[str] = "GIT_TIMEOUT"
GIT_EXECUTION_ERROR: Final[str] = "GIT_EXECUTION_ERROR"
GIT_ERROR: Final[str] = "GIT_ERROR"
GIT_OUTPUT_TOO_LARGE: Final[str] = "GIT_OUTPUT_TOO_LARGE"
_TRUSTED_GIT_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
    }
)


@final
class ScanError(RuntimeError):
    """A fail-closed scanner result with a public rule and safe path."""

    rule: str
    path: str

    def __init__(self, rule: str, path: str = ".") -> None:
        super().__init__(rule)
        self.rule = rule
        self.path = path


class _ReadablePipe(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _DrainTarget:
    pipe: _ReadablePipe
    limit: int
    output: bytearray
    stop_requested: threading.Event
    exceeded: threading.Event
    failed: threading.Event


def _drain(target: _DrainTarget) -> None:
    try:
        while chunk := target.pipe.read(65_536):
            remaining = target.limit - len(target.output)
            if len(chunk) > remaining:
                target.output.extend(chunk[:remaining])
                target.exceeded.set()
                target.stop_requested.set()
                return
            target.output.extend(chunk)
    except (OSError, ValueError):
        target.failed.set()
        target.stop_requested.set()


def _git_environment(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    child_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    if environment is not None:
        if not set(environment).issubset(_TRUSTED_GIT_VARIABLES):
            raise ScanError(GIT_EXECUTION_ERROR)
        child_environment.update(environment)
    child_environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return child_environment


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        _ = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        _ = process.wait(timeout=1)


def _wait_for_process(
    process: subprocess.Popen[bytes],
    stop_requested: threading.Event,
) -> bool:
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    while process.poll() is None and not stop_requested.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        _ = stop_requested.wait(min(remaining, 0.05))
    return False


def _collect_output(process: subprocess.Popen[bytes], limit: int) -> bytes:
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    stop_requested = threading.Event()
    exceeded = threading.Event()
    reader_failed = threading.Event()
    stdout_thread = threading.Thread(
        target=_drain,
        args=(
            _DrainTarget(
                process.stdout,
                limit,
                stdout,
                stop_requested,
                exceeded,
                reader_failed,
            ),
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(
            _DrainTarget(
                process.stderr,
                65_536,
                stderr,
                stop_requested,
                exceeded,
                reader_failed,
            ),
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = _wait_for_process(process, stop_requested)
    if timed_out or stop_requested.is_set():
        _terminate(process)
    else:
        _ = process.wait(timeout=1)
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    readers_stopped = not stdout_thread.is_alive() and not stderr_thread.is_alive()
    if readers_stopped:
        process.stdout.close()
        process.stderr.close()
    else:
        reader_failed.set()
    if timed_out:
        raise ScanError(GIT_TIMEOUT)
    if exceeded.is_set():
        raise ScanError(GIT_OUTPUT_TOO_LARGE)
    if reader_failed.is_set():
        raise ScanError(GIT_EXECUTION_ERROR)
    if process.returncode != 0:
        raise ScanError(GIT_ERROR)
    return bytes(stdout)


def run_git(
    cwd: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
    max_stdout_bytes: int | None = None,
) -> bytes:
    """Run Git with bounded streaming output, timeout, and redacted stderr."""
    if GIT is None:
        raise ScanError(GIT_UNAVAILABLE)
    limit = MAX_GIT_OUTPUT_BYTES if max_stdout_bytes is None else max_stdout_bytes
    try:
        process = subprocess.Popen(  # noqa: S603
            (GIT, *arguments),
            cwd=cwd,
            env=_git_environment(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ScanError(GIT_EXECUTION_ERROR) from error
    return _collect_output(process, limit)
