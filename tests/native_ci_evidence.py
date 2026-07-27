# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python tests/native_ci_evidence.py --run-id ID --expected-sha SHA \
#   --required-job ubuntu-x64 --required-job macos-arm64 \
#   --required-marker first_install --output evidence.json
"""Collect redacted evidence for one exact native installer GitHub Actions run."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.native_ci_evidence_models import (
    EvidenceError,
    RunEvidence,
    RunExpectation,
    parse_run_metadata,
)
from tests.native_ci_evidence_output import write_summary
from tests.native_ci_evidence_process import GhInvocation, run_gh_spooled

if TYPE_CHECKING:
    from tests.native_ci_evidence_json import JsonValue

_SHA: Final = re.compile(r"[0-9a-f]{40}")
_PUBLIC_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_METADATA_FIELDS: Final = "databaseId,headSha,event,status,conclusion,createdAt,jobs"
_MAX_METADATA_BYTES: Final = 2_000_000
_MAX_LOG_BYTES: Final = 16_000_000
_MARKER_MISSING: Final = "marker_missing"
_GH_UNAVAILABLE: Final = "gh_unavailable"
_MAX_STDERR_BYTES: Final = 2_000_000
_REQUIRED_MARKERS: Final = (
    "first_install",
    "unsafe_runtime_rejected",
    "serialized_reinstall",
    "no_write_reinstall",
    "executable_mode",
    "capability_key_metadata",
    "mcp_import",
)


@final
class _CliNamespace:
    """Mutable argparse destination converted immediately to immutable values."""

    __slots__ = (
        "expected_sha",
        "gh_timeout_seconds",
        "output",
        "pushed_at",
        "required_job",
        "required_marker",
        "run_id",
    )

    run_id: str
    expected_sha: str
    required_job: list[str]
    required_marker: list[str]
    output: Path
    pushed_at: str
    gh_timeout_seconds: float

    def __init__(self) -> None:
        self.run_id = ""
        self.expected_sha = ""
        self.required_job = []
        self.required_marker = []
        self.output = Path()
        self.pushed_at = ""
        self.gh_timeout_seconds = 30.0


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    run_id: int
    expected_sha: str
    required_jobs: tuple[str, ...]
    required_markers: tuple[str, ...]
    output: Path
    timeout_seconds: float
    pushed_at: str


def _arguments(argv: list[str]) -> EvidenceRequest:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--run-id", required=True)
    _ = parser.add_argument("--expected-sha", required=True)
    _ = parser.add_argument("--required-job", action="append", required=True)
    _ = parser.add_argument("--required-marker", action="append", required=True)
    _ = parser.add_argument("--output", required=True, type=Path)
    _ = parser.add_argument("--pushed-at", required=True)
    _ = parser.add_argument("--gh-timeout-seconds", type=float, default=30.0)
    parsed = parser.parse_args(argv, namespace=_CliNamespace())
    try:
        run_id = int(parsed.run_id)
    except ValueError:
        parser.error("--run-id must be a positive integer")
    jobs = tuple(parsed.required_job)
    markers = tuple(parsed.required_marker)
    if run_id < 1:
        parser.error("--run-id must be a positive integer")
    if _SHA.fullmatch(parsed.expected_sha) is None:
        parser.error("--expected-sha must be a full lowercase SHA")
    if len(jobs) != len(set(jobs)) or frozenset(jobs) != frozenset(("ubuntu-x64", "macos-arm64")):
        parser.error("required jobs must be ubuntu-x64 and macos-arm64 exactly once")
    if (
        len(markers) != len(set(markers))
        or frozenset(markers) != frozenset(_REQUIRED_MARKERS)
        or any(_PUBLIC_NAME.fullmatch(marker) is None for marker in markers)
    ):
        parser.error("all seven release markers are required exactly once")
    if not 0.05 <= parsed.gh_timeout_seconds <= 30.0:
        parser.error("--gh-timeout-seconds must be between 0.05 and 30")
    return EvidenceRequest(
        run_id,
        parsed.expected_sha,
        jobs,
        markers,
        parsed.output,
        parsed.gh_timeout_seconds,
        parsed.pushed_at,
    )


def _markers(
    log: str,
    job_name: str,
    required: tuple[str, ...],
) -> tuple[str, ...]:
    lines = log.splitlines()
    for marker in required:
        expected = (job_name, "Run native installer smoke", f"{marker}=true")
        matches = tuple(line for line in lines if tuple(line.split("\t", maxsplit=2)) == expected)
        if len(matches) != 1:
            raise EvidenceError(_MARKER_MISSING)
    return required


def _summary(run: RunEvidence, marker_rows: tuple[tuple[str, ...], ...]) -> dict[str, JsonValue]:
    jobs: list[JsonValue] = [
        {"name": job.name, "conclusion": job.conclusion, "markers": list(markers)}
        for job, markers in zip(run.jobs, marker_rows, strict=True)
    ]
    return {
        "run_id": run.run_id,
        "head_sha": run.head_sha,
        "event": run.event,
        "status": run.status,
        "conclusion": run.conclusion,
        "jobs": jobs,
    }


def collect(request: EvidenceRequest) -> dict[str, JsonValue]:
    """Collect one exact run and return only validated public fields."""
    resolved = shutil.which("gh")
    if resolved is None:
        raise EvidenceError(_GH_UNAVAILABLE)
    gh = Path(resolved).absolute()
    metadata = run_gh_spooled(
        GhInvocation(
            gh,
            ("run", "view", str(request.run_id), "--json", _METADATA_FIELDS),
            request.timeout_seconds,
            _MAX_METADATA_BYTES,
            _MAX_STDERR_BYTES,
        )
    )
    run = parse_run_metadata(
        metadata,
        RunExpectation(
            request.run_id,
            request.expected_sha,
            request.required_jobs,
            request.pushed_at,
        ),
    )
    marker_rows = tuple(
        _markers(
            run_gh_spooled(
                GhInvocation(
                    gh,
                    (
                        "run",
                        "view",
                        str(request.run_id),
                        "--job",
                        str(job.database_id),
                        "--log",
                    ),
                    request.timeout_seconds,
                    _MAX_LOG_BYTES,
                    _MAX_STDERR_BYTES,
                )
            ),
            job.name,
            request.required_markers,
        )
        for job in run.jobs
    )
    return _summary(run, marker_rows)


def main(argv: list[str] | None = None) -> int:
    try:
        request = _arguments(sys.argv[1:] if argv is None else argv)
        summary = collect(request)
        write_summary(request.output, summary)
    except EvidenceError as error:
        _ = sys.stdout.write(f"native_ci_evidence=failed\nreason={error.reason}\n")
        return 1
    _ = sys.stdout.write("native_ci_evidence=ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
