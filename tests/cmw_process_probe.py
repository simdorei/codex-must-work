#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# ─── How to run ───
# 1. Install uv: https://docs.astral.sh/uv/
# 2. Run: uv run python tests/cmw_process_probe.py --help
# 3. Supply the exact resident PID, rollout identity, limits, and a new absolute JSON output.
# ──────────────────
"""Audit one resident CMW daemon with authenticated controls and native tracing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.cmw_process_probe_endpoint import (
    EndpointAttachError,
    EndpointClient,
    load_control_endpoint,
)
from tests.cmw_process_probe_etw_session import EtwSessionError
from tests.cmw_process_probe_io import (
    LocatorError,
    OutputPathError,
    load_session_locator,
    parse_output_path,
)
from tests.cmw_process_probe_live import LiveDependencies
from tests.cmw_process_probe_models import ProbeLimits
from tests.cmw_process_probe_runtime import (
    Lifecycle,
    ProbeExecutionError,
    ProbeRun,
    ProbeRuntime,
)
from tests.cmw_process_probe_sampler import ProcessSampleError


def main(argv: list[str] | None = None) -> int:
    """Parse the live contract, run once, and write only public evidence."""
    arguments = _parse_args(argv)
    try:
        output = parse_output_path(arguments.output)
        locator = load_session_locator(arguments.rollout, arguments.session_id)
        endpoint = load_control_endpoint(locator.plugin_data, arguments.daemon_pid)
        dependencies = LiveDependencies(
            arguments.daemon_pid,
            locator,
            EndpointClient(endpoint),
            arguments.duration_seconds,
            output.parent,
        )
        limits = ProbeLimits(
            max_cpu_seconds=arguments.max_cpu_seconds,
            max_handle_growth=arguments.max_handle_growth,
            max_thread_growth=arguments.max_thread_growth,
            max_heartbeat_gap_seconds=arguments.max_heartbeat_gap_seconds,
            max_descendant_starts=arguments.max_descendant_starts,
            max_wmi_operations=arguments.max_wmi_operations,
            require_zero_event_loss=arguments.require_zero_event_loss,
            expect_process_alive=(
                arguments.expect_process_alive or arguments.lifecycle is Lifecycle.START_STOP
            ),
        )
        result = ProbeRuntime(dependencies).run(
            ProbeRun(arguments.lifecycle, arguments.cycle_count, limits)
        )
        evidence = dependencies.public_evidence(result)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(evidence, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            _ = stream.write("\n")
    except (
        EndpointAttachError,
        EtwSessionError,
        LocatorError,
        OSError,
        OutputPathError,
        ProbeExecutionError,
        ProcessSampleError,
        ValueError,
    ) as error:
        _ = sys.stderr.write(f"{error}\n")
        return 2
    except KeyboardInterrupt:
        _ = sys.stderr.write("probe_interrupted\n")
        return 130
    return 0 if result.outcome == "passed" else 1


def _parse_args(argv: list[str] | None) -> _ProbeArgs:
    parser = argparse.ArgumentParser(prog="cmw-process-probe")
    _ = parser.add_argument("--daemon-pid", type=_positive_int, required=True)
    _ = parser.add_argument("--rollout", type=Path, required=True)
    _ = parser.add_argument("--session-id", required=True)
    _ = parser.add_argument("--lifecycle", type=Lifecycle, choices=tuple(Lifecycle), required=True)
    _ = parser.add_argument("--duration-seconds", type=_positive_float, required=True)
    _ = parser.add_argument("--cycle-count", type=_nonnegative_int, required=True)
    _ = parser.add_argument("--max-cpu-seconds", type=_nonnegative_float, required=True)
    _ = parser.add_argument("--max-handle-growth", type=_nonnegative_int, required=True)
    _ = parser.add_argument("--max-thread-growth", type=_nonnegative_int, required=True)
    _ = parser.add_argument(
        "--max-heartbeat-gap-seconds",
        type=_positive_float,
        required=True,
    )
    _ = parser.add_argument("--max-descendant-starts", type=_nonnegative_int, required=True)
    _ = parser.add_argument("--max-wmi-operations", type=_nonnegative_int, required=True)
    _ = parser.add_argument("--require-zero-event-loss", action="store_true")
    _ = parser.add_argument("--expect-process-alive", action="store_true")
    _ = parser.add_argument("--output", type=Path, required=True)
    namespace = _ProbeArgs()
    _ = parser.parse_args(argv, namespace=namespace)
    if not namespace.require_zero_event_loss:
        parser.error("--require-zero-event-loss is required")
    if namespace.lifecycle is Lifecycle.OBSERVE_INACTIVE and namespace.cycle_count != 0:
        parser.error("observe-inactive requires --cycle-count 0")
    return namespace


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        reason = "value must be positive"
        raise argparse.ArgumentTypeError(reason)
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        reason = "value must be nonnegative"
        raise argparse.ArgumentTypeError(reason)
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        reason = "value must be positive"
        raise argparse.ArgumentTypeError(reason)
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        reason = "value must be nonnegative"
        raise argparse.ArgumentTypeError(reason)
    return parsed


@final
class _ProbeArgs(argparse.Namespace):
    daemon_pid: int
    rollout: Path
    session_id: str
    lifecycle: Lifecycle
    duration_seconds: float
    cycle_count: int
    max_cpu_seconds: float
    max_handle_growth: int
    max_thread_growth: int
    max_heartbeat_gap_seconds: float
    max_descendant_starts: int
    max_wmi_operations: int
    require_zero_event_loss: bool
    expect_process_alive: bool
    output: Path

    def __init__(self) -> None:
        super().__init__()
        self.daemon_pid = 0
        self.rollout = Path()
        self.session_id = ""
        self.lifecycle = Lifecycle.START_STOP
        self.duration_seconds = 0.0
        self.cycle_count = 0
        self.max_cpu_seconds = 0.0
        self.max_handle_growth = 0
        self.max_thread_growth = 0
        self.max_heartbeat_gap_seconds = 0.0
        self.max_descendant_starts = 0
        self.max_wmi_operations = 0
        self.require_zero_event_loss = False
        self.expect_process_alive = False
        self.output = Path()


if __name__ == "__main__":
    raise SystemExit(main())
