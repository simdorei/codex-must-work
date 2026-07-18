"""Parse and serialize the small allowlist used by Codex hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final, Protocol, assert_never

from scripts.calibration import CalibrationStatus

if TYPE_CHECKING:
    from scripts.calibration_state import CalibrationSnapshot
    from scripts.state import JsonValue


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@unique
class HookEvent(StrEnum):
    """Hook events whose metadata can change monitor state."""

    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    STOP = "Stop"


@dataclass(frozen=True, slots=True)
class HookPayload:
    """Privacy-safe fields accepted from a hook payload."""

    session_id: str
    event: HookEvent
    turn_id: str | None
    transcript_path: str | None
    agent_id: str | None
    permission_mode: str | None


@dataclass(frozen=True, slots=True)
class SessionLocator:
    """Inputs an explicit work-on or work-off skill needs for this session."""

    session_id: str
    transcript_path: str
    plugin_root: str
    plugin_data: str
    permission_mode: str | None
    calibration: CalibrationSnapshot


@dataclass(frozen=True, slots=True)
class StopContinuation:
    """A user-style prompt for Codex's supported Stop continuation."""

    reason: str


def parse_payload(raw: str) -> HookPayload | None:
    """Parse only identifiers needed for routing; ignore every body field."""
    decoded = _LOAD_JSON(raw)
    if type(decoded) is not dict:
        return None
    session_id = _optional_string(decoded, "session_id")
    event_name = _optional_string(decoded, "hook_event_name")
    if session_id is None or event_name is None:
        return None
    try:
        event = HookEvent(event_name)
    except ValueError:
        return None
    return HookPayload(
        session_id=session_id,
        event=event,
        turn_id=_optional_string(decoded, "turn_id"),
        transcript_path=_optional_string(decoded, "transcript_path"),
        agent_id=_optional_string(decoded, "agent_id"),
        permission_mode=_optional_string(decoded, "permission_mode"),
    )


def serialize_locator(locator: SessionLocator) -> str:
    """Encode SessionStart context without creating persistent state."""
    context = json.dumps(
        {
            "codex_must_work_locator": {
                "session_id": locator.session_id,
                "transcript_path": locator.transcript_path,
                "plugin_root": locator.plugin_root,
                "plugin_data": locator.plugin_data,
                "permission_mode": locator.permission_mode,
            },
            "codex_must_work_calibration": _calibration_context(locator.calibration),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": HookEvent.SESSION_START.value,
                "additionalContext": context,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _calibration_context(snapshot: CalibrationSnapshot) -> dict[str, JsonValue]:
    match snapshot.status:
        case CalibrationStatus.PENDING:
            action = "ask_apply" if snapshot.should_announce else "awaiting_answer"
            required_skill: str | None = "work-calibration" if snapshot.should_announce else None
        case CalibrationStatus.ACCEPTED:
            action = "use_recommendation"
            required_skill = None
        case CalibrationStatus.REJECTED:
            action = "use_defaults"
            required_skill = None
        case CalibrationStatus.INSUFFICIENT:
            action = "notify_insufficient_once" if snapshot.should_announce else "no_action"
            required_skill = "work-calibration" if snapshot.should_announce else None
        case CalibrationStatus.UNAVAILABLE:
            action = "notify_unavailable_once" if snapshot.should_announce else "no_action"
            required_skill = "work-calibration" if snapshot.should_announce else None
        case _:
            assert_never(snapshot.status)
    return {
        "plugin_version": snapshot.plugin_version,
        "status": snapshot.status.value,
        "action": action,
        "required_skill": required_skill,
        "sample_count": snapshot.sample_count,
        "warning_after_ms": snapshot.warning_after_ms,
        "restart_after_ms": snapshot.restart_after_ms,
        "reason_code": snapshot.reason_code,
    }


def serialize_stop_continuation(continuation: StopContinuation) -> str:
    """Encode the documented Stop-hook continuation response."""
    return json.dumps(
        {"decision": "block", "reason": continuation.reason},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _optional_string(values: dict[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) and value else None
