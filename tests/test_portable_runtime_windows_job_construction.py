from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    Literal,
    Never,
    NoReturn,
    assert_never,
    cast,
)

import pytest

from tests import portable_runtime_windows_start as windows_start
from tests.portable_runtime_windows_api import WindowsJobError
from tests.portable_runtime_windows_job import WindowsJobProcess

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object construction cleanup runs only on Windows",
)

type FailureSeam = Literal["create", "configure", "process", "assign", "resume"]
type CreateArgument = str | None | bool | int | dict[str, str] | subprocess.STARTUPINFO


class _ResourceTrace:
    def __init__(self) -> None:
        self.handles: list[int] = []
        self.closed_handles: list[int] = []
        self.fds: list[int] = []
        self.closed_fds: list[int] = []
        self.terminated_processes: list[int] = []


@pytest.mark.parametrize(
    ("seam", "operation"),
    [
        ("create", "CreateJobObjectW"),
        ("configure", "SetInformationJobObject"),
        ("process", "CreateProcessW"),
        ("assign", "AssignProcessToJobObject"),
        ("resume", "ResumeThread"),
    ],
)
def test_construction_failure_closes_every_prior_resource_before_child_runs(
    seam: FailureSeam,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-started"
    trace = _ResourceTrace()
    _install_trace(trace, monkeypatch)
    _inject_failure(seam, operation, monkeypatch)

    with pytest.raises((OSError, WindowsJobError), match=operation):
        _ = WindowsJobProcess.start(
            _marker_command(marker),
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    time.sleep(0.05)
    assert not marker.exists()
    assert sorted(trace.closed_handles) == sorted(trace.handles)
    assert sorted(trace.closed_fds) == sorted(trace.fds)
    if seam in {"assign", "resume"}:
        assert trace.terminated_processes


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_stream_acquisition_failure_closes_streams_fds_process_and_job(
    failure_call: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = _ResourceTrace()
    _install_trace(trace, monkeypatch)
    real_fdopen = windows_start.os.fdopen
    streams: list[BinaryIO] = []
    calls = 0

    def fdopen(fd: int, mode: str, buffering: int) -> BinaryIO:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            reason = f"fdopen-{failure_call}"
            raise OSError(reason)
        stream = cast("BinaryIO", real_fdopen(fd, mode, buffering))
        streams.append(stream)
        return stream

    monkeypatch.setattr(windows_start.os, "fdopen", fdopen)

    with pytest.raises(OSError, match=rf"fdopen-{failure_call}"):
        _ = WindowsJobProcess.start(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    assert all(stream.closed for stream in streams)
    assert sorted(trace.closed_handles) == sorted(trace.handles)
    assert trace.terminated_processes


def test_primary_thread_close_failure_retries_owned_handle_and_aborts_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = _ResourceTrace()
    primary_thread: list[int] = []
    _install_trace(trace, monkeypatch, primary_thread=primary_thread)
    real_close_handle = windows_start.close_handle
    injected = False

    def close_handle(handle: int) -> None:
        nonlocal injected
        if primary_thread and handle == primary_thread[0] and not injected:
            injected = True
            reason = "primary-thread-close-injected"
            raise OSError(reason)
        real_close_handle(handle)

    monkeypatch.setattr(windows_start, "close_handle", close_handle)

    with pytest.raises(OSError, match="primary-thread-close-injected"):
        _ = WindowsJobProcess.start(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    assert injected
    assert sorted(trace.closed_handles) == sorted(trace.handles)
    assert trace.terminated_processes


def _install_trace(
    trace: _ResourceTrace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary_thread: list[int] | None = None,
) -> None:
    real_create_job = windows_start.create_job
    real_create_process = windows_start.winapi.CreateProcess
    real_pipe = windows_start.os.pipe
    real_close_handle = windows_start.close_handle
    real_close_fd = windows_start.os.close
    real_terminate = windows_start.terminate_process

    def create_job() -> int:
        handle = real_create_job()
        trace.handles.append(handle)
        return handle

    def create_process(  # noqa: PLR0913
        application_name: str | None,
        command_line: str | None,
        process_attributes: int | None,
        thread_attributes: int | None,
        inherit_handles: bool,
        creation_flags: int,
        environment: dict[str, str],
        current_directory: str | None,
        startup_info: subprocess.STARTUPINFO,
    ) -> tuple[int, int, int, int]:
        result = real_create_process(
            application_name,
            command_line,
            process_attributes,
            thread_attributes,
            inherit_handles,
            creation_flags,
            environment,
            current_directory,
            startup_info,
        )
        trace.handles.extend(result[:2])
        if primary_thread is not None:
            primary_thread.append(result[1])
        return result

    def create_pipe() -> tuple[int, int]:
        pair = real_pipe()
        trace.fds.extend(pair)
        return pair

    def close_handle(handle: int) -> None:
        real_close_handle(handle)
        trace.closed_handles.append(handle)

    def close_fd(fd: int) -> None:
        real_close_fd(fd)
        trace.closed_fds.append(fd)

    def terminate(process_handle: int) -> None:
        real_terminate(process_handle)
        trace.terminated_processes.append(process_handle)

    monkeypatch.setattr(windows_start, "create_job", create_job)
    monkeypatch.setattr(windows_start.winapi, "CreateProcess", create_process)
    monkeypatch.setattr(windows_start.os, "pipe", create_pipe)
    monkeypatch.setattr(windows_start, "close_handle", close_handle)
    monkeypatch.setattr(windows_start.os, "close", close_fd)
    monkeypatch.setattr(windows_start, "terminate_process", terminate)


def _inject_failure(
    seam: str,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_arguments: CreateArgument) -> NoReturn:
        if seam == "process":
            raise OSError(operation)
        raise WindowsJobError(operation, 5)

    match seam:
        case "create":
            monkeypatch.setattr(windows_start, "create_job", fail)
        case "configure":
            monkeypatch.setattr(windows_start, "configure_kill_on_close", fail)
        case "process":
            monkeypatch.setattr(windows_start.winapi, "CreateProcess", fail)
        case "assign":
            monkeypatch.setattr(windows_start, "assign_process_to_job", fail)
        case "resume":
            monkeypatch.setattr(windows_start, "resume_thread", fail)
        case _:
            assert_never(cast("Never", seam))


def _marker_command(marker: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        (f"from pathlib import Path;Path({str(marker)!r}).write_text('started',encoding='utf-8')"),
    ]
