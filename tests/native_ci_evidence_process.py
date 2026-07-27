"""Run one gh child with strict time and byte ceilings without memory capture."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Final

if TYPE_CHECKING:
    from pathlib import Path

from tests.native_ci_evidence_models import EvidenceError

_GH_FAILED: Final = "gh_failed"
_GH_TIMEOUT: Final = "gh_timeout"
_GH_OUTPUT_TOO_LARGE: Final = "gh_output_too_large"
_POLL_SECONDS: Final = 0.01
_STOP_SECONDS: Final = 5


@dataclass(frozen=True, slots=True)
class GhInvocation:
    """One executable request and its public resource ceilings."""

    executable: Path
    arguments: tuple[str, ...]
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int


def run_gh_spooled(invocation: GhInvocation) -> str:
    """Return bounded UTF-8 stdout without retaining unbounded child output."""
    with tempfile.TemporaryFile("w+b") as stdout, tempfile.TemporaryFile("w+b") as stderr:
        process = _start(invocation, stdout, stderr)
        deadline = time.monotonic() + invocation.timeout_seconds
        while process.poll() is None:
            if _size(stdout) > invocation.stdout_limit or _size(stderr) > invocation.stderr_limit:
                _stop(process)
                raise EvidenceError(_GH_OUTPUT_TOO_LARGE)
            if time.monotonic() >= deadline:
                _stop(process)
                raise EvidenceError(_GH_TIMEOUT)
            time.sleep(_POLL_SECONDS)
        if _size(stdout) > invocation.stdout_limit or _size(stderr) > invocation.stderr_limit:
            raise EvidenceError(_GH_OUTPUT_TOO_LARGE)
        if process.returncode != 0:
            raise EvidenceError(_GH_FAILED)
        _ = stdout.seek(0)
        try:
            return stdout.read(invocation.stdout_limit + 1).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise EvidenceError(_GH_FAILED) from None


def _start(
    invocation: GhInvocation,
    stdout: BinaryIO,
    stderr: BinaryIO,
) -> subprocess.Popen[bytes]:
    gh = invocation.executable
    command = (
        (
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/s",
            "/c",
            str(gh),
            *invocation.arguments,
        )
        if os.name == "nt" and gh.suffix.casefold() in {".bat", ".cmd"}
        else (str(gh), *invocation.arguments)
    )
    try:
        return subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except OSError:
        raise EvidenceError(_GH_FAILED) from None


def _size(stream: BinaryIO) -> int:
    return os.fstat(stream.fileno()).st_size


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt" and _interrupt_windows_group(process):
        return
    process.kill()
    try:
        _ = process.wait(timeout=_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        _ = process.wait(timeout=_STOP_SECONDS)


def _interrupt_windows_group(process: subprocess.Popen[bytes]) -> bool:
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        _ = process.wait(timeout=_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True
