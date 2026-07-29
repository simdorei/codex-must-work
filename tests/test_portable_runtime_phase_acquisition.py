from __future__ import annotations

import os
from typing import TYPE_CHECKING, BinaryIO

import pytest

from tests import portable_runtime_native_process as native_process
from tests.portable_runtime_native_process import run_bounded_process
from tests.portable_runtime_phase_doubles import ProcessDouble, ThreadDouble
from tests.portable_runtime_windows_ownership import ResourceCleanupError
from tests.test_portable_runtime import ROOT

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object phase acquisition runs only on Windows",
)


@pytest.mark.parametrize(
    "seam",
    ["stdout-construct", "stdout-start", "stderr-construct", "stderr-start", "stdin-close"],
)
def test_each_acquisition_failure_releases_process_readers_and_pipes(
    seam: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ProcessDouble(stdin_close_failure=seam == "stdin-close")
    threads: list[ThreadDouble] = []
    reader_calls = 0

    def start_process(
        _args: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> ProcessDouble:
        _ = cwd, environment
        return process

    def reader(
        _stream: BinaryIO,
        _chunks: list[bytes],
        _name: str,
    ) -> ThreadDouble:
        nonlocal reader_calls
        reader_calls += 1
        if seam == "stdout-construct" and reader_calls == 1:
            reason = "stdout-reader-construct"
            raise RuntimeError(reason)
        if seam == "stderr-construct" and reader_calls == 2:
            reason = "stderr-reader-construct"
            raise RuntimeError(reason)
        thread = ThreadDouble(
            fail_start=(seam == "stdout-start" and reader_calls == 1)
            or (seam == "stderr-start" and reader_calls == 2)
        )
        threads.append(thread)
        return thread

    monkeypatch.setattr(native_process, "start_process", start_process)
    monkeypatch.setattr(native_process, "_reader_thread", reader)

    with pytest.raises((OSError, RuntimeError)):
        _ = run_bounded_process(
            ["ignored"],
            cwd=ROOT,
            environment={},
            phase="acquire",
            timeout_seconds=1,
        )

    assert process.close_calls == 1
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(thread.join_calls == (1 if thread.ident is not None else 0) for thread in threads)


def test_primary_acquisition_error_keeps_process_and_reader_cleanup_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ProcessDouble(stdin_close_failure=True, close_failure=True)
    threads = [ThreadDouble(fail_join=True), ThreadDouble()]

    def start_process(
        _args: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> ProcessDouble:
        _ = cwd, environment
        return process

    def reader(
        _stream: BinaryIO,
        _chunks: list[bytes],
        _name: str,
    ) -> ThreadDouble:
        return threads.pop(0)

    monkeypatch.setattr(native_process, "start_process", start_process)
    monkeypatch.setattr(native_process, "_reader_thread", reader)

    with pytest.raises(ResourceCleanupError) as caught:
        _ = run_bounded_process(
            ["ignored"],
            cwd=ROOT,
            environment={},
            phase="acquire",
            timeout_seconds=1,
        )

    message = str(caught.value)
    assert message.startswith("stdin-close-injected; cleanup failures:")
    assert "process-close-injected" in message
    assert "reader-join-injected" in message
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
