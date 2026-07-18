"""Build fixed user-style prompts without persisting task text."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.state_io import JsonValue


def continuation_prompt(preset: str, *, restarted: bool) -> str:
    """Return the selected fixed handoff or restart instruction."""
    prefix = (
        "The previous managed turn had no observable progress and was interrupted. "
        if restarted
        else ""
    )
    cleanup = (
        "Check whether task-owned runtime processes are merely left open, clean them up safely, "
        if preset == "cleanup"
        else ""
    )
    return (
        prefix
        + cleanup
        + "continue the same opted-in task until every success criterion is verified. "
        + "When it is genuinely complete, invoke $work-off as a verified-completion shutdown "
        + "before the final answer."
    )


def result_turn_id(result: dict[str, JsonValue]) -> str | None:
    """Read the required turn identifier from a turn/start response."""
    turn = result.get("turn")
    if not isinstance(turn, dict):
        return None
    value = turn.get("id")
    return value if isinstance(value, str) and value else None
