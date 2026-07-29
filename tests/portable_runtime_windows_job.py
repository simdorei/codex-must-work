"""Suspended admission and resource ownership for one Windows Job process tree."""

from __future__ import annotations

import _winapi
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Final, final

from tests.portable_runtime_windows_api import (
    close_handle,
    terminate_job,
)
from tests.portable_runtime_windows_ownership import (
    CleanupFailure,
    raise_collected,
)
from tests.portable_runtime_windows_start import start_windows_job

__all__ = (
    "JobCleanupReport",
    "JobResourceState",
    "WindowsJobProcess",
    "close_handle",
    "get_process_exit_code",
    "terminate_job",
    "wait_for_process",
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_WAIT_MILLISECONDS: Final = 1000


@dataclass(frozen=True, slots=True)
class JobCleanupReport:
    """Observable result of identity-bound job termination and close."""

    terminated: bool
    closed: bool

    def render(self) -> str:
        return f"job_terminated={self.terminated} job_closed={self.closed}"


@dataclass(frozen=True, slots=True)
class JobResourceState:
    closed: bool
    process_handle: int
    job_handle: int
    streams_closed: tuple[bool, bool, bool]


@final
class WindowsJobProcess:
    """Own one suspended-admitted process, its job, handles, and stdio pipes."""

    __slots__ = (
        "_closed",
        "_job_handle",
        "_process_handle",
        "_returncode",
        "args",
        "pid",
        "stderr",
        "stdin",
        "stdout",
    )

    def __init__(  # noqa: PLR0913
        self,
        args: tuple[str, ...],
        pid: int,
        process_handle: int,
        job_handle: int,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> None:
        self.args: tuple[str, ...] = args
        self.pid: int = pid
        self.stdin: BinaryIO = stdin
        self.stdout: BinaryIO = stdout
        self.stderr: BinaryIO = stderr
        self._process_handle: int = process_handle
        self._job_handle: int = job_handle
        self._returncode: int | None = None
        self._closed: bool = False

    @classmethod
    def start(
        cls,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> WindowsJobProcess:
        """Create suspended, assign by retained handle, then resume."""
        started = start_windows_job(command, cwd=cwd, environment=environment)
        return cls(
            started.args,
            started.pid,
            started.process_handle,
            started.job_handle,
            started.stdin,
            started.stdout,
            started.stderr,
        )

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def resource_state(self) -> JobResourceState:
        """Expose cleanup state without transferring or re-resolving identity."""
        return JobResourceState(
            closed=self._closed,
            process_handle=self._process_handle,
            job_handle=self._job_handle,
            streams_closed=(
                self.stdin.closed,
                self.stdout.closed,
                self.stderr.closed,
            ),
        )

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if wait_for_process(self._process_handle, 0) == _winapi.WAIT_TIMEOUT:
            return None
        self._returncode = get_process_exit_code(self._process_handle)
        return self._returncode

    def wait(self, timeout: float) -> int:
        result = wait_for_process(
            self._process_handle,
            max(0, round(timeout * _WAIT_MILLISECONDS)),
        )
        if result == _winapi.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self._returncode = get_process_exit_code(self._process_handle)
        return self._returncode

    def terminate_owned_job(self) -> JobCleanupReport:
        if self._job_handle == 0:
            return JobCleanupReport(terminated=False, closed=False)
        job_handle = self._job_handle
        errors: list[CleanupFailure] = []
        _ = _attempt(errors, "TerminateJobObject", terminate_job, job_handle)
        if _attempt(errors, "CloseHandle(job)", close_handle, job_handle):
            self._job_handle = 0
        raise_collected(errors)
        return JobCleanupReport(terminated=True, closed=True)

    def close(self) -> None:
        """Idempotently release streams and every retained identity handle."""
        if self._closed:
            return
        errors: list[CleanupFailure] = []
        alive = self._process_handle != 0
        if alive:
            try:
                alive = self.poll() is None
            except OSError as error:
                errors.append(CleanupFailure("poll(process)", error))
        if alive and self._job_handle != 0:
            terminated = _attempt(
                errors,
                "TerminateJobObject",
                terminate_job,
                self._job_handle,
            )
            if terminated:
                _ = _attempt(errors, "wait(process)", self.wait, 10)
        self._close_streams(errors)
        process_handle = self._process_handle
        if process_handle != 0 and _attempt(
            errors,
            "CloseHandle(process)",
            close_handle,
            process_handle,
        ):
            self._process_handle = 0
        job_handle = self._job_handle
        if job_handle != 0 and _attempt(
            errors,
            "CloseHandle(job)",
            close_handle,
            job_handle,
        ):
            self._job_handle = 0
        self._closed = (
            self._process_handle == 0
            and self._job_handle == 0
            and self.stdin.closed
            and self.stdout.closed
            and self.stderr.closed
        )
        raise_collected(errors)

    def _close_streams(self, errors: list[CleanupFailure]) -> None:
        """Attempt each still-owned stream once without hiding a failed close."""
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream.closed:
                continue
            label = f"close({stream.name})"
            released = _attempt(errors, label, stream.close)
            if released and not stream.closed:
                errors.append(
                    CleanupFailure(
                        label,
                        RuntimeError("stream remained open after close"),
                    )
                )


def _attempt(
    errors: list[CleanupFailure],
    label: str,
    callback: Callable[..., int | None],
    *arguments: int,
) -> bool:
    try:
        _ = callback(*arguments)
    except (OSError, RuntimeError) as error:
        errors.append(CleanupFailure(label, error))
        return False
    return True


def wait_for_process(process_handle: int, milliseconds: int) -> int:
    return _winapi.WaitForSingleObject(process_handle, milliseconds)


def get_process_exit_code(process_handle: int) -> int:
    return _winapi.GetExitCodeProcess(process_handle)
