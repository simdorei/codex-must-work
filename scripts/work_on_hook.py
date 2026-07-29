# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Authorize one explicit work-on prompt through the private ticket boundary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Final, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.control_capability import load_control_key
from scripts.state import JsonValue, state_root
from scripts.work_on_activation import (
    ActivationIdentity,
    ActivationTicketStore,
    contains_explicit_work_on,
)

_EVENT: Final = "UserPromptSubmit"


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


def process_user_prompt_submit(
    raw: str,
    *,
    plugin_data: Path,
) -> dict[str, JsonValue] | None:
    """Issue a ticket only for the documented exact raw-prompt token."""
    decoded = _LOAD_JSON(raw)
    if type(decoded) is not dict or decoded.get("hook_event_name") != _EVENT:
        return None
    identity = _identity(decoded)
    prompt = decoded.get("prompt")
    if identity is None or type(prompt) is not str or not contains_explicit_work_on(prompt):
        return None
    key = load_control_key(plugin_data, state_root())
    if not ActivationTicketStore(plugin_data, key).issue(identity):
        return None
    return {
        "session_id": identity.session_id,
        "activation_turn_id": identity.turn_id,
        "transcript_path": identity.transcript_path,
    }


def _identity(values: dict[str, JsonValue]) -> ActivationIdentity | None:
    session_id = values.get("session_id")
    turn_id = values.get("turn_id")
    transcript_path = values.get("transcript_path")
    if type(session_id) is not str or type(turn_id) is not str or type(transcript_path) is not str:
        return None
    return ActivationIdentity(session_id, turn_id, transcript_path)


def _main() -> int:
    configured = os.environ.get("PLUGIN_DATA")
    if not configured:
        _ = sys.stderr.write("PLUGIN_DATA is required for Codex Must Work hooks\n")
        return 1
    try:
        context = process_user_prompt_submit(sys.stdin.read(), plugin_data=Path(configured))
    except json.JSONDecodeError as error:
        _ = sys.stderr.write(f"invalid hook JSON: {error.msg}\n")
        return 1
    if context is not None:
        _ = sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": _EVENT,
                        "additionalContext": json.dumps(
                            {"codex_must_work_activation": context},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
