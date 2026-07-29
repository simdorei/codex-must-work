"""Bounded, phase-aware process support for native Windows runtime tests."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn, cast

from tests.portable_runtime_native_process import (
    NativeProbeError,
    ProbeContext,
    ProcessOutput,
    diagnostic,
    run_bounded_process,
    start_process,
)
from tests.portable_runtime_protocol_cleanup import cleanup_protocol_resources
from tests.portable_runtime_windows_ownership import (
    ResourceCleanupError,
    raise_collected,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.portable_runtime_windows_job import WindowsJobProcess
    from tests.test_portable_runtime import JsonObject


@dataclass(frozen=True, slots=True)
class ProbeTimeouts:
    provision: float = 90.0
    protocol: float = 15.0
    shutdown: float = 10.0


DEFAULT_TIMEOUTS: Final = ProbeTimeouts()


def clean_native_mcp_probe(  # noqa: PLR0913, PLR0915
    launcher: Path,
    bootstrap: str,
    *,
    cwd: Path,
    environment: dict[str, str],
    data_root: Path,
    requests: tuple[JsonObject, JsonObject, JsonObject],
    timeouts: ProbeTimeouts = DEFAULT_TIMEOUTS,
) -> tuple[JsonObject, JsonObject]:
    """Provision, initialize, list tools, and shut down with separate bounds."""
    provisioned = run_bounded_process(
        [str(launcher), "-c", "print('portable-runtime-ready')"],
        cwd=cwd,
        environment=environment,
        phase="provision",
        data_root=data_root,
        timeout_seconds=timeouts.provision,
    )
    if provisioned.stdout.decode("utf-8").splitlines() != ["portable-runtime-ready"]:
        reason = "phase=provision unexpected_stdout"
        raise NativeProbeError(reason)

    process = start_process(
        [str(launcher), bootstrap],
        cwd=cwd,
        environment=environment,
    )
    owned_readers: list[threading.Thread] = []
    context = ProbeContext(data_root, time.monotonic())
    lines: queue.Queue[bytes] = queue.Queue()
    stderr_chunks: list[bytes] = []
    observed: list[bytes] = []
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    try:
        stdout_thread = _stdout_reader(process, lines)
        owned_readers.append(stdout_thread)
        stdout_thread.start()
        stderr_thread = _stderr_reader(process, stderr_chunks)
        owned_readers.append(stderr_thread)
        stderr_thread.start()
        _send(process, requests[0])
        initialize = _await_response(
            process,
            lines,
            response_id=1,
            phase="initialize",
            timeout_seconds=timeouts.protocol,
            observed=observed,
        )
        _send(process, requests[1])
        _send(process, requests[2])
        tools = _await_response(
            process,
            lines,
            response_id=2,
            phase="tools_list",
            timeout_seconds=timeouts.protocol,
            observed=observed,
        )
        process.stdin.close()
        try:
            returncode = process.wait(timeouts.shutdown)
        except subprocess.TimeoutExpired:
            _raise_protocol_failure(
                "shutdown",
                process,
                observed,
                stderr_chunks,
                stdout_thread,
                stderr_thread,
                context,
                detail=f"timeout={timeouts.shutdown:g}",
            )
        if returncode != 0:
            _raise_protocol_failure(
                "shutdown",
                process,
                observed,
                stderr_chunks,
                stdout_thread,
                stderr_thread,
                context,
                detail=f"returncode={returncode}",
            )
        _join_readers(stdout_thread, stderr_thread)
    except (BrokenPipeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if stdout_thread is None or stderr_thread is None:
            raise
        _raise_protocol_failure(
            "protocol",
            process,
            observed,
            stderr_chunks,
            stdout_thread,
            stderr_thread,
            context,
            detail=type(error).__name__,
        )
    except NativeProbeError as error:
        if stdout_thread is None or stderr_thread is None:
            raise
        phase = str(error).partition("phase=")[2].partition(" ")[0] or "protocol"
        _raise_protocol_failure(
            phase,
            process,
            observed,
            stderr_chunks,
            stdout_thread,
            stderr_thread,
            context,
            detail=str(error),
        )
    finally:
        primary = sys.exception()
        failures = cleanup_protocol_resources(process, owned_readers)
        if failures:
            if isinstance(primary, Exception):
                raise ResourceCleanupError(primary, failures) from primary
            raise_collected(list(failures))
    return initialize, tools


def _send(process: WindowsJobProcess, request: JsonObject) -> None:
    _ = process.stdin.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
    process.stdin.flush()


def _await_response(  # noqa: PLR0913
    process: WindowsJobProcess,
    lines: queue.Queue[bytes],
    *,
    response_id: int,
    phase: str,
    timeout_seconds: float,
    observed: list[bytes],
) -> JsonObject:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = f"phase={phase} pid={process.pid} timeout={timeout_seconds:g}"
            raise NativeProbeError(reason)
        try:
            raw = lines.get(timeout=remaining)
        except queue.Empty:
            reason = f"phase={phase} pid={process.pid} timeout={timeout_seconds:g}"
            raise NativeProbeError(reason) from None
        if raw == b"":
            reason = (
                f"phase={phase} pid={process.pid} " + f"unexpected_eof returncode={process.poll()}"
            )
            raise NativeProbeError(reason)
        observed.append(raw)
        decoded = cast("JsonObject", json.loads(raw.decode("utf-8")))
        if decoded.get("id") == response_id:
            return decoded


def _raise_protocol_failure(  # noqa: PLR0913
    phase: str,
    process: WindowsJobProcess,
    observed: list[bytes],
    stderr_chunks: list[bytes],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    context: ProbeContext,
    *,
    detail: str,
) -> NoReturn:
    cleanup = process.terminate_owned_job()
    _ = process.wait(10)
    _join_readers(stdout_thread, stderr_thread)
    raise NativeProbeError(
        diagnostic(
            process,
            phase=phase,
            reason=f"{detail} {context.render()}",
            output=ProcessOutput(
                stdout=b"".join(observed),
                stderr=b"".join(stderr_chunks),
            ),
            cleanup=cleanup.render(),
        )
    )


def _stdout_reader(
    process: WindowsJobProcess,
    lines: queue.Queue[bytes],
) -> threading.Thread:
    def read_lines() -> None:
        for raw in iter(process.stdout.readline, b""):
            lines.put(raw)
        lines.put(b"")

    return threading.Thread(target=read_lines, name="cmw-native-stdout", daemon=False)


def _stderr_reader(
    process: WindowsJobProcess,
    chunks: list[bytes],
) -> threading.Thread:
    def read_stderr() -> None:
        chunks.append(process.stderr.read())

    return threading.Thread(target=read_stderr, name="cmw-native-stderr", daemon=False)


def _join_readers(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(timeout=10)
        if thread.is_alive():
            reason = "phase=cleanup pipe_reader_timeout"
            raise NativeProbeError(reason)
