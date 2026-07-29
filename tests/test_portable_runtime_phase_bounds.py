from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests import portable_runtime_native_support as native_support
from tests.portable_runtime_native_process import NativeProbeError, run_bounded_process
from tests.portable_runtime_native_support import ProbeTimeouts, clean_native_mcp_probe
from tests.portable_runtime_phase_doubles import ProcessDouble, ThreadDouble
from tests.test_portable_runtime import ROOT, JsonObject

if TYPE_CHECKING:
    import queue
    from collections.abc import Sequence

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object phase bounds run only on Windows",
)

REQUESTS: tuple[JsonObject, JsonObject, JsonObject] = (
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "phase-stall", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
)


@pytest.mark.parametrize(
    "seam",
    ["stdout-construct", "stdout-start", "stderr-construct", "stderr-start", "stdin-write"],
)
def test_protocol_acquisition_or_send_failure_releases_every_owned_resource(
    seam: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = ProcessDouble()
    readers: list[ThreadDouble] = []

    def provision(
        _args: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
        phase: str,
        data_root: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        _ = cwd, environment, timeout_seconds, phase, data_root
        return subprocess.CompletedProcess(
            ["provision"],
            0,
            stdout=b"portable-runtime-ready\n",
            stderr=b"",
        )

    def start_process(
        _args: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> ProcessDouble:
        _ = cwd, environment
        return process

    def stdout_reader(
        _process: ProcessDouble,
        _lines: queue.Queue[bytes],
    ) -> ThreadDouble:
        if seam == "stdout-construct":
            reason = "stdout-construct-injected"
            raise RuntimeError(reason)
        thread = ThreadDouble(name="stdout", fail_start=seam == "stdout-start")
        readers.append(thread)
        return thread

    def stderr_reader(
        _process: ProcessDouble,
        _chunks: list[bytes],
    ) -> ThreadDouble:
        if seam == "stderr-construct":
            reason = "stderr-construct-injected"
            raise RuntimeError(reason)
        thread = ThreadDouble(name="stderr", fail_start=seam == "stderr-start")
        readers.append(thread)
        return thread

    def send(_process: ProcessDouble, _request: JsonObject) -> NoReturn:
        reason = "stdin-write-injected"
        raise RuntimeError(reason)

    monkeypatch.setattr(native_support, "run_bounded_process", provision)
    monkeypatch.setattr(native_support, "start_process", start_process)
    monkeypatch.setattr(native_support, "_stdout_reader", stdout_reader)
    monkeypatch.setattr(native_support, "_stderr_reader", stderr_reader)
    if seam == "stdin-write":
        monkeypatch.setattr(native_support, "_send", send)

    with pytest.raises(RuntimeError, match="injected"):
        _ = clean_native_mcp_probe(
            ROOT / "unused-launcher",
            "unused-bootstrap",
            cwd=ROOT,
            environment={},
            data_root=tmp_path,
            requests=REQUESTS,
        )

    assert process.close_calls == 1
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(reader.join_calls == (1 if reader.ident is not None else 0) for reader in readers)


def test_provision_stall_is_bounded_with_exact_diagnostic(tmp_path: Path) -> None:
    with pytest.raises(NativeProbeError) as caught:
        _ = run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time;sys.stderr.write('phase-stall');"
                    "sys.stderr.flush();time.sleep(30)"
                ),
            ],
            cwd=ROOT,
            environment=os.environ.copy(),
            phase="provision",
            data_root=tmp_path,
            timeout_seconds=0.1,
        )

    diagnostic = str(caught.value)
    assert "phase=provision" in diagnostic
    assert "pid=" in diagnostic
    assert "stderr='phase-stall'" in diagnostic
    assert "cleanup='job_terminated=True job_closed=True'" in diagnostic


def test_provision_cleanup_never_invokes_numeric_process_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_numeric_cleanup(*_args: str, **_kwargs: str) -> NoReturn:
        pytest.fail("numeric process cleanup invoked")

    monkeypatch.setattr(subprocess, "run", reject_numeric_cleanup)

    with pytest.raises(NativeProbeError, match="phase=provision"):
        _ = run_bounded_process(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            cwd=ROOT,
            environment=os.environ.copy(),
            phase="provision",
            data_root=tmp_path,
            timeout_seconds=0.1,
        )


@pytest.mark.parametrize("phase", ["initialize", "tools_list", "shutdown"])
def test_protocol_phase_stall_is_bounded_and_releases_tree_and_pipes(
    phase: str,
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["CMW_TEST_STALL_PHASE"] = phase

    with pytest.raises(NativeProbeError) as caught:
        _ = clean_native_mcp_probe(
            Path(sys.executable),
            str(ROOT / "tests/native_mcp_phase_stall_fixture.py"),
            cwd=ROOT,
            environment=environment,
            data_root=tmp_path,
            requests=REQUESTS,
            timeouts=ProbeTimeouts(
                provision=2,
                protocol=0.1,
                shutdown=0.1,
            ),
        )

    message = str(caught.value)
    assert f"phase={phase}" in message
    assert f"stderr='stall:{phase}'" in message
    assert "cleanup='job_terminated=True job_closed=True'" in message
    assert "returncode=" in message
    assert "elapsed=" in message
    assert "lock_exists=" in message
    assert "stages=" in message
    assert not any(thread.name.startswith("cmw-native-") for thread in threading.enumerate())
