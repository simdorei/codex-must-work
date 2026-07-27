# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# Invoked only through tests/test_native_ci_evidence.py as a fake gh wire endpoint.
"""Deterministic fake for the two authenticated gh surfaces used by native CI evidence."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


def _fake_github_token(label: str) -> str:
    return f"{chr(103)}hp_{label}"


@dataclass(frozen=True, slots=True)
class _ChunkedOutput:
    stream: int
    data: bytes
    size: int
    delay: float


def _write_chunks(output: _ChunkedOutput) -> None:
    for offset in range(0, len(output.data), output.size):
        _ = os.write(output.stream, output.data[offset : offset + output.size])
        if output.delay > 0:
            time.sleep(output.delay)


def _load_fixture() -> dict[str, JsonValue] | None:
    fixture_path = Path(os.environ["CMW_FAKE_GH_FIXTURE"])
    raw = _LOAD_JSON(fixture_path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else None


def _record_call(arguments: list[str]) -> None:
    wire_path = Path(os.environ["CMW_FAKE_GH_WIRE"])
    with wire_path.open("a", encoding="utf-8", newline="\n") as wire:
        _ = wire.write(json.dumps(arguments, separators=(",", ":")) + "\n")


def _preflight(raw: dict[str, JsonValue]) -> int | None:
    delay = raw.get("delay_seconds")
    if isinstance(delay, (int, float)) and not isinstance(delay, bool):
        time.sleep(delay)
    exit_code = raw.get("exit_code", 0)
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return 91
    if exit_code != 0:
        secret = _fake_github_token("FAKE_SHOULD_NEVER_ESCAPE")
        _ = sys.stderr.write(f"gh failed with secret {secret}\n")
        return exit_code
    return None


def _stream_fixture(raw: dict[str, JsonValue]) -> int | None:
    stream_stdout = raw.get("stream_stdout_bytes", 0)
    stream_stderr = raw.get("stream_stderr_bytes", 0)
    chunk_size = raw.get("chunk_size", 65_536)
    chunk_delay = raw.get("chunk_delay_seconds", 0)
    if (
        not isinstance(stream_stdout, int)
        or isinstance(stream_stdout, bool)
        or not isinstance(stream_stderr, int)
        or isinstance(stream_stderr, bool)
        or not isinstance(chunk_size, int)
        or isinstance(chunk_size, bool)
        or chunk_size < 1
        or not isinstance(chunk_delay, (int, float))
        or isinstance(chunk_delay, bool)
    ):
        return 94
    if stream_stdout:
        output = _ChunkedOutput(sys.stdout.fileno(), b"x" * stream_stdout, chunk_size, chunk_delay)
    elif stream_stderr:
        secret = _fake_github_token("FAKE_STREAM_SECRET").encode()
        payload = (secret * ((stream_stderr // len(secret)) + 1))[:stream_stderr]
        output = _ChunkedOutput(sys.stderr.fileno(), payload, chunk_size, chunk_delay)
    else:
        return None
    _write_chunks(output)
    marker = raw.get("completion_marker")
    if isinstance(marker, str):
        Path(marker).touch()
    return 0


def _metadata_response(raw: dict[str, JsonValue]) -> int:
    metadata = raw.get("metadata")
    response_chunk = raw.get("response_chunk_size")
    response_delay = raw.get("response_chunk_delay_seconds", 0)
    if isinstance(response_chunk, int) and not isinstance(response_chunk, bool):
        payload = (
            metadata.encode("utf-8")
            if isinstance(metadata, str)
            else json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        delay = (
            float(response_delay)
            if isinstance(response_delay, (int, float)) and not isinstance(response_delay, bool)
            else 0
        )
        _write_chunks(_ChunkedOutput(sys.stdout.fileno(), payload, response_chunk, delay))
    elif isinstance(metadata, str):
        _ = sys.stdout.write(metadata)
    else:
        _ = sys.stdout.write(json.dumps(metadata, separators=(",", ":")))
    return 0


def _respond(raw: dict[str, JsonValue], args: list[str]) -> int:
    if args[:2] != ["run", "view"]:
        return 92
    if "--json" in args:
        return _metadata_response(raw)
    if "--job" in args and "--log" in args:
        job_index = args.index("--job") + 1
        logs = raw.get("logs")
        value = logs.get(args[job_index]) if isinstance(logs, dict) else None
        if isinstance(value, str):
            _ = sys.stdout.write(value)
            return 0
    return 93


def main() -> int:
    raw = _load_fixture()
    if raw is None:
        return 90
    arguments = sys.argv[1:]
    _record_call(arguments)
    failure = _preflight(raw)
    if failure is not None:
        return failure
    streamed = _stream_fixture(raw)
    if streamed is not None:
        return streamed
    return _respond(raw, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
