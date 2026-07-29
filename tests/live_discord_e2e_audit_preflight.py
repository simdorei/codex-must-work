"""Read-only Discord-to-Codex preflight parsing and policy gates."""
# ruff: noqa: EM101, TC001

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scripts.state_io import JsonValue
from tests.live_discord_e2e_audit_records import decode_json

_APPROVAL_FREE: Final = frozenset({"dontAsk", "bypassPermissions"})
_SAFE_GOALS: Final = frozenset({None, "complete"})


class PreflightError(RuntimeError):
    """Name one failed preflight gate without private details."""


@dataclass(frozen=True, slots=True)
class PreflightLocator:
    """Non-secret locator values retained only in memory."""

    session_id: str
    transcript_path: Path
    plugin_root: Path
    plugin_data: Path
    permission_mode: str
    package_digest_sha256: str


@dataclass(frozen=True, slots=True)
class PreflightSnapshot:
    """Machine observations needed for a pure preflight decision."""

    discord_thread_id: str
    codex_thread_id: str
    session_id: str
    locator_session_id: str
    locator_transcript_matches: bool
    actual_package_digest_sha256: str
    locator_package_digest_sha256: str
    expected_package_digest_sha256: str | None
    permission_mode: str
    cmw_authenticated: bool
    cmw_active: bool
    app_thread_id: str
    goal_status: str | None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Public-safe booleans and IDs emitted before Discord mutation."""

    discord_thread_id: str
    codex_thread_id: str
    session_id: str
    ready: bool
    exact_mapping: bool
    locator_matches: bool
    installed_sha_matches: bool
    approval_free: bool
    cmw_authenticated: bool
    cmw_inactive: bool
    app_thread_matches: bool
    goal_absent_or_complete: bool

    def public_values(self) -> dict[str, str | bool]:
        """Return no paths, capability, environment, tokens, or private text."""
        return {
            "discord_thread_id": self.discord_thread_id,
            "codex_thread_id": self.codex_thread_id,
            "session_id": self.session_id,
            "ready": self.ready,
            "exact_mapping": self.exact_mapping,
            "locator_matches": self.locator_matches,
            "installed_sha_matches": self.installed_sha_matches,
            "approval_free": self.approval_free,
            "cmw_authenticated": self.cmw_authenticated,
            "cmw_inactive": self.cmw_inactive,
            "app_thread_matches": self.app_thread_matches,
            "goal_absent_or_complete": self.goal_absent_or_complete,
        }


def evaluate_preflight(snapshot: PreflightSnapshot) -> PreflightResult:
    """Fail closed unless every read-only pre-send gate passes."""
    if snapshot.discord_thread_id != "1528639615592828980":
        raise PreflightError("discord_thread_not_allowlisted")
    if not snapshot.codex_thread_id or snapshot.codex_thread_id != snapshot.app_thread_id:
        raise PreflightError("app_server_thread_mismatch")
    if snapshot.locator_session_id != snapshot.session_id:
        raise PreflightError("locator_session_mismatch")
    if not snapshot.locator_transcript_matches:
        raise PreflightError("locator_transcript_mismatch")
    locator_matches = (
        snapshot.actual_package_digest_sha256 == snapshot.locator_package_digest_sha256
    )
    expected_matches = (
        snapshot.expected_package_digest_sha256 is None
        or snapshot.actual_package_digest_sha256 == snapshot.expected_package_digest_sha256
    )
    if not locator_matches or not expected_matches:
        raise PreflightError("installed_package_digest_mismatch")
    if snapshot.permission_mode not in _APPROVAL_FREE:
        raise PreflightError("approval_required")
    if not snapshot.cmw_authenticated:
        raise PreflightError("cmw_status_unauthenticated")
    if snapshot.cmw_active:
        raise PreflightError("cmw_active")
    if snapshot.goal_status not in _SAFE_GOALS:
        raise PreflightError("goal_not_absent_or_complete")
    return PreflightResult(
        discord_thread_id=snapshot.discord_thread_id,
        codex_thread_id=snapshot.codex_thread_id,
        session_id=snapshot.session_id,
        ready=True,
        exact_mapping=True,
        locator_matches=True,
        installed_sha_matches=True,
        approval_free=True,
        cmw_authenticated=True,
        cmw_inactive=True,
        app_thread_matches=True,
        goal_absent_or_complete=True,
    )


def load_mapping(database: Path, discord_thread_id: str) -> str:
    """Resolve one exact mapping through SQLite read-only mode."""
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT codex_thread_id FROM mirror_threads WHERE discord_thread_id = ?",
                (int(discord_thread_id),),
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError) as error:
        raise PreflightError("mapping_read_failed") from error
    if len(rows) != 1:
        raise PreflightError("mapping_not_unique")
    mapped = f"{rows[0][0]}"
    if not mapped:
        raise PreflightError("mapping_not_unique")
    return mapped


def load_preflight_locator(rollout: Path) -> PreflightLocator:
    """Find the exact SessionStart locator bound to the rollout session."""
    records = _load_json_lines(rollout)
    sessions = _session_ids(records)
    locators = _locator_candidates(records)
    if len(sessions) != 1:
        raise PreflightError("session_meta_invalid")
    if len(locators) != 1:
        raise PreflightError("locator_count_invalid")
    return _parse_locator(locators[0], sessions[0], rollout)


def _session_ids(records: tuple[dict[str, JsonValue], ...]) -> tuple[str, ...]:
    sessions: list[str] = []
    for record in records:
        payload = record.get("payload")
        session_id = payload.get("id") if isinstance(payload, dict) else None
        if record.get("type") == "session_meta" and isinstance(session_id, str):
            sessions.append(session_id)
    return tuple(sessions)


def _locator_candidates(
    records: tuple[dict[str, JsonValue], ...],
) -> tuple[dict[str, JsonValue], ...]:
    locators: list[dict[str, JsonValue]] = []
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "hook_completed":
            continue
        run = payload.get("run")
        if not isinstance(run, dict) or run.get("event_name") != "session_start":
            continue
        entries = run.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("kind") == "context":
                locator = _locator_from_text(entry.get("text"))
                if locator is not None:
                    locators.append(locator)
    return tuple(locators)


def _load_json_lines(path: Path) -> tuple[dict[str, JsonValue], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PreflightError("rollout_read_failed") from error
    records: list[dict[str, JsonValue]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = decode_json(line)
        except json.JSONDecodeError as error:
            raise PreflightError("rollout_malformed_json") from error
        if not isinstance(value, dict):
            raise PreflightError("rollout_record_invalid")
        records.append(value)
    return tuple(records)


def _locator_from_text(value: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = decode_json(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    locator = decoded.get("codex_must_work_locator")
    return locator if isinstance(locator, dict) else None


def _parse_locator(
    values: dict[str, JsonValue],
    session_id: str,
    rollout: Path,
) -> PreflightLocator:
    locator_session = values.get("session_id")
    transcript = values.get("transcript_path")
    plugin_root = values.get("plugin_root")
    plugin_data = values.get("plugin_data")
    permission = values.get("permission_mode")
    package_digest_sha256 = values.get("package_digest_sha256")
    if (
        not isinstance(locator_session, str)
        or not locator_session
        or not isinstance(transcript, str)
        or not transcript
        or not isinstance(plugin_root, str)
        or not plugin_root
        or not isinstance(plugin_data, str)
        or not plugin_data
        or not isinstance(permission, str)
        or not isinstance(package_digest_sha256, str)
        or not _is_sha256(package_digest_sha256)
    ):
        raise PreflightError("locator_invalid")
    if locator_session != session_id:
        raise PreflightError("locator_session_mismatch")
    transcript_path = Path(transcript).resolve()
    if transcript_path != rollout.resolve():
        raise PreflightError("locator_transcript_mismatch")
    return PreflightLocator(
        locator_session,
        transcript_path,
        Path(plugin_root).resolve(),
        Path(plugin_data).resolve(),
        permission,
        package_digest_sha256,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
