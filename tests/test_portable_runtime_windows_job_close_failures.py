from __future__ import annotations

import os
import subprocess
import sys
from typing import (
    TYPE_CHECKING,
    Literal,
    NoReturn,
    cast,
)

import pytest

from tests import portable_runtime_windows_job as windows_job
from tests.portable_runtime_windows_close_failures import (
    CloseSeam,
    ControlledClose,
    inject_close_failure,
)
from tests.portable_runtime_windows_ownership import ResourceCleanupError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object exceptional close runs only on Windows",
)


@pytest.mark.parametrize(
    "seam",
    [
        "terminate",
        "wait",
        "stdin",
        "stdout",
        "stderr",
        "wait_api",
        "exit_code",
    ],
)
def test_each_close_failure_clears_every_resource_and_preserves_sentinel(
    seam: CloseSeam,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
    )
    command = (
        [sys.executable, "-c", "pass"]
        if seam == "exit_code"
        else [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    managed = windows_job.WindowsJobProcess.start(
        command,
        cwd=tmp_path,
        environment=os.environ.copy(),
    )
    if seam == "exit_code":
        state = managed.resource_state()
        _ = windows_job.wait_for_process(state.process_handle, 5_000)
    inject_close_failure(managed, seam, monkeypatch)

    try:
        expected_error: type[OSError | ResourceCleanupError]
        expected_error = ResourceCleanupError if seam in {"wait_api", "exit_code"} else OSError
        with pytest.raises(expected_error, match=rf"{seam}-injected"):
            managed.close()

        state = managed.resource_state()
        if seam in {"stdin", "stdout", "stderr"}:
            stream = cast("ControlledClose", getattr(managed, seam))
            assert stream.close_calls == 1
            assert state.closed is False
            assert state.streams_closed != (True, True, True)
            stream.fail_before_release = False
            managed.close()
            assert stream.close_calls == 2
        else:
            assert state.closed is True
        state = managed.resource_state()
        assert state.streams_closed == (True, True, True)
        assert state.process_handle == 0
        assert state.job_handle == 0
        assert state.closed is True
        assert sentinel.poll() is None
        managed.close()
    finally:
        sentinel.terminate()
        _ = sentinel.wait(timeout=5)


def test_close_aggregates_cleanup_errors_after_first_operational_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = windows_job.WindowsJobProcess.start(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        environment=os.environ.copy(),
    )

    def terminate(_job_handle: int) -> NoReturn:
        reason = "terminate-primary"
        raise OSError(reason)

    monkeypatch.setattr(windows_job, "terminate_job", terminate)
    managed.stderr.close()
    managed.stderr = ControlledClose("stderr-cleanup")

    with pytest.raises(ResourceCleanupError) as caught:
        managed.close()

    message = str(caught.value)
    assert "terminate-primary" in message
    assert "stderr-cleanup" in message
    state = managed.resource_state()
    assert state.streams_closed == (True, True, False)
    assert state.process_handle == 0
    assert state.job_handle == 0
    assert state.closed is False
    managed.stderr.fail_before_release = False
    managed.close()
    assert managed.resource_state().closed is True


@pytest.mark.parametrize("seam", ["process_handle", "job_handle"])
def test_handle_close_failure_before_release_retains_and_retries_exact_owner(
    seam: Literal["process_handle", "job_handle"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
    )
    managed = windows_job.WindowsJobProcess.start(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        environment=os.environ.copy(),
    )
    initial = managed.resource_state()
    target = initial.process_handle if seam == "process_handle" else initial.job_handle
    real_close_handle = windows_job.close_handle
    calls: list[int] = []
    fail_before_release = True

    def close_handle(handle: int) -> None:
        nonlocal fail_before_release
        if handle == target:
            calls.append(handle)
            if fail_before_release:
                reason = f"{seam}-injected"
                raise OSError(reason)
        real_close_handle(handle)

    monkeypatch.setattr(windows_job, "close_handle", close_handle)

    try:
        with pytest.raises(OSError, match=rf"{seam}-injected"):
            managed.close()

        failed = managed.resource_state()
        assert failed.closed is False
        assert calls == [target]
        if seam == "process_handle":
            assert failed.process_handle == target
            assert failed.job_handle == 0
        else:
            assert failed.process_handle == 0
            assert failed.job_handle == target
        assert sentinel.poll() is None

        fail_before_release = False
        managed.close()

        assert calls == [target, target]
        completed = managed.resource_state()
        assert completed.closed is True
        assert completed.process_handle == 0
        assert completed.job_handle == 0
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        _ = sentinel.wait(timeout=5)


@pytest.mark.parametrize("seam", ["process_handle", "job_handle"])
def test_persistent_handle_close_failure_is_bounded_and_visible_on_every_call(
    seam: Literal["process_handle", "job_handle"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = windows_job.WindowsJobProcess.start(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        environment=os.environ.copy(),
    )
    initial = managed.resource_state()
    target = initial.process_handle if seam == "process_handle" else initial.job_handle
    real_close_handle = windows_job.close_handle
    calls = 0
    fail_before_release = True

    def close_handle(handle: int) -> None:
        nonlocal calls
        if handle == target:
            calls += 1
            if fail_before_release:
                reason = f"{seam}-persistent"
                raise OSError(reason)
        real_close_handle(handle)

    monkeypatch.setattr(windows_job, "close_handle", close_handle)

    for expected_calls in (1, 2, 3):
        with pytest.raises(OSError, match=rf"{seam}-persistent"):
            managed.close()
        state = managed.resource_state()
        assert state.closed is False
        assert calls == expected_calls
        retained = state.process_handle if seam == "process_handle" else state.job_handle
        assert retained == target

    fail_before_release = False
    managed.close()
    assert calls == 4
    assert managed.resource_state().closed is True
