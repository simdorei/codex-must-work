from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.native_ci_evidence_test_support import (
    FakeGh,
    JsonValue,
    invoke_fake_gh,
    metadata_fixture,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path


def _assert_public_failure(
    completed: subprocess.CompletedProcess[str],
    output: Path,
    reason: str,
) -> None:
    assert completed.returncode == 1
    assert completed.stdout == f"native_ci_evidence=failed\nreason={reason}\n"
    assert completed.stderr == ""
    assert not output.exists()


@pytest.mark.parametrize("name", [[], {}, None, True, 7, 1.5])
def test_cli_rejects_non_text_job_names_without_traceback(
    tmp_path: Path,
    name: JsonValue,
) -> None:
    # Given: an untrusted job name that cannot be a public job identity.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    job = jobs[0]
    assert isinstance(job, dict)
    job["name"] = name

    # When: metadata crosses the real CLI/fake-gh wire boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: even unhashable values produce one stable, redacted public failure.
    _assert_public_failure(completed, output, "job_identity")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("boundary", ["run", "job", "step"])
def test_cli_rejects_non_standard_json_constants_at_every_boundary(
    tmp_path: Path,
    constant: str,
    boundary: str,
) -> None:
    # Given: otherwise valid metadata with one non-standard JSON numeric constant.
    source = json.dumps(metadata_fixture(), separators=(",", ":"))
    replacements = {
        "run": ('"databaseId":731', f'"databaseId":{constant}'),
        "job": ('"databaseId":41', f'"databaseId":{constant}'),
        "step": (
            '"name":"Verify candidate SHA","status":"completed","conclusion":"success"',
            f'"name":"Verify candidate SHA","status":"completed","conclusion":{constant}',
        ),
    }
    old, new = replacements[boundary]
    source = source.replace(old, new, 1)

    # When: the document crosses the strict JSON boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: Python-only constants are malformed JSON, never downstream values.
    _assert_public_failure(completed, output, "metadata_malformed")


@pytest.mark.parametrize("row", [[], None, True, 7, 1.5, "job", {}])
def test_cli_rejects_wrong_job_row_types_stably(
    tmp_path: Path,
    row: JsonValue,
) -> None:
    # Given: one required job row is replaced with an invalid JSON value.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    jobs[0] = row

    # When: the malformed nested row is authenticated.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: the public error remains stable and traceback-free.
    _assert_public_failure(completed, output, "job_identity")


@pytest.mark.parametrize("value", [[], {}, None, True, 1.5, "41"])
def test_cli_rejects_wrong_job_database_id_types_stably(
    tmp_path: Path,
    value: JsonValue,
) -> None:
    # Given: a nested job database ID contains a wrong JSON value type.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    job = jobs[0]
    assert isinstance(job, dict)
    job["databaseId"] = value

    # When: the malformed identity is authenticated.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: the identity boundary emits only its stable public reason.
    _assert_public_failure(completed, output, "job_identity")


@pytest.mark.parametrize("value", [[], {}, None, True, 7, 1.5])
@pytest.mark.parametrize("field", ["status", "conclusion"])
def test_cli_rejects_wrong_job_state_field_types_stably(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    # Given: a nested job state field contains a wrong JSON value type.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    job = jobs[0]
    assert isinstance(job, dict)
    job[field] = value

    # When: the malformed state is authenticated.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: the job-state boundary emits only its stable public reason.
    _assert_public_failure(completed, output, "job_state")


@pytest.mark.parametrize("value", [[], {}, None, True, 7, 1.5])
def test_cli_rejects_wrong_steps_types_stably(
    tmp_path: Path,
    value: JsonValue,
) -> None:
    # Given: the nested steps field contains a wrong JSON value type.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    job = jobs[0]
    assert isinstance(job, dict)
    job["steps"] = value

    # When: the malformed steps value is authenticated.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: the step boundary emits only its stable public reason.
    _assert_public_failure(completed, output, "step_state")


@pytest.mark.parametrize("value", [[], {}, None, True, 7, 1.5])
@pytest.mark.parametrize("field", ["name", "status", "conclusion"])
def test_cli_rejects_wrong_nested_step_field_types_stably(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    # Given: one required step field contains a wrong JSON value type.
    metadata = metadata_fixture()
    jobs = metadata["jobs"]
    assert isinstance(jobs, list)
    job = jobs[0]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, dict)
    step[field] = value

    # When: the malformed step is authenticated.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=metadata))

    # Then: the public step error stays stable and traceback-free.
    _assert_public_failure(completed, output, "step_state")
