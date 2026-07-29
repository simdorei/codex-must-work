from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests import portable_runtime_native_process
from tests import portable_runtime_windows_job as windows_job
from tests import portable_runtime_windows_ownership as windows_ownership
from tests import portable_runtime_windows_start as windows_start
from tests.portable_runtime_windows_api import WindowsJobError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object behavior runs only on Windows",
)


def test_cleanup_source_has_no_numeric_process_command_path() -> None:
    sources = (
        inspect.getsource(portable_runtime_native_process),
        inspect.getsource(windows_job),
        inspect.getsource(windows_start),
        inspect.getsource(windows_ownership),
    )

    forbidden_command = bytes.fromhex("7461736b6b696c6c").decode()
    assert forbidden_command not in "".join(sources).casefold()
    assert "subprocess.run" not in "".join(sources)


@pytest.mark.parametrize(
    "operation",
    ["CreateJobObjectW", "SetInformationJobObject"],
)
def test_job_setup_failure_occurs_before_child_protocol(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-started"

    def reject_job_setup(*_handles: int) -> NoReturn:
        raise WindowsJobError(operation, 5)

    monkeypatch.setattr(
        windows_start,
        "create_job" if operation == "CreateJobObjectW" else "configure_kill_on_close",
        reject_job_setup,
    )

    with pytest.raises(
        WindowsJobError,
        match=rf"{operation} failed: winerror=5",
    ):
        _ = windows_job.WindowsJobProcess.start(
            _marker_command(marker),
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    assert not marker.exists()


def test_job_assignment_failure_cannot_run_suspended_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-started"

    def reject_assignment(_job_handle: int, _process_handle: int) -> NoReturn:
        operation = "AssignProcessToJobObject"
        raise WindowsJobError(operation, 5)

    monkeypatch.setattr(
        windows_start,
        "assign_process_to_job",
        reject_assignment,
    )

    with pytest.raises(
        WindowsJobError,
        match=r"AssignProcessToJobObject failed: winerror=5",
    ):
        _ = windows_job.WindowsJobProcess.start(
            _marker_command(marker),
            cwd=tmp_path,
            environment=os.environ.copy(),
        )

    time.sleep(0.05)
    assert not marker.exists()


def test_job_cleanup_kills_launcher_and_descendant_but_not_sentinel(
    tmp_path: Path,
) -> None:
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
    )
    managed = windows_job.WindowsJobProcess.start(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)']);"
                "print('ready',flush=True);time.sleep(30)"
            ),
        ],
        cwd=tmp_path,
        environment=os.environ.copy(),
    )
    try:
        assert managed.stdout.readline() == b"ready\r\n"

        first = managed.terminate_owned_job()
        assert managed.wait(2) != 0
        assert managed.stdout.read() == b""
        assert sentinel.poll() is None
        assert first.render() == "job_terminated=True job_closed=True"
        assert managed.terminate_owned_job().render() == "job_terminated=False job_closed=False"

        managed.close()
        managed.close()
    finally:
        managed.close()
        sentinel.terminate()
        _ = sentinel.wait(timeout=5)


def _marker_command(marker: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        (f"from pathlib import Path;Path({str(marker)!r}).write_text('started',encoding='utf-8')"),
    ]
