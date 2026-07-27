"""Typed fake-gh fixtures shared by native CI evidence tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

_ROOT = Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "tests" / "native_ci_evidence.py"
_FAKE = _ROOT / "tests" / "native_ci_evidence_fake_gh.py"
_JOBS = ("ubuntu-x64", "macos-arm64")
SHA = "a" * 40
MARKERS = (
    "first_install",
    "unsafe_runtime_rejected",
    "serialized_reinstall",
    "no_write_reinstall",
    "executable_mode",
    "capability_key_metadata",
    "mcp_import",
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class FakeGh:
    metadata: dict[str, JsonValue] | str | None = None
    logs: dict[str, str] | None = None
    exit_code: int = 0
    delay_seconds: float = 0
    pushed_at: str | None = "2026-07-24T01:02:02Z"
    required_markers: tuple[str, ...] = MARKERS
    stream_stdout_bytes: int = 0
    stream_stderr_bytes: int = 0
    chunk_size: int = 65_536
    chunk_delay_seconds: float = 0
    response_chunk_size: int | None = None
    response_chunk_delay_seconds: float = 0
    completion_marker: str | None = None


_DEFAULT_FAKE_GH: Final = FakeGh()


def _fake_github_token(label: str) -> str:
    return f"{chr(103)}hp_{label}"


def load_json_document(source: str) -> JsonValue:
    """Load one test-produced JSON document with a recursive public type."""
    return _LOAD_JSON(source)


def job_fixture(
    name: str,
    database_id: int,
    conclusion: str = "success",
) -> dict[str, JsonValue]:
    return {
        "name": name,
        "databaseId": database_id,
        "status": "completed",
        "conclusion": conclusion,
        "steps": [
            {"name": "Verify candidate SHA", "status": "completed", "conclusion": "success"},
            {
                "name": "Run native installer smoke",
                "status": "completed",
                "conclusion": "success",
            },
        ],
    }


def metadata_fixture() -> dict[str, JsonValue]:
    return {
        "databaseId": 731,
        "headSha": SHA,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "createdAt": "2026-07-24T01:02:03Z",
        "jobs": [job_fixture("ubuntu-x64", 41), job_fixture("macos-arm64", 42)],
    }


def _logs() -> dict[str, str]:
    fake_log_token = _fake_github_token("FAKE_LOG_SECRET")
    return {
        "41": "".join(
            (
                f"ubuntu-x64\tRun native installer smoke\t{fake_log_token}\n",
                "ubuntu-x64\tRun native installer smoke\tAuthorization: Bearer fake-token\n",
                "".join(
                    f"ubuntu-x64\tRun native installer smoke\t{marker}=true\n" for marker in MARKERS
                ),
            )
        ),
        "42": "".join(
            (
                f"macos-arm64\tRun native installer smoke\t{fake_log_token}\n",
                "macos-arm64\tRun native installer smoke\tAuthorization: Bearer fake-token\n",
                "".join(
                    f"macos-arm64\tRun native installer smoke\t{marker}=true\n"
                    for marker in MARKERS
                ),
            )
        ),
    }


def _write_fake_gh(tmp_path: Path) -> None:
    if os.name == "nt":
        wrapper = tmp_path / "gh.cmd"
        _ = wrapper.write_text(f'@"{sys.executable}" "{_FAKE}" %*\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "gh"
        _ = wrapper.write_text(
            f"#!{sys.executable}\nexec(open({_FAKE.as_posix()!r}).read())\n",
            encoding="utf-8",
        )
        _ = wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)


def invoke_fake_gh(
    tmp_path: Path,
    fake: FakeGh = _DEFAULT_FAKE_GH,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the evidence CLI with one isolated fake gh executable."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_fake_gh(tmp_path)
    fixture = tmp_path / "fixture.json"
    wire = tmp_path / "wire.jsonl"
    output = tmp_path / "evidence.json"
    _ = fixture.write_text(
        json.dumps(
            {
                "metadata": metadata_fixture() if fake.metadata is None else fake.metadata,
                "logs": _logs() if fake.logs is None else fake.logs,
                "exit_code": fake.exit_code,
                "delay_seconds": fake.delay_seconds,
                "stream_stdout_bytes": fake.stream_stdout_bytes,
                "stream_stderr_bytes": fake.stream_stderr_bytes,
                "chunk_size": fake.chunk_size,
                "chunk_delay_seconds": fake.chunk_delay_seconds,
                "response_chunk_size": fake.response_chunk_size,
                "response_chunk_delay_seconds": fake.response_chunk_delay_seconds,
                "completion_marker": fake.completion_marker,
            }
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        str(_HELPER),
        "--run-id",
        "731",
        "--expected-sha",
        SHA,
    ]
    for job in _JOBS:
        command.extend(("--required-job", job))
    for marker in fake.required_markers:
        command.extend(("--required-marker", marker))
    command.extend(("--output", str(output), "--gh-timeout-seconds", "2"))
    if fake.pushed_at is not None:
        command.extend(("--pushed-at", fake.pushed_at))
    environment = os.environ | {
        "PATH": str(tmp_path),
        "CMW_FAKE_GH_FIXTURE": str(fixture),
        "CMW_FAKE_GH_WIRE": str(wire),
        "GH_TOKEN": _fake_github_token("FAKE_TOKEN_MUST_NOT_ESCAPE"),
    }
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=5,
    )
    return completed, output, wire
