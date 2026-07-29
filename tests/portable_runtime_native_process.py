from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, final

from tests.portable_runtime_windows_job import JobCleanupReport, WindowsJobProcess
from tests.portable_runtime_windows_ownership import (
    CleanupFailure,
    raise_collected,
    raise_with_cleanup,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class NativeProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class ProbeContext:
    data_root: Path | None
    started: float

    def render(self) -> str:
        elapsed = time.monotonic() - self.started
        lock_exists = (
            False if self.data_root is None else (self.data_root / ".portable-python.lock").exists()
        )
        stages = (
            []
            if self.data_root is None
            else sorted(path.name for path in self.data_root.glob(".portable-python-stage-*"))
        )
        return f"elapsed={elapsed:.3f}s lock_exists={lock_exists} stages={stages!r}"


def start_process(
    args: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> WindowsJobProcess:
    if os.name != "nt":
        reason = "native portable-runtime probe requires Windows Job Objects"
        raise NativeProbeError(reason)
    try:
        return WindowsJobProcess.start(list(args), cwd=cwd, environment=environment)
    except OSError as exc:
        reason = f"native portable-runtime launch failed: {exc}"
        raise NativeProbeError(reason) from exc


def run_bounded_process(  # noqa: PLR0913
    args: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    phase: str,
    data_root: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    context = ProbeContext(data_root, time.monotonic())
    process = start_process(args, cwd=cwd, environment=environment)
    output: _OutputCollector | None = None
    try:
        output = _OutputCollector()
        output.start(process)
        process.stdin.close()
        result = _complete_bounded_process(
            process,
            output,
            args=args,
            timeout_seconds=timeout_seconds,
            phase=phase,
            context=context,
        )
    except (OSError, RuntimeError) as primary:
        raise_with_cleanup(primary, _cleanup_acquired(process, output))
    cleanup_failures = _cleanup_acquired(process, output)
    raise_collected(list(cleanup_failures))
    return result


def _complete_bounded_process(  # noqa: PLR0913
    process: WindowsJobProcess,
    output: _OutputCollector,
    *,
    args: Sequence[str],
    timeout_seconds: float,
    phase: str,
    context: ProbeContext,
) -> subprocess.CompletedProcess[bytes]:
    cleanup = JobCleanupReport(terminated=False, closed=False)
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        cleanup = process.terminate_owned_job()
        _ = process.wait(timeout=10)
        captured = output.finish()
        raise NativeProbeError(
            diagnostic(
                process,
                phase=phase,
                reason=(f"timed out after {timeout_seconds:g}s {context.render()}"),
                output=captured,
                cleanup=cleanup.render(),
            )
        ) from exc

    captured = output.finish()
    if returncode != 0:
        raise NativeProbeError(
            diagnostic(
                process,
                phase=phase,
                reason=f"exited with status {returncode} {context.render()}",
                output=captured,
                cleanup=cleanup.render(),
            )
        )
    return subprocess.CompletedProcess(
        list(args),
        returncode,
        stdout=captured.stdout,
        stderr=captured.stderr,
    )


def _cleanup_acquired(
    process: WindowsJobProcess,
    output: _OutputCollector | None,
) -> tuple[CleanupFailure, ...]:
    failures: list[CleanupFailure] = []
    callbacks = [("close(process)", process.close)]
    if output is not None:
        callbacks.append(("join(output-readers)", output.close))
    for label, callback in callbacks:
        try:
            callback()
        except (OSError, RuntimeError) as error:
            failures.append(CleanupFailure(label, error))
    return tuple(failures)


def diagnostic(
    process: WindowsJobProcess,
    *,
    phase: str,
    reason: str,
    output: ProcessOutput,
    cleanup: str,
) -> str:
    return (
        f"native portable-runtime phase={phase} {reason}; "
        f"pid={process.pid}; returncode={process.poll()}; cleanup={cleanup!r}; "
        f"stdout={_decode(output.stdout)!r}; stderr={_decode(output.stderr)!r}"
    )


def read_lines(stream: BinaryIO) -> list[bytes]:
    return list(iter(stream.readline, b""))


def read_all(stream: BinaryIO) -> bytes:
    return stream.read()


@final
class _OutputCollector:
    """Own reader threads incrementally so partial startup is still recoverable."""

    __slots__ = ("stderr_chunks", "stderr_thread", "stdout_chunks", "stdout_thread")

    def __init__(self) -> None:
        self.stdout_chunks: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

    def start(self, process: WindowsJobProcess) -> None:
        self.stdout_thread = _reader_thread(
            process.stdout,
            self.stdout_chunks,
            "cmw-native-stdout",
        )
        self.stdout_thread.start()
        self.stderr_thread = _reader_thread(
            process.stderr,
            self.stderr_chunks,
            "cmw-native-stderr",
        )
        self.stderr_thread.start()

    def finish(self) -> ProcessOutput:
        self.close()
        return ProcessOutput(
            stdout=b"".join(self.stdout_chunks),
            stderr=b"".join(self.stderr_chunks),
        )

    def close(self) -> None:
        errors: list[CleanupFailure] = []
        for label, thread in (
            ("join(stdout-reader)", self.stdout_thread),
            ("join(stderr-reader)", self.stderr_thread),
        ):
            if thread is None or thread.ident is None:
                continue
            try:
                thread.join(timeout=10)
            except RuntimeError as error:
                errors.append(CleanupFailure(label, error))
            else:
                if thread.is_alive():
                    reason = "native portable-runtime pipe reader did not reach EOF"
                    errors.append(CleanupFailure(label, NativeProbeError(reason)))
        raise_collected(errors)


def _reader_thread(
    stream: BinaryIO,
    chunks: list[bytes],
    name: str,
) -> threading.Thread:
    def read_stream() -> None:
        chunks.append(read_all(stream))

    return threading.Thread(target=read_stream, name=name, daemon=False)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")
