from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from tests import native_ci_evidence
from tests.native_ci_evidence_test_support import (
    MARKERS,
    FakeGh,
    JsonValue,
    invoke_fake_gh,
    metadata_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path

type TempfileArgument = str | bytes | int | bool | os.PathLike[str] | None


@dataclass(frozen=True, slots=True)
class WireCase:
    metadata: dict[str, JsonValue] | str | None
    logs: dict[str, str] | None
    exit_code: int
    delay: float
    reason: str


@pytest.mark.parametrize(
    "case",
    [
        WireCase("{", None, 0, 0, "metadata_malformed"),
        WireCase(None, {"41": "", "42": ""}, 0, 0, "marker_missing"),
        WireCase(
            metadata=None,
            logs={
                "41": "".join(
                    (
                        "ubuntu-x64\tRun native installer smoke\tERROR first_install=true\n",
                        "ubuntu-x64\tVerify candidate SHA\tfirst_install=true\n",
                        "".join(
                            f"ubuntu-x64\tRun native installer smoke\t{marker}=true\n"
                            for marker in MARKERS[1:]
                        ),
                    )
                ),
                "42": "".join(
                    f"macos-arm64\tRun native installer smoke\t{marker}=true\n"
                    for marker in MARKERS
                ),
            },
            exit_code=0,
            delay=0,
            reason="marker_missing",
        ),
        WireCase(
            metadata=None,
            logs={
                "41": "".join(
                    f"ubuntu-x64\tRun native installer smoke\t{marker}=true\n" for marker in MARKERS
                ),
                "42": "".join(
                    f"macos-arm64\tRun native installer smoke\t{marker}=true\n"
                    for marker in MARKERS[:-1]
                )
                + "ubuntu-x64\tRun native installer smoke\tmcp_import=true\n",
            },
            exit_code=0,
            delay=0,
            reason="marker_missing",
        ),
        WireCase(None, None, 23, 0, "gh_failed"),
        WireCase(None, None, 0, 3, "gh_timeout"),
        WireCase(
            None,
            {
                "41": "".join(
                    (
                        "ubuntu-x64\tRun native installer smoke\tfirst_install=true\n",
                        "ubuntu-x64\tRun native installer smoke\tfirst_install=true\n",
                        "".join(
                            f"ubuntu-x64\tRun native installer smoke\t{marker}=true\n"
                            for marker in MARKERS[1:]
                        ),
                    )
                ),
                "42": "".join(
                    f"macos-arm64\tRun native installer smoke\t{marker}=true\n"
                    for marker in MARKERS
                ),
            },
            0,
            0,
            "marker_missing",
        ),
    ],
)
def test_cli_rejects_bad_wire_data_without_leaking_it(
    tmp_path: Path,
    case: WireCase,
) -> None:
    # Given/When: malformed, misleading, missing, failed, or hung fake-gh data is supplied.
    completed, output, _wire = invoke_fake_gh(
        tmp_path,
        FakeGh(
            metadata=case.metadata,
            logs=case.logs,
            exit_code=case.exit_code,
            delay_seconds=case.delay,
        ),
    )

    # Then: only a stable public reason is returned.
    assert completed.returncode == 1
    assert completed.stdout == f"native_ci_evidence=failed\nreason={case.reason}\n"
    assert completed.stderr == ""
    assert not output.exists()
    assert "ghp_" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("stream_stdout_bytes", "gh_output_too_large"),
        ("stream_stderr_bytes", "gh_output_too_large"),
    ],
)
def test_cli_caps_streamed_stdout_and_stderr_before_capture(
    tmp_path: Path,
    field: str,
    reason: str,
) -> None:
    # Given: gh streams more bytes than either public boundary permits.
    completed_marker = tmp_path / "child-completed"
    fake = (
        FakeGh(
            stream_stdout_bytes=5_000_000,
            chunk_delay_seconds=0.01,
            completion_marker=str(completed_marker),
        )
        if field == "stream_stdout_bytes"
        else FakeGh(
            stream_stderr_bytes=5_000_000,
            chunk_delay_seconds=0.01,
            completion_marker=str(completed_marker),
        )
    )

    # When: the helper monitors the child while it writes.
    completed, output, _ = invoke_fake_gh(tmp_path, fake)
    time.sleep(1)

    # Then: the child stays stopped and no captured private output is returned.
    assert completed.stdout == f"native_ci_evidence=failed\nreason={reason}\n"
    assert completed.stderr == ""
    assert not output.exists()
    assert not completed_marker.exists()


def test_cli_decodes_utf8_only_after_slow_chunked_stream_completes(tmp_path: Path) -> None:
    # Given: valid metadata with a multibyte field split across one-byte writes.
    metadata = _metadata_with_unicode()

    # When: the fake gh emits the response slowly across UTF-8 boundaries.
    completed, output, _ = invoke_fake_gh(
        tmp_path,
        FakeGh(
            metadata=metadata,
            response_chunk_size=1,
            response_chunk_delay_seconds=0.0001,
        ),
    )

    # Then: final decoding succeeds without exposing the unused private field.
    assert completed.stdout == "native_ci_evidence=ok\n"
    assert output.is_file()
    assert "한" not in output.read_text(encoding="utf-8")


def _metadata_with_unicode() -> str:
    metadata = metadata_fixture()
    metadata["private_note"] = "한"
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


def test_cli_redacts_output_write_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: validated public evidence followed by an output filesystem failure.
    summary: dict[str, JsonValue] = {
        "run_id": 731,
        "head_sha": "a" * 40,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "jobs": [],
    }

    def fixed_collect(
        _request: native_ci_evidence.EvidenceRequest,
    ) -> dict[str, JsonValue]:
        return summary

    monkeypatch.setattr(native_ci_evidence, "collect", fixed_collect)

    def reject_tempfile(
        *_args: TempfileArgument,
        **_kwargs: TempfileArgument,
    ) -> tuple[int, str]:
        msg = "private/local/path must not escape"
        raise OSError(msg)

    monkeypatch.setattr(tempfile, "mkstemp", reject_tempfile)
    arguments = [
        "--run-id",
        "731",
        "--expected-sha",
        "a" * 40,
        "--pushed-at",
        "2026-07-24T01:02:02Z",
        "--output",
        str(tmp_path / "evidence.json"),
    ]
    for job in ("ubuntu-x64", "macos-arm64"):
        arguments.extend(("--required-job", job))
    for marker in MARKERS:
        arguments.extend(("--required-marker", marker))

    # When: the CLI attempts the atomic evidence write.
    exit_code = native_ci_evidence.main(arguments)

    # Then: no traceback, local path, or raw OSError reaches either public stream.
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "native_ci_evidence=failed\nreason=output_failed\n"
    assert captured.err == ""
