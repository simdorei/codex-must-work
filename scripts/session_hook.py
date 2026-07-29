# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Retain a fail-silent compatibility entrypoint for removed SessionStart hooks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.hook_payload import HookEvent, parse_payload


def process_session_start(
    raw: str,
    *,
    root: Path | None = None,
    plugin_root: Path | None = None,
    plugin_data: Path | None = None,
) -> None:
    """Parse compatibility input without emitting context or touching state."""
    _ = root, plugin_root, plugin_data
    payload = parse_payload(raw)
    if payload is None or payload.event is not HookEvent.SESSION_START:
        return


def _main() -> int:
    try:
        process_session_start(sys.stdin.read())
    except json.JSONDecodeError as error:
        _ = sys.stderr.write(f"invalid hook JSON: {error.msg}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
