"""Safe input and evidence-output boundaries for the process probe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, override

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

_MAX_ROLLOUT_LINE_BYTES: Final = 1_048_576
_LINE_TOO_LARGE: Final = "rollout_line_too_large"
_LOCATOR_MISSING: Final = "missing"
_SESSION_MISMATCH: Final = "session_mismatch"
_ROLLOUT_MISMATCH: Final = "rollout_mismatch"
_ROOT_INVALID: Final = "installed_root_invalid"


class _JsonLoader(Protocol):
    def __call__(self, value: str) -> JsonValue: ...


def _json_loader(value: str) -> JsonValue:
    return cast("JsonValue", json.loads(value))


_LOAD_JSON: Final[_JsonLoader] = _json_loader


@dataclass(frozen=True, slots=True)
class OutputPathError(Exception):
    path: Path
    reason: str

    @override
    def __str__(self) -> str:
        return f"invalid output path ({self.reason}): {self.path}"


@dataclass(frozen=True, slots=True)
class LocatorError(Exception):
    reason: str

    @override
    def __str__(self) -> str:
        return f"session locator unavailable: {self.reason}"


@dataclass(frozen=True, slots=True)
class SessionLocator:
    session_id: str
    transcript_path: Path
    plugin_root: Path
    plugin_data: Path
    permission_mode: str | None = None


def parse_output_path(raw: Path) -> Path:
    """Require a new JSON file under a real directory, without overwriting evidence."""
    if not raw.is_absolute():
        raise OutputPathError(raw, "absolute_path_required")
    path = raw.resolve(strict=False)
    if path.suffix.lower() != ".json":
        raise OutputPathError(path, "json_suffix_required")
    if not path.parent.is_dir():
        raise OutputPathError(path, "parent_missing")
    if path.exists():
        raise OutputPathError(path, "already_exists")
    return path


def load_session_locator(rollout: Path, session_id: str) -> SessionLocator:
    """Read the exact non-secret hook locator into memory."""
    resolved_rollout = rollout.resolve(strict=True)
    locator: SessionLocator | None = None
    with resolved_rollout.open(encoding="utf-8") as stream:
        for raw_line in stream:
            if len(raw_line.encode("utf-8")) > _MAX_ROLLOUT_LINE_BYTES:
                raise LocatorError(_LINE_TOO_LARGE)
            candidate = _locator_from_record(raw_line)
            if candidate is not None:
                locator = candidate
    if locator is None:
        raise LocatorError(_LOCATOR_MISSING)
    if locator.session_id != session_id:
        raise LocatorError(_SESSION_MISMATCH)
    if locator.transcript_path.resolve(strict=False) != resolved_rollout:
        raise LocatorError(_ROLLOUT_MISMATCH)
    _require_installed_root(locator.plugin_root)
    return locator


def _locator_from_record(  # noqa: PLR0911 - each guard rejects one untrusted shape.
    raw_line: str,
) -> SessionLocator | None:
    try:
        decoded = _LOAD_JSON(raw_line)
    except json.JSONDecodeError:
        return None
    if type(decoded) is not dict:
        return None
    payload = decoded.get("payload")
    if type(payload) is not dict or payload.get("type") != "hook_completed":
        return None
    run = payload.get("run")
    if type(run) is not dict or run.get("event_name") != "session_start":
        return None
    entries = run.get("entries")
    if type(entries) is not list:
        return None
    for entry in entries:
        if type(entry) is not dict or entry.get("kind") != "context":
            continue
        text = entry.get("text")
        if type(text) is not str:
            continue
        candidate = _locator_from_context(text)
        if candidate is not None:
            return candidate
    return None


def _locator_from_context(text: str) -> SessionLocator | None:
    try:
        decoded = _LOAD_JSON(text)
    except json.JSONDecodeError:
        return None
    if type(decoded) is not dict:
        return None
    raw = decoded.get("codex_must_work_locator")
    if type(raw) is not dict:
        return None
    session_id = raw.get("session_id")
    transcript = raw.get("transcript_path")
    plugin_root = raw.get("plugin_root")
    plugin_data = raw.get("plugin_data")
    permission = raw.get("permission_mode")
    if not (
        type(session_id) is str
        and session_id
        and type(transcript) is str
        and transcript
        and type(plugin_root) is str
        and plugin_root
        and type(plugin_data) is str
        and plugin_data
    ):
        return None
    if permission is not None and type(permission) is not str:
        return None
    return SessionLocator(
        session_id=session_id,
        transcript_path=Path(transcript),
        plugin_root=Path(plugin_root).resolve(strict=False),
        plugin_data=Path(plugin_data).resolve(strict=False),
        permission_mode=permission,
    )


def _require_installed_root(root: Path) -> None:
    required = (
        root / ".codex-plugin" / "plugin.json",
        root / ".mcp.json",
        root / "scripts" / "mcp_server.py",
    )
    if not root.is_absolute() or not all(path.is_file() for path in required):
        raise LocatorError(_ROOT_INVALID)
