"""Persist one calibration decision per installed plugin version."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Never, Protocol, assert_never, override

from scripts.calibration import (
    CalibrationInsufficient,
    CalibrationOutcome,
    CalibrationRecommendation,
    CalibrationStatus,
    CalibrationUnavailable,
)
from scripts.durations import Milliseconds
from scripts.private_root import ensure_private_root
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    JsonValue,
    StateDocument,
    load_state,
    save_state,
)
from scripts.state_io import ExclusiveWriteLock, StateError

_STATE_NAME: Final = "calibration.json"
_OPERATION_NAME: Final = "calibration-operation"


class _JsonLoader(Protocol):
    def __call__(
        self,
        s: str,
        *,
        parse_constant: Callable[[str], Never],
    ) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class CalibrationEnvironment:
    """Paths and clock used to prepare first-thread calibration state."""

    state_root: Path
    plugin_root: Path
    codex_home: Path
    now: datetime


@dataclass(frozen=True, slots=True)
class CalibrationSnapshot:
    """Machine-consumed calibration context injected into a thread."""

    plugin_version: str
    status: CalibrationStatus
    sample_count: int
    warning_after_ms: Milliseconds | None
    restart_after_ms: Milliseconds | None
    reason_code: str | None = None
    should_announce: bool = False


@unique
class CalibrationDecision(StrEnum):
    """Explicit answers accepted by the calibration command."""

    ACCEPT = "accept"
    REJECT = "reject"


type HistoryScanner = Callable[[Path, datetime], CalibrationOutcome]


@dataclass(frozen=True, slots=True)
class CalibrationStateError(StateError):
    """Report a public-safe calibration state or decision failure."""

    path: Path
    reason_code: str

    @override
    def __str__(self) -> str:
        return f"calibration state rejected at {self.path}: {self.reason_code}"


def load_or_calibrate(
    environment: CalibrationEnvironment,
    scanner: HistoryScanner,
) -> CalibrationSnapshot:
    """Reuse this version's decision or perform its one bounded scan."""
    ensure_private_root(environment.state_root)
    path = _state_path(environment.state_root)
    with ExclusiveWriteLock(_operation_path(environment.state_root), timeout_seconds=55.0):
        version = _plugin_version(environment.plugin_root)
        if path.is_file():
            stored = _load_snapshot(environment.state_root, path)
            if stored.plugin_version == version:
                return stored
        snapshot = _snapshot_from_outcome(
            version,
            scanner(environment.codex_home, environment.now),
        )
        _save_snapshot(environment.state_root, path, snapshot)
        return snapshot


def record_decision(
    root: Path,
    plugin_version: str,
    decision: CalibrationDecision,
) -> CalibrationSnapshot:
    """Persist an explicit answer for the current pending recommendation."""
    ensure_private_root(root)
    path = _state_path(root)
    with ExclusiveWriteLock(_operation_path(root), timeout_seconds=55.0):
        snapshot = _load_snapshot(root, path)
        if snapshot.plugin_version != plugin_version:
            raise CalibrationStateError(path, "plugin_version_changed")
        if snapshot.status is not CalibrationStatus.PENDING:
            raise CalibrationStateError(path, "recommendation_not_pending")
        match decision:
            case CalibrationDecision.ACCEPT:
                status = CalibrationStatus.ACCEPTED
            case CalibrationDecision.REJECT:
                status = CalibrationStatus.REJECTED
            case _:
                assert_never(decision)
        decided = CalibrationSnapshot(
            plugin_version=snapshot.plugin_version,
            status=status,
            sample_count=snapshot.sample_count,
            warning_after_ms=snapshot.warning_after_ms,
            restart_after_ms=snapshot.restart_after_ms,
        )
        _save_snapshot(root, path, decided)
        return decided


def _snapshot_from_outcome(version: str, outcome: CalibrationOutcome) -> CalibrationSnapshot:
    match outcome:
        case CalibrationRecommendation(sample_count, warning, restart):
            return CalibrationSnapshot(
                plugin_version=version,
                status=CalibrationStatus.PENDING,
                sample_count=sample_count,
                warning_after_ms=warning,
                restart_after_ms=restart,
                should_announce=True,
            )
        case CalibrationInsufficient(sample_count):
            return CalibrationSnapshot(
                plugin_version=version,
                status=CalibrationStatus.INSUFFICIENT,
                sample_count=sample_count,
                warning_after_ms=None,
                restart_after_ms=None,
                should_announce=True,
            )
        case CalibrationUnavailable(reason_code):
            return CalibrationSnapshot(
                plugin_version=version,
                status=CalibrationStatus.UNAVAILABLE,
                sample_count=0,
                warning_after_ms=None,
                restart_after_ms=None,
                reason_code=reason_code,
                should_announce=True,
            )
        case _:
            assert_never(outcome)


def _plugin_version(plugin_root: Path) -> str:
    path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        decoded = _LOAD_JSON(path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CalibrationStateError(path, "plugin_manifest_unreadable") from error
    if type(decoded) is not dict:
        raise CalibrationStateError(path, "plugin_manifest_invalid")
    version = decoded.get("version")
    if not isinstance(version, str) or not version:
        raise CalibrationStateError(path, "plugin_version_missing")
    return version


def _state_path(root: Path) -> Path:
    return root / _STATE_NAME


def _operation_path(root: Path) -> Path:
    return root / _OPERATION_NAME


def _save_snapshot(root: Path, path: Path, snapshot: CalibrationSnapshot) -> None:
    save_state(
        root,
        path,
        StateDocument(
            values={
                "plugin_version": snapshot.plugin_version,
                "status": snapshot.status.value,
                "sample_count": snapshot.sample_count,
                "warning_after_ms": snapshot.warning_after_ms,
                "restart_after_ms": snapshot.restart_after_ms,
                "reason_code": snapshot.reason_code,
            }
        ),
    )


def _load_snapshot(root: Path, path: Path) -> CalibrationSnapshot:
    values = load_state(root, path).values
    version = values.get("plugin_version")
    status_value = values.get("status")
    sample_count = values.get("sample_count")
    warning = values.get("warning_after_ms")
    restart = values.get("restart_after_ms")
    reason = values.get("reason_code")
    if (
        not isinstance(version, str)
        or not isinstance(status_value, str)
        or type(sample_count) is not int
        or sample_count < 0
        or (warning is not None and type(warning) is not int)
        or (restart is not None and type(restart) is not int)
        or (reason is not None and not isinstance(reason, str))
    ):
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
    try:
        status = CalibrationStatus(status_value)
    except ValueError as error:
        raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from error
    return CalibrationSnapshot(
        plugin_version=version,
        status=status,
        sample_count=sample_count,
        warning_after_ms=Milliseconds(warning) if warning is not None else None,
        restart_after_ms=Milliseconds(restart) if restart is not None else None,
        reason_code=reason,
    )


def _reject_nonfinite(token: str) -> Never:
    message = f"non-finite number: {token}"
    raise json.JSONDecodeError(message, token, 0)
