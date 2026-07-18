"""Apply allowlisted hook metadata to one opted-in runtime document."""

from __future__ import annotations

from pathlib import Path
from typing import assert_never
from uuid import uuid4

from scripts.activity_epoch import advance_monitor_progress_epoch, advance_turn_activity_epoch
from scripts.hook_payload import HookEvent, HookPayload
from scripts.state import CorruptReason, CorruptStateError, JsonValue


def apply_hook_event(
    values: dict[str, JsonValue],
    payload: HookPayload,
    now: str,
    path: Path,
) -> bool:
    """Mutate runtime values and return whether the first watcher must launch."""
    children = _children(values, path)
    before = (
        values.get("parent"),
        dict(children),
        values.get("parent_turn_id"),
        values.get("parent_complete"),
    )
    should_launch = _apply_event(values, children, payload, now, path)
    after = (
        values.get("parent"),
        children,
        values.get("parent_turn_id"),
        values.get("parent_complete"),
    )
    if after != before:
        advance_turn_activity_epoch(values, path)
    values["children"] = children
    return should_launch


def safe_transcript_path(raw: str | None, root: Path) -> str | None:
    """Return a CODEX_HOME-relative rollout path or reject it."""
    if raw is None:
        return None
    codex_home = root.parent.resolve()
    try:
        return Path(raw).resolve().relative_to(codex_home).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def start_managed_parent(
    values: dict[str, JsonValue],
    turn_id: str,
    now: str,
    path: Path,
) -> None:
    """Synchronize watcher ownership when the manager observes a new turn."""
    children = _children(values, path)
    _start_parent(values, children, turn_id, now, path)
    values["children"] = children


def _children(values: dict[str, JsonValue], path: Path) -> dict[str, JsonValue]:
    children = values.get("children")
    if isinstance(children, dict):
        return dict(children)
    raise CorruptStateError(path=path, reason=CorruptReason.INVALID_VALUE)


def _monitor(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> dict[str, JsonValue] | None:
    monitor = values.get("parent") if target_id is None else children.get(target_id)
    if monitor is None:
        return None
    if isinstance(monitor, dict):
        return dict(monitor)
    raise CorruptStateError(path=path, reason=CorruptReason.INVALID_VALUE)


def _apply_event(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    payload: HookPayload,
    now: str,
    path: Path,
) -> bool:
    match payload.event:
        case HookEvent.SESSION_START:
            return False
        case HookEvent.USER_PROMPT_SUBMIT:
            if payload.turn_id is None:
                return False
            _start_parent(values, children, payload.turn_id, now, path)
            _clear_waits(values, children, path)
            return True
        case HookEvent.SUBAGENT_START:
            return _start_child(values, children, payload.agent_id, now, path)
        case HookEvent.SUBAGENT_STOP:
            _mark_monitor_terminal(values, children, payload.agent_id, path)
        case HookEvent.PRE_TOOL_USE:
            _tool_started(values, children, payload.agent_id, path)
        case HookEvent.POST_TOOL_USE:
            _tool_finished(values, children, payload.agent_id, now, path)
        case HookEvent.PERMISSION_REQUEST:
            _mark_approval_wait(values, children, payload.agent_id, path)
        case HookEvent.STOP:
            return _stop_parent(values, children, path)
        case _:
            assert_never(payload.event)
    return False


def _stop_parent(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    path: Path,
) -> bool:
    if values.get("managed_mode") is True:
        for target_id in (None, *tuple(children)):
            _mark_monitor_terminal(values, children, target_id, path)
        values["handoff_requested"] = values.get("shutdown_requested") is not True
        values["parent_complete"] = False
        return False
    if values.get("observe_only") is not True:
        return False
    _mark_monitor_terminal(values, children, None, path)
    values["parent_complete"] = True
    return True


def _clear_waits(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    path: Path,
) -> None:
    for target_id in (None, *tuple(children)):
        monitor = _monitor(values, children, target_id, path)
        if monitor is not None:
            monitor["waiting_for_user"] = False
            monitor["waiting_for_approval"] = False
            _save_monitor(values, children, target_id, monitor)


def _start_parent(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    turn_id: str,
    now: str,
    path: Path,
) -> None:
    previous = _monitor(values, children, None, path)
    if values.get("parent_turn_id") != turn_id or previous is None:
        values["parent"] = _new_monitor(now, _next_parent_generation(previous))
    values["parent_turn_id"] = turn_id
    values["parent_complete"] = False


def _start_child(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    child_id: str | None,
    now: str,
    path: Path,
) -> bool:
    if child_id is None:
        return False
    target_ids: tuple[str | None, ...] = (None, *tuple(children))
    had_nonterminal = any(
        monitor is not None and monitor.get("status") != "terminal"
        for monitor in (_monitor(values, children, target_id, path) for target_id in target_ids)
    )
    previous = _monitor(values, children, child_id, path)
    children[child_id] = _new_monitor(now, _next_generation(previous))
    values["parent_complete"] = False
    return not had_nonterminal


def _tool_started(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> None:
    monitor = _monitor(values, children, target_id, path)
    if monitor is None:
        return
    count = monitor.get("open_tool_count")
    monitor["open_tool_count"] = count + 1 if type(count) is int else 1
    monitor["waiting_for_approval"] = False
    advance_monitor_progress_epoch(monitor, path)
    _save_monitor(values, children, target_id, monitor)


def _tool_finished(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    now: str,
    path: Path,
) -> None:
    monitor = _monitor(values, children, target_id, path)
    if monitor is None:
        return
    count = monitor.get("open_tool_count")
    monitor["open_tool_count"] = max(0, count - 1) if type(count) is int else 0
    monitor["last_tool_result_at"] = now
    monitor["waiting_for_approval"] = False
    advance_monitor_progress_epoch(monitor, path)
    _save_monitor(values, children, target_id, monitor)


def _mark_monitor_terminal(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> None:
    monitor = _monitor(values, children, target_id, path)
    if monitor is None:
        return
    monitor["status"] = "terminal"
    _save_monitor(values, children, target_id, monitor)


def _mark_approval_wait(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    path: Path,
) -> None:
    monitor = _monitor(values, children, target_id, path)
    if monitor is None:
        return
    monitor["waiting_for_approval"] = True
    _save_monitor(values, children, target_id, monitor)


def _save_monitor(
    values: dict[str, JsonValue],
    children: dict[str, JsonValue],
    target_id: str | None,
    monitor: dict[str, JsonValue],
) -> None:
    if target_id is None:
        values["parent"] = monitor
    else:
        children[target_id] = monitor


def _next_generation(previous: dict[str, JsonValue] | None) -> int:
    if previous is None:
        return 1
    generation = previous.get("generation")
    current = generation if type(generation) is int and generation > 0 else 1
    return current + 1 if previous.get("status") == "terminal" else current


def _next_parent_generation(previous: dict[str, JsonValue] | None) -> int:
    if previous is None:
        return 1
    generation = previous.get("generation")
    return generation + 1 if type(generation) is int and generation > 0 else 2


def _new_monitor(now: str, generation: int) -> dict[str, JsonValue]:
    return {
        "status": "running",
        "generation": generation,
        "last_item_at": None,
        "last_delta_at": None,
        "last_tool_result_at": None,
        "silence_started_at": now,
        "open_tool_count": 0,
        "waiting_for_approval": False,
        "waiting_for_user": False,
        "silence_id": uuid4().hex,
        "warning_sent": False,
        "restart_count": 0,
        "progress_epoch": 0,
    }
