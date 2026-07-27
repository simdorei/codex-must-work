"""Parse GitHub run metadata into public, validated native-CI evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, final, override

from tests.native_ci_evidence_json import JsonInputError, JsonValue, load_json

_REQUIRED_STEPS: Final = ("Verify candidate SHA", "Run native installer smoke")
_METADATA_MALFORMED: Final = "metadata_malformed"
_RUN_IDENTITY: Final = "run_identity"
_CANDIDATE_IDENTITY: Final = "candidate_identity"
_RUN_STATE: Final = "run_state"
_JOB_IDENTITY: Final = "job_identity"
_JOB_STATE: Final = "job_state"
_STEP_STATE: Final = "step_state"
_RUN_FRESHNESS: Final = "run_freshness"


@final
class EvidenceError(RuntimeError):
    """Expose one stable public reason without retaining private gh output."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class JobEvidence:
    """Public identity and conclusion for one required native job."""

    name: str
    database_id: int
    conclusion: str


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """Public identity and state for one exact successful push run."""

    run_id: int
    head_sha: str
    event: str
    status: str
    conclusion: str
    jobs: tuple[JobEvidence, ...]


@dataclass(frozen=True, slots=True)
class RunExpectation:
    """Caller-bound run identity, freshness, and required jobs."""

    run_id: int
    expected_sha: str
    required_jobs: tuple[str, ...]
    pushed_at: str


def parse_run_metadata(
    source: str,
    expected: RunExpectation,
) -> RunEvidence:
    """Parse and authenticate the exact run, job, and required-step states."""
    raw = _parse_document(source)
    _validate_run(raw, expected)
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(expected.required_jobs):
        raise EvidenceError(_JOB_IDENTITY)
    rows = tuple(item for item in jobs if isinstance(item, dict))
    if len(rows) != len(jobs):
        raise EvidenceError(_JOB_IDENTITY)
    names = tuple(name for row in rows if isinstance(name := row.get("name"), str))
    if (
        len(names) != len(rows)
        or len(set(names)) != len(names)
        or frozenset(names) != frozenset(expected.required_jobs)
    ):
        raise EvidenceError(_JOB_IDENTITY)
    by_name = dict(zip(names, rows, strict=True))
    selected = tuple(_parse_job(by_name[name], name) for name in expected.required_jobs)
    if len({job.database_id for job in selected}) != len(selected):
        raise EvidenceError(_JOB_IDENTITY)
    return RunEvidence(
        run_id=expected.run_id,
        head_sha=expected.expected_sha,
        event="push",
        status="completed",
        conclusion="success",
        jobs=selected,
    )


def _parse_document(source: str) -> dict[str, JsonValue]:
    try:
        raw = load_json(source)
    except JsonInputError:
        raise EvidenceError(_METADATA_MALFORMED) from None
    if not isinstance(raw, dict):
        raise EvidenceError(_METADATA_MALFORMED)
    return raw


def _validate_run(raw: dict[str, JsonValue], expected: RunExpectation) -> None:
    database_id = raw.get("databaseId")
    if (
        not isinstance(database_id, int)
        or isinstance(database_id, bool)
        or database_id != expected.run_id
    ):
        raise EvidenceError(_RUN_IDENTITY)
    head_sha = raw.get("headSha")
    event = raw.get("event")
    if head_sha != expected.expected_sha or event != "push":
        raise EvidenceError(_CANDIDATE_IDENTITY)
    created_at = raw.get("createdAt")
    if not isinstance(created_at, str) or _timestamp(created_at) < _timestamp(expected.pushed_at):
        raise EvidenceError(_RUN_FRESHNESS)
    status = raw.get("status")
    conclusion = raw.get("conclusion")
    if status != "completed" or conclusion != "success":
        raise EvidenceError(_RUN_STATE)


def _parse_job(raw: dict[str, JsonValue], expected_name: str) -> JobEvidence:
    database_id = raw.get("databaseId")
    if (
        raw.get("name") != expected_name
        or not isinstance(database_id, int)
        or isinstance(database_id, bool)
        or database_id < 1
    ):
        raise EvidenceError(_JOB_IDENTITY)
    if raw.get("status") != "completed" or raw.get("conclusion") != "success":
        raise EvidenceError(_JOB_STATE)
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise EvidenceError(_STEP_STATE)
    for required_name in _REQUIRED_STEPS:
        matches = tuple(
            item for item in steps if isinstance(item, dict) and item.get("name") == required_name
        )
        if len(matches) != 1:
            raise EvidenceError(_STEP_STATE)
        step = matches[0]
        if step.get("status") != "completed" or step.get("conclusion") != "success":
            raise EvidenceError(_STEP_STATE)
    return JobEvidence(expected_name, database_id, "success")


def _timestamp(source: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError:
        raise EvidenceError(_RUN_FRESHNESS) from None
    if parsed.tzinfo is None:
        raise EvidenceError(_RUN_FRESHNESS)
    return parsed
