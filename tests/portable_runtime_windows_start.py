"""Transactional suspended construction for one Windows Job process."""

from __future__ import annotations

import _winapi
import msvcrt
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from tests.portable_runtime_windows_api import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    WindowsJobError,
    assign_process_to_job,
    close_handle,
    configure_kill_on_close,
    create_job,
    resume_thread,
    terminate_process,
)
from tests.portable_runtime_windows_ownership import (
    ProcessLifecycleOwner,
    ResourceLedger,
    raise_with_cleanup,
)

if TYPE_CHECKING:
    from pathlib import Path

winapi = _winapi

__all__ = (
    "StartedWindowsJob",
    "assign_process_to_job",
    "close_handle",
    "configure_kill_on_close",
    "create_job",
    "os",
    "resume_thread",
    "start_windows_job",
    "terminate_process",
    "winapi",
)


@dataclass(frozen=True, slots=True)
class StartedWindowsJob:
    args: tuple[str, ...]
    pid: int
    process_handle: int
    job_handle: int
    stdin: BinaryIO
    stdout: BinaryIO
    stderr: BinaryIO


def start_windows_job(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> StartedWindowsJob:
    """Acquire every resource transactionally and transfer complete ownership."""
    ledger = ResourceLedger()
    try:
        job_handle = create_job()
        ledger.register("job", "CloseHandle(job)", close_handle, job_handle)
        configure_kill_on_close(job_handle)
        pipe_fds = _create_pipe_fds(ledger)
        child_fds = pipe_fds[0], pipe_fds[3], pipe_fds[5]
        startup = _startup_info(child_fds)
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | subprocess.CREATE_NEW_PROCESS_GROUP
        process_handle, thread_handle, pid, _thread_id = winapi.CreateProcess(
            None,
            subprocess.list2cmdline(command),
            None,
            None,
            True,  # noqa: FBT003
            flags,
            environment,
            str(cwd),
            startup,
        )
        ledger.register_process_lifecycle(
            "process-lifecycle",
            ProcessLifecycleOwner(
                process_handle,
                terminate_process,
                close_handle,
            ),
        )
        ledger.register(
            "thread",
            "CloseHandle(primary-thread)",
            close_handle,
            thread_handle,
        )
        assign_process_to_job(job_handle, process_handle)
        resume_thread(thread_handle)
        ledger.close_now("thread")
        for key in ("stdin-child", "stdout-child", "stderr-child"):
            ledger.close_now(key)
        stdin = os.fdopen(pipe_fds[1], "wb", buffering=0)
        ledger.replace("stdin-parent", "close(stdin)", stdin.close)
        stdout = os.fdopen(pipe_fds[2], "rb", buffering=0)
        ledger.replace("stdout-parent", "close(stdout)", stdout.close)
        stderr = os.fdopen(pipe_fds[4], "rb", buffering=0)
        ledger.replace("stderr-parent", "close(stderr)", stderr.close)
        started = StartedWindowsJob(
            tuple(command),
            pid,
            process_handle,
            job_handle,
            stdin,
            stdout,
            stderr,
        )
        for key in (
            "process-lifecycle",
            "job",
            "stdin-parent",
            "stdout-parent",
            "stderr-parent",
        ):
            ledger.discard(key)
    except (OSError, WindowsJobError) as primary:
        raise_with_cleanup(primary, ledger.cleanup(), ledger)
    else:
        return started


def _create_pipe_fds(
    ledger: ResourceLedger,
) -> tuple[int, int, int, int, int, int]:
    stdin_read, stdin_write = os.pipe()
    ledger.register("stdin-child", "close(stdin-child-fd)", os.close, stdin_read)
    ledger.register("stdin-parent", "close(stdin-parent-fd)", os.close, stdin_write)
    stdout_read, stdout_write = os.pipe()
    ledger.register("stdout-parent", "close(stdout-parent-fd)", os.close, stdout_read)
    ledger.register("stdout-child", "close(stdout-child-fd)", os.close, stdout_write)
    stderr_read, stderr_write = os.pipe()
    ledger.register("stderr-parent", "close(stderr-parent-fd)", os.close, stderr_read)
    ledger.register("stderr-child", "close(stderr-child-fd)", os.close, stderr_write)
    for fd in (stdin_read, stdout_write, stderr_write):
        os.set_inheritable(fd, True)  # noqa: FBT003
    return stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write


def _startup_info(child_fds: tuple[int, int, int]) -> subprocess.STARTUPINFO:
    handles = [msvcrt.get_osfhandle(fd) for fd in child_fds]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
    startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
    startup.lpAttributeList = {"handle_list": handles}
    return startup
