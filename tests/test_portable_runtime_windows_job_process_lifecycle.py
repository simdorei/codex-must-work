from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests import portable_runtime_windows_job as windows_job
from tests import portable_runtime_windows_start as windows_start
from tests.portable_runtime_windows_api import WindowsJobError
from tests.portable_runtime_windows_ownership import (
    ProcessLifecycleOwner,
    ResourceCleanupError,
    ResourceLedger,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object process lifecycle runs only on Windows",
)


@pytest.mark.parametrize("termination_failures", [1, 3])
def test_unassigned_suspended_child_retains_same_process_owner_until_terminated(
    termination_failures: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-ran"
    sentinel = tmp_path / "unrelated-sentinel"
    _ = sentinel.write_text("keep", encoding="utf-8")
    real_create_process = windows_start.winapi.CreateProcess
    real_close = windows_start.close_handle
    real_terminate = windows_start.terminate_process
    process_handles: list[int] = []
    terminate_calls: list[int] = []
    closed_handles: list[int] = []

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
        process_handles.append(result[0])
        return result

    def reject_assignment(_job_handle: int, _process_handle: int) -> NoReturn:
        operation = "AssignProcessToJobObject"
        raise WindowsJobError(operation, 5)

    def terminate(handle: int) -> None:
        terminate_calls.append(handle)
        if len(terminate_calls) <= termination_failures:
            reason = "TerminateProcess-injected"
            raise OSError(reason)
        real_terminate(handle)

    def close(handle: int) -> None:
        closed_handles.append(handle)
        real_close(handle)

    monkeypatch.setattr(windows_start.winapi, "CreateProcess", create_process)
    monkeypatch.setattr(windows_start, "assign_process_to_job", reject_assignment)
    monkeypatch.setattr(windows_start, "terminate_process", terminate)
    monkeypatch.setattr(windows_start, "close_handle", close)

    with pytest.raises(ResourceCleanupError) as caught:
        _ = windows_job.WindowsJobProcess.start(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')",
            ],
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    error = caught.value
    process_handle = process_handles[0]
    assert process_handle not in closed_handles
    assert terminate_calls == [process_handle]
    assert tuple(owner.key for owner in error.pending_ownership) == ("process-lifecycle",)

    for _ in range(termination_failures - 1):
        with pytest.raises(ResourceCleanupError) as retried:
            error.retry_cleanup()
        error = retried.value
        assert process_handle not in closed_handles
        assert terminate_calls[-1] == process_handle

    error.retry_cleanup()
    time.sleep(0.05)
    assert terminate_calls == [process_handle] * (termination_failures + 1)
    assert closed_handles.count(process_handle) == 1
    assert error.pending_ownership == ()
    assert not marker.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_process_lifecycle_retries_only_close_after_termination_succeeds() -> None:
    terminate_calls: list[int] = []
    close_calls: list[int] = []

    def terminate(handle: int) -> None:
        terminate_calls.append(handle)

    def close(handle: int) -> None:
        close_calls.append(handle)
        if len(close_calls) == 1:
            reason = "process-close-injected"
            raise OSError(reason)

    ledger = ResourceLedger()
    ledger.register_process_lifecycle(
        "process-lifecycle",
        ProcessLifecycleOwner(314, terminate, close),
    )

    failures = ledger.cleanup()

    assert [failure.render() for failure in failures] == [
        "CloseHandle(terminated-process): process-close-injected"
    ]
    assert terminate_calls == [314]
    assert close_calls == [314]
    assert tuple(owner.label for owner in ledger.pending_ownership()) == (
        "CloseHandle(terminated-process)",
    )

    assert ledger.cleanup() == ()
    assert terminate_calls == [314]
    assert close_calls == [314, 314]
    assert ledger.pending_ownership() == ()
