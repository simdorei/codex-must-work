# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Handle privacy-safe Codex hook events."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.calibration_scan import scan_history
from scripts.calibration_state import CalibrationEnvironment, load_or_calibrate
from scripts.hook_payload import (
    HookEvent,
    HookPayload,
    SessionLocator,
    StopContinuation,
    parse_payload,
    serialize_locator,
    serialize_stop_continuation,
)
from scripts.hook_state import apply_hook_event, safe_transcript_path
from scripts.path_identity import resolve_local_path
from scripts.private_root import ensure_private_root
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    config_path,
    load_state,
    mutate_existing_state,
    runtime_path,
    state_root,
)
from scripts.watcher_launch import launch_watcher

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

_launch_watcher = launch_watcher
_scan_history = scan_history


def process_hook(
    raw: str,
    *,
    root: Path | None = None,
    plugin_root: Path | None = None,
    plugin_data: Path | None = None,
) -> SessionLocator | StopContinuation | None:
    """Apply one allowlisted hook payload without retaining body fields."""
    payload = parse_payload(raw)
    if payload is None:
        return None
    if payload.event is HookEvent.SESSION_START:
        return _session_locator(payload, root, plugin_root, plugin_data)
    return _process_enabled_event(payload, root)


def _session_locator(
    payload: HookPayload,
    root: Path | None,
    plugin_root: Path | None,
    plugin_data: Path | None,
) -> SessionLocator | None:
    if payload.transcript_path is None:
        return None
    if plugin_data is None:
        message = "PLUGIN_DATA is required for SessionStart"
        raise RuntimeError(message)
    active_root = state_root() if root is None else root
    active_plugin_root = (
        Path(__file__).resolve().parent.parent if plugin_root is None else plugin_root.resolve()
    )
    calibration = load_or_calibrate(
        CalibrationEnvironment(
            state_root=active_root,
            plugin_root=active_plugin_root,
            codex_home=active_root.parent,
            now=datetime.now(UTC),
        ),
        _scan_history,
    )
    return SessionLocator(
        session_id=payload.session_id,
        transcript_path=str(resolve_local_path(payload.transcript_path)),
        plugin_root=str(active_plugin_root),
        plugin_data=str(plugin_data.resolve()),
        permission_mode=payload.permission_mode,
        calibration=calibration,
    )


def _process_enabled_event(
    payload: HookPayload,
    root: Path | None,
) -> StopContinuation | None:

    active_root = state_root() if root is None else root
    path = runtime_path(active_root, payload.session_id)
    if not path.is_file():
        return None
    ensure_private_root(active_root)

    def update(values: dict[str, JsonValue]) -> tuple[bool, bool] | None:
        if values.get("enabled") is not True:
            return None
        revision = values.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        transcript = safe_transcript_path(payload.transcript_path, active_root)
        if payload.transcript_path is not None and transcript != values.get("transcript_path"):
            return None
        should_launch = apply_hook_event(values, payload, _utc_now(), path)
        values["revision"] = revision + 1
        stop_continuation = (
            payload.event is HookEvent.STOP
            and values.get("observe_only") is not True
            and values.get("managed_mode") is not True
        )
        return should_launch, stop_continuation

    result = mutate_existing_state(active_root, path, update)
    if result is None:
        return None
    should_launch, stop_continuation = result
    if should_launch:
        _launch_watcher()
    if stop_continuation:
        return StopContinuation(_stop_reason(active_root, path))
    return None


def _stop_reason(root: Path, runtime: Path) -> str:
    preset = load_state(root, runtime).values.get("message_preset")
    if preset == "continue":
        return (
            "Continue the same monitored task. If every success criterion is verified, "
            "invoke $work-off as a verified-completion shutdown before the final answer."
        )
    if preset == "cleanup":
        return (
            "Check whether task-owned runtime work is merely left open, clean it up safely, "
            "and continue the same task. If every success criterion is verified, invoke "
            "$work-off as a verified-completion shutdown before the final answer."
        )
    raise CorruptStateError(config_path(root), CorruptReason.INVALID_VALUE)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _main() -> int:
    plugin_data = os.environ.get("PLUGIN_DATA")
    if not plugin_data:
        _ = sys.stderr.write("PLUGIN_DATA is required for Codex Must Work hooks\n")
        return 1
    try:
        locator = process_hook(sys.stdin.read(), plugin_data=Path(plugin_data))
    except json.JSONDecodeError as error:
        _ = sys.stderr.write(f"invalid hook JSON: {error.msg}\n")
        return 1
    match locator:
        case SessionLocator():
            _ = sys.stdout.write(serialize_locator(locator) + "\n")
        case StopContinuation():
            _ = sys.stdout.write(serialize_stop_continuation(locator) + "\n")
        case None:
            pass
        case _:
            assert_never(locator)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
