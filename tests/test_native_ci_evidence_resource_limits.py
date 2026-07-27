from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import pytest

from tests import native_ci_evidence_json
from tests.native_ci_evidence_json import (
    MAX_JSON_NESTING,
    MAX_JSON_NUMBER_DIGITS,
    JsonInputError,
)
from tests.native_ci_evidence_test_support import FakeGh, invoke_fake_gh, metadata_fixture

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

type Boundary = Literal["run", "job", "unused"]
type Resource = Literal["nesting", "integer"]


def _metadata_source() -> str:
    return json.dumps(metadata_fixture(), separators=(",", ":"))


def _place_payload(source: str, boundary: Boundary, payload: str) -> str:
    placements: dict[Boundary, str] = {
        "run": source.replace('"databaseId":731', f'"databaseId":{payload}', 1),
        "job": source.replace('"databaseId":41', f'"databaseId":{payload}', 1),
        "unused": f'{source[:-1]},"unused":{payload}}}',
    }
    return placements[boundary]


def _assert_malformed(
    completed: subprocess.CompletedProcess[str],
    output: Path,
) -> None:
    assert completed.returncode == 1
    assert completed.stdout == "native_ci_evidence=failed\nreason=metadata_malformed\n"
    assert completed.stderr == ""
    assert not output.exists()


@pytest.mark.parametrize(
    "source",
    [
        "{",
        f"{'[' * 5_000}0{']' * 5_000}",
        "9" * 5_000,
    ],
)
def test_json_loader_normalizes_expected_stdlib_decoder_failures(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the budget scanner is bypassed to exercise decoder failure containment.
    def allow_all(_source: str) -> None:
        return None

    monkeypatch.setattr(native_ci_evidence_json, "_enforce_resource_limits", allow_all)

    # When/Then: JSONDecodeError, RecursionError, and integer ValueError are typed.
    with pytest.raises(JsonInputError):
        _ = native_ci_evidence_json.load_json(source)


@pytest.mark.parametrize("boundary", ["run", "job", "unused"])
@pytest.mark.parametrize("resource", ["nesting", "integer"])
def test_cli_contains_extreme_json_resource_failures_at_parse_boundary(
    tmp_path: Path,
    boundary: Boundary,
    resource: Resource,
) -> None:
    # Given: a sub-2 MB document that exceeds a stdlib JSON resource boundary.
    payloads: dict[Resource, str] = {
        "nesting": f"{'[' * 5_000}0{']' * 5_000}",
        "integer": "9" * 5_000,
    }
    payload = payloads[resource]
    source = _place_payload(_metadata_source(), boundary, payload)
    assert len(source.encode("utf-8")) < 2_000_000

    # When: the document crosses the real CLI/fake-gh boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: recursion and integer-limit errors become one stable public error.
    _assert_malformed(completed, output)


def test_cli_accepts_exact_json_nesting_limit(tmp_path: Path) -> None:
    # Given: root object plus nested arrays exactly equal the aggregate depth limit.
    array_levels = MAX_JSON_NESTING - 1
    payload = f"{'[' * array_levels}0{']' * array_levels}"
    source = _place_payload(_metadata_source(), "unused", payload)

    # When: exact-limit metadata crosses the boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: the unused value does not invalidate otherwise authentic evidence.
    assert completed.returncode == 0
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert completed.stderr == ""
    assert output.is_file()


def test_cli_rejects_json_nesting_limit_plus_one(tmp_path: Path) -> None:
    # Given: root object plus arrays exceed the aggregate depth limit by one.
    payload = f"{'[' * MAX_JSON_NESTING}0{']' * MAX_JSON_NESTING}"
    source = _place_payload(_metadata_source(), "unused", payload)

    # When: over-depth metadata crosses the boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: it fails before recursive stdlib decoding.
    _assert_malformed(completed, output)


def test_cli_accepts_exact_json_numeric_digit_limit(tmp_path: Path) -> None:
    # Given: one unused integer token exactly at the digit budget.
    source = _place_payload(
        _metadata_source(),
        "unused",
        "9" * MAX_JSON_NUMBER_DIGITS,
    )

    # When: exact-limit metadata crosses the boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: the bounded unused number does not invalidate evidence.
    assert completed.returncode == 0
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert completed.stderr == ""
    assert output.is_file()


def test_cli_rejects_json_numeric_digit_limit_plus_one(tmp_path: Path) -> None:
    # Given: one unused integer token exceeds the digit budget by one.
    source = _place_payload(
        _metadata_source(),
        "unused",
        "9" * (MAX_JSON_NUMBER_DIGITS + 1),
    )

    # When: the over-budget number crosses the boundary.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: it fails before integer conversion.
    _assert_malformed(completed, output)


def test_cli_ignores_brackets_digits_and_surrogates_inside_strings(
    tmp_path: Path,
) -> None:
    # Given: a valid string containing over-limit-looking text and escaped surrogates.
    value = (
        ("[" * (MAX_JSON_NESTING + 1))
        + '"\\'
        + ("9" * (MAX_JSON_NUMBER_DIGITS + 1))
        + "\ud800\udfff"
    )
    source = _place_payload(_metadata_source(), "unused", json.dumps(value))

    # When: the scanner and JSON decoder process string escapes.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: string content never consumes structural or numeric budgets.
    assert completed.returncode == 0
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert completed.stderr == ""
    assert output.is_file()


def test_cli_accepts_large_but_bounded_unused_string(tmp_path: Path) -> None:
    # Given: valid metadata with a large string still below the 2 MB process ceiling.
    source = _place_payload(
        _metadata_source(),
        "unused",
        json.dumps("x" * 1_800_000),
    )
    assert len(source.encode("utf-8")) < 2_000_000

    # When: the bounded document crosses the fake-gh wire.
    completed, output, _wire = invoke_fake_gh(tmp_path, FakeGh(metadata=source))

    # Then: linear scanning and decoding preserve the valid public result.
    assert completed.returncode == 0
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert completed.stderr == ""
    assert output.is_file()
