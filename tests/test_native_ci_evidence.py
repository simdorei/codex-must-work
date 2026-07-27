from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.native_ci_evidence_test_support import (
    MARKERS,
    SHA,
    FakeGh,
    invoke_fake_gh,
    job_fixture,
    load_json_document,
    metadata_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_cli_writes_only_validated_public_summary_for_exact_run(tmp_path: Path) -> None:
    # Given: one successful exact-SHA run with both required jobs and all markers.
    # When: the helper reads metadata and each job log through the fake gh executable.
    completed, output, wire = invoke_fake_gh(tmp_path)

    # Then: only a compact public result is emitted and all calls stay on run 731.
    assert completed.returncode == 0
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert completed.stderr == ""
    summary = load_json_document(output.read_text(encoding="utf-8"))
    assert summary == {
        "conclusion": "success",
        "event": "push",
        "head_sha": SHA,
        "jobs": [
            {"conclusion": "success", "markers": list(MARKERS), "name": "ubuntu-x64"},
            {"conclusion": "success", "markers": list(MARKERS), "name": "macos-arm64"},
        ],
        "run_id": 731,
        "status": "completed",
    }
    calls = [load_json_document(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3
    assert all(isinstance(call, list) and call[2] == "731" for call in calls)
    public_bytes = completed.stdout + completed.stderr + output.read_text(encoding="utf-8")
    assert "TOKEN" not in public_bytes
    assert "ghp_" not in public_bytes
    assert "Bearer" not in public_bytes


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("wrong_run", "run_identity"),
        ("wrong_sha", "candidate_identity"),
        ("wrong_event", "candidate_identity"),
        ("running", "run_state"),
        ("cancelled", "run_state"),
        ("duplicate_job", "job_identity"),
        ("missing_job", "job_identity"),
        ("failed_job", "job_state"),
        ("skipped_job", "job_state"),
        ("failed_step", "step_state"),
    ],
)
def test_cli_rejects_untrusted_metadata(
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    # Given: one metadata boundary violation.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    if mutation == "wrong_run":
        metadata["databaseId"] = 732
    elif mutation == "wrong_sha":
        metadata["headSha"] = "b" * 40
    elif mutation == "wrong_event":
        metadata["event"] = "workflow_dispatch"
    elif mutation == "running":
        metadata["status"] = "in_progress"
        metadata["conclusion"] = None
    elif mutation == "cancelled":
        metadata["conclusion"] = "cancelled"
    elif mutation == "duplicate_job":
        jobs.append(job_fixture("ubuntu-x64", 43))
    elif mutation == "missing_job":
        _ = jobs.pop()
    elif mutation in {"failed_job", "skipped_job"}:
        jobs[0] = job_fixture("ubuntu-x64", 41, mutation.removesuffix("_job"))
    elif mutation == "failed_step":
        job = jobs[0]
        assert isinstance(job, dict)
        steps = job["steps"]
        assert isinstance(steps, list)
        steps[0] = {
            "name": "Verify candidate SHA",
            "status": "completed",
            "conclusion": "failure",
        }

    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    assert completed.returncode == 1
    assert completed.stdout == f"native_ci_evidence=failed\nreason={expected_reason}\n"
    assert completed.stderr == ""
    assert not output.exists()
    assert SHA not in completed.stdout


def test_cli_rejects_run_created_before_recorded_push(tmp_path: Path) -> None:
    completed, output, _wire = invoke_fake_gh(
        tmp_path,
        FakeGh(pushed_at="2026-07-24T01:02:04Z"),
    )

    assert completed.returncode == 1
    assert completed.stdout == "native_ci_evidence=failed\nreason=run_freshness\n"
    assert completed.stderr == ""
    assert not output.exists()


def test_cli_requires_push_boundary_and_exact_release_markers(tmp_path: Path) -> None:
    missing_push, missing_push_output, missing_push_wire = invoke_fake_gh(
        tmp_path / "missing-push",
        FakeGh(pushed_at=None),
    )
    subset, subset_output, subset_wire = invoke_fake_gh(
        tmp_path / "marker-subset",
        FakeGh(required_markers=("first_install",)),
    )

    assert missing_push.returncode != 0
    assert subset.returncode != 0
    assert not missing_push_output.exists()
    assert not subset_output.exists()
    assert not missing_push_wire.exists()
    assert not subset_wire.exists()


def test_cli_rejects_extra_job_and_duplicate_job_database_id(tmp_path: Path) -> None:
    extra = metadata_fixture()
    extra_jobs = extra["jobs"]
    assert isinstance(extra_jobs, list)
    extra_jobs.append(job_fixture("unexpected", 43))
    duplicate_id = metadata_fixture()
    duplicate_jobs = duplicate_id["jobs"]
    assert isinstance(duplicate_jobs, list)
    second = duplicate_jobs[1]
    assert isinstance(second, dict)
    second["databaseId"] = 41

    extra_result, extra_output, _ = invoke_fake_gh(
        tmp_path / "extra",
        FakeGh(metadata=extra),
    )
    duplicate_result, duplicate_output, _ = invoke_fake_gh(
        tmp_path / "duplicate-id",
        FakeGh(metadata=duplicate_id),
    )

    assert extra_result.stdout == "native_ci_evidence=failed\nreason=job_identity\n"
    assert duplicate_result.stdout == "native_ci_evidence=failed\nreason=job_identity\n"
    assert not extra_output.exists()
    assert not duplicate_output.exists()


@pytest.mark.parametrize("boundary", ["run", "job", "step"])
def test_cli_rejects_duplicate_json_members_at_every_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    source = json.dumps(metadata_fixture(), separators=(",", ":"))
    if boundary == "run":
        source = source.replace(
            f'"headSha":"{SHA}"',
            f'"headSha":"{"b" * 40}","headSha":"{SHA}"',
            1,
        )
    elif boundary == "job":
        source = source.replace(
            '"name":"ubuntu-x64"',
            '"name":"wrong","name":"ubuntu-x64"',
            1,
        )
    else:
        source = source.replace(
            '"name":"Verify candidate SHA"',
            '"name":"wrong","name":"Verify candidate SHA"',
            1,
        )

    completed, output, _ = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    assert completed.stdout == "native_ci_evidence=failed\nreason=metadata_malformed\n"
    assert completed.stderr == ""
    assert not output.exists()
