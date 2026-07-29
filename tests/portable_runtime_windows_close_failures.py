from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal, NoReturn, assert_never, final, override

from tests import portable_runtime_windows_job as windows_job

if TYPE_CHECKING:
    import pytest

type CloseSeam = Literal[
    "terminate",
    "wait",
    "stdin",
    "stdout",
    "stderr",
    "wait_api",
    "exit_code",
]


@final
class ControlledClose(io.BytesIO):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message: str = message
        self.name: int = -1
        self.fail_before_release: bool = True
        self.close_calls: int = 0

    @override
    def close(self) -> None:
        self.close_calls += 1
        if self.fail_before_release:
            raise OSError(self._message)
        super().close()


def inject_close_failure(
    managed: windows_job.WindowsJobProcess,
    seam: CloseSeam,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match seam:
        case "stdin" | "stdout" | "stderr":
            _inject_stream_failure(managed, seam)
        case "wait_api" | "exit_code":
            _inject_api_failure(seam, monkeypatch)
        case "terminate":

            def terminate(_job_handle: int) -> NoReturn:
                reason = "terminate-injected"
                raise OSError(reason)

            monkeypatch.setattr(windows_job, "terminate_job", terminate)
        case "wait":

            def wait(
                _process: windows_job.WindowsJobProcess,
                _timeout: float,
            ) -> NoReturn:
                reason = "wait-injected"
                raise OSError(reason)

            monkeypatch.setattr(windows_job.WindowsJobProcess, "wait", wait)
        case _:
            assert_never(seam)


def _inject_api_failure(
    seam: Literal["wait_api", "exit_code"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def injected(*_arguments: int) -> NoReturn:
        reason = f"{seam}-injected"
        raise OSError(reason)

    target = "wait_for_process" if seam == "wait_api" else "get_process_exit_code"
    monkeypatch.setattr(windows_job, target, injected)


def _inject_stream_failure(
    managed: windows_job.WindowsJobProcess,
    seam: Literal["stdin", "stdout", "stderr"],
) -> None:
    match seam:
        case "stdin":
            managed.stdin.close()
            managed.stdin = ControlledClose("stdin-injected")
        case "stdout":
            managed.stdout.close()
            managed.stdout = ControlledClose("stdout-injected")
        case "stderr":
            managed.stderr.close()
            managed.stderr = ControlledClose("stderr-injected")
        case _:
            assert_never(seam)
