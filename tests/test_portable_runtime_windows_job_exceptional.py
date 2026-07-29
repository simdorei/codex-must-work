from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests import portable_runtime_windows_job as windows_job
from tests import portable_runtime_windows_start as windows_start
from tests.portable_runtime_windows_api import WindowsJobError
from tests.portable_runtime_windows_ownership import ResourceCleanupError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object exceptional cleanup runs only on Windows",
)


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_partial_pipe_failure_closes_job_and_every_acquired_fd(
    failure_call: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_jobs: list[int] = []
    created_fds: list[int] = []
    closed_handles: list[int] = []
    closed_fds: list[int] = []
    real_create_job = windows_start.create_job
    real_pipe = windows_start.os.pipe
    real_close_handle = windows_start.close_handle
    real_close_fd = windows_start.os.close
    calls = 0

    def create_job() -> int:
        handle = real_create_job()
        created_jobs.append(handle)
        return handle

    def create_pipe() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            reason = f"pipe-{failure_call}"
            raise OSError(reason)
        pair = real_pipe()
        created_fds.extend(pair)
        return pair

    def close_handle(handle: int) -> None:
        closed_handles.append(handle)
        real_close_handle(handle)

    def close_fd(fd: int) -> None:
        closed_fds.append(fd)
        real_close_fd(fd)

    monkeypatch.setattr(windows_start, "create_job", create_job)
    monkeypatch.setattr(windows_start.os, "pipe", create_pipe)
    monkeypatch.setattr(windows_start, "close_handle", close_handle)
    monkeypatch.setattr(windows_start.os, "close", close_fd)

    try:
        with pytest.raises(OSError, match=rf"pipe-{failure_call}"):
            _ = windows_job.WindowsJobProcess.start(
                [sys.executable, "-c", "raise AssertionError('must not run')"],
                cwd=tmp_path,
                environment=os.environ.copy(),
            )

        assert closed_handles == created_jobs
        assert sorted(closed_fds) == sorted(created_fds)
    finally:
        for fd in set(created_fds) - set(closed_fds):
            real_close_fd(fd)
        for handle in set(created_jobs) - set(closed_handles):
            real_close_handle(handle)


def test_wait_failure_still_closes_all_resources_and_repeat_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = windows_job.WindowsJobProcess.start(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        environment=os.environ.copy(),
    )
    original_wait = windows_job.WindowsJobProcess.wait

    def fail_wait(
        _process: windows_job.WindowsJobProcess,
        _timeout: float,
    ) -> NoReturn:
        reason = "wait-injected"
        raise OSError(reason)

    try:
        with monkeypatch.context() as injected:
            injected.setattr(windows_job.WindowsJobProcess, "wait", fail_wait)
            with pytest.raises(OSError, match="wait-injected"):
                managed.close()

        assert managed.stdin.closed
        assert managed.stdout.closed
        assert managed.stderr.closed
        state = managed.resource_state()
        assert state.job_handle == 0
        assert state.process_handle == 0
        assert state.closed is True
        managed.close()
    finally:
        state = managed.resource_state()
        if state.process_handle != 0:
            if state.job_handle != 0:
                _ = managed.terminate_owned_job()
            _ = original_wait(managed, 5)
            for stream in (managed.stdin, managed.stdout, managed.stderr):
                stream.close()
            windows_start.close_handle(state.process_handle)


def test_child_fd_close_failure_is_retried_without_losing_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_fds: list[int] = []
    closed_fds: list[int] = []
    real_pipe = windows_start.os.pipe
    real_close = windows_start.os.close
    injected = False

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        created_fds.extend(pair)
        return pair

    def close(fd: int) -> None:
        nonlocal injected
        if not injected:
            injected = True
            reason = "child-fd-close-injected"
            raise OSError(reason)
        real_close(fd)
        closed_fds.append(fd)

    monkeypatch.setattr(windows_start.os, "pipe", pipe)
    monkeypatch.setattr(windows_start.os, "close", close)

    with pytest.raises(OSError, match="child-fd-close-injected"):
        _ = windows_job.WindowsJobProcess.start(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    assert injected
    assert sorted(closed_fds) == sorted(created_fds)


def test_construction_preserves_primary_winerror_and_aggregates_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = windows_start.close_handle
    close_calls: list[int] = []
    fail_before_release = True

    def configure(_job_handle: int) -> NoReturn:
        operation = "SetInformationJobObject"
        raise WindowsJobError(operation, 5)

    def close(handle: int) -> None:
        nonlocal fail_before_release
        close_calls.append(handle)
        if fail_before_release:
            reason = "job-close-cleanup"
            raise OSError(reason)
        real_close(handle)

    monkeypatch.setattr(windows_start, "configure_kill_on_close", configure)
    monkeypatch.setattr(windows_start, "close_handle", close)

    with pytest.raises(ResourceCleanupError) as caught:
        _ = windows_job.WindowsJobProcess.start(
            [sys.executable, "-c", "raise AssertionError('must not run')"],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    message = str(caught.value)
    assert "SetInformationJobObject failed: winerror=5" in message
    assert "job-close-cleanup" in message
    assert tuple((owner.key, owner.label) for owner in caught.value.pending_ownership) == (
        ("job", "CloseHandle(job)"),
    )
    assert len(close_calls) == 1

    fail_before_release = False
    caught.value.retry_cleanup()

    assert caught.value.pending_ownership == ()
    assert len(close_calls) == 2


def test_construction_persistent_cleanup_failure_is_bounded_and_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = windows_start.close_handle
    close_calls: list[int] = []
    fail_before_release = True

    def configure(_job_handle: int) -> NoReturn:
        operation = "SetInformationJobObject"
        raise WindowsJobError(operation, 5)

    def close(handle: int) -> None:
        close_calls.append(handle)
        if fail_before_release:
            reason = "job-close-persistent"
            raise OSError(reason)
        real_close(handle)

    monkeypatch.setattr(windows_start, "configure_kill_on_close", configure)
    monkeypatch.setattr(windows_start, "close_handle", close)

    with pytest.raises(ResourceCleanupError) as caught:
        _ = windows_job.WindowsJobProcess.start(
            [sys.executable, "-c", "raise AssertionError('must not run')"],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    error = caught.value
    assert len(close_calls) == 1
    for expected_calls in (2, 3):
        with pytest.raises(ResourceCleanupError) as retried:
            error.retry_cleanup()
        error = retried.value
        assert isinstance(error.primary, WindowsJobError)
        assert "SetInformationJobObject failed: winerror=5" in str(error)
        assert "job-close-persistent" in str(error)
        assert tuple(owner.key for owner in error.pending_ownership) == ("job",)
        assert len(close_calls) == expected_calls

    fail_before_release = False
    error.retry_cleanup()
    assert error.pending_ownership == ()
    assert len(close_calls) == 4
