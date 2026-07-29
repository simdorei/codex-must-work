"""Persist user-selected warning and critical-stall thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final, final, override

from scripts.calibration import CalibrationStatus
from scripts.calibration_state import load_calibration_snapshot
from scripts.durations import (
    Milliseconds,
    ThresholdOrderError,
    ThresholdValueError,
    validate_thresholds,
)
from scripts.private_root import ensure_private_root
from scripts.state import (
    CorruptReason,
    CorruptStateError,
    StateDocument,
    load_state,
    save_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type ThresholdRecommendation = tuple[Milliseconds, Milliseconds]
type RecommendationProvider = Callable[[], ThresholdRecommendation | None]

DEFAULT_WARNING_MS: Final = Milliseconds(5 * 60 * 1000)
DEFAULT_CRITICAL_MS: Final = Milliseconds(10 * 60 * 1000)
_STATE_NAME: Final = "threshold-settings.json"
_RECOMMENDATION_UNAVAILABLE: Final = "threshold_recommendation_unavailable"
_ORDER_INVALID: Final = "threshold_order_invalid"


@unique
class ThresholdMode(StrEnum):
    """User-visible sources for the active threshold pair."""

    DEFAULT = "default"
    RECOMMENDED = "recommended"
    CUSTOM = "custom"


@unique
class ThresholdSettingsAction(StrEnum):
    """Supported MCP mutations for one threshold selection."""

    SHOW = "show"
    DEFAULT = "default"
    RECOMMENDED = "recommended"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class ThresholdSettingsSnapshot:
    """One validated threshold pair ready for monitoring."""

    mode: ThresholdMode
    warning_after_ms: Milliseconds
    critical_after_ms: Milliseconds


@dataclass(frozen=True, slots=True)
class ThresholdSettingsError(ValueError):
    """Expose a stable public reason for an unsupported settings request."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


@final
class ThresholdSettingsStore:
    """Read and atomically replace one private threshold selection."""

    def __init__(
        self,
        root: Path,
        *,
        recommendation: RecommendationProvider | None = None,
    ) -> None:
        """Bind state and an optional deterministic recommendation provider."""
        self._root = root
        self._recommendation = recommendation or self._stored_recommendation

    def load(self) -> ThresholdSettingsSnapshot:
        """Return the saved selection or the 5m/10m defaults."""
        path = self._path
        if not path.is_file():
            accepted = self._accepted_recommendation()
            return self._default_snapshot() if accepted is None else accepted
        values = load_state(self._root, path).values
        mode_value = values.get("mode")
        warning = values.get("warning_after_ms")
        critical = values.get("critical_after_ms")
        if not isinstance(mode_value, str) or type(warning) is not int or type(critical) is not int:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE)
        try:
            mode = ThresholdMode(mode_value)
            pair = validate_thresholds(Milliseconds(warning), Milliseconds(critical))
        except (ValueError, ThresholdOrderError, ThresholdValueError) as error:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from error
        return ThresholdSettingsSnapshot(
            mode,
            pair.warning_after_ms,
            pair.restart_after_ms,
        )

    def set_default(self) -> ThresholdSettingsSnapshot:
        """Persist the canonical 5m/10m selection."""
        return self._save(self._default_snapshot())

    def set_recommended(self) -> ThresholdSettingsSnapshot:
        """Copy the current local-history recommendation into settings."""
        recommendation = self._recommendation()
        if recommendation is None:
            raise ThresholdSettingsError(_RECOMMENDATION_UNAVAILABLE)
        warning, critical = recommendation
        return self._save(self._snapshot(ThresholdMode.RECOMMENDED, warning, critical))

    def set_custom(
        self,
        warning_after_ms: Milliseconds,
        critical_after_ms: Milliseconds,
    ) -> ThresholdSettingsSnapshot:
        """Persist one explicit positive ordered threshold pair."""
        return self._save(self._snapshot(ThresholdMode.CUSTOM, warning_after_ms, critical_after_ms))

    @property
    def _path(self) -> Path:
        return self._root / _STATE_NAME

    def _stored_recommendation(self) -> ThresholdRecommendation | None:
        snapshot = load_calibration_snapshot(self._root)
        if (
            snapshot is None
            or snapshot.warning_after_ms is None
            or snapshot.restart_after_ms is None
        ):
            return None
        return snapshot.warning_after_ms, snapshot.restart_after_ms

    def _accepted_recommendation(self) -> ThresholdSettingsSnapshot | None:
        snapshot = load_calibration_snapshot(self._root)
        if (
            snapshot is None
            or snapshot.status is not CalibrationStatus.ACCEPTED
            or snapshot.warning_after_ms is None
            or snapshot.restart_after_ms is None
        ):
            return None
        return self._snapshot(
            ThresholdMode.RECOMMENDED,
            snapshot.warning_after_ms,
            snapshot.restart_after_ms,
        )

    def _save(self, snapshot: ThresholdSettingsSnapshot) -> ThresholdSettingsSnapshot:
        ensure_private_root(self._root)
        save_state(
            self._root,
            self._path,
            StateDocument(
                values={
                    "mode": snapshot.mode.value,
                    "warning_after_ms": snapshot.warning_after_ms,
                    "critical_after_ms": snapshot.critical_after_ms,
                }
            ),
        )
        return snapshot

    @staticmethod
    def _default_snapshot() -> ThresholdSettingsSnapshot:
        return ThresholdSettingsSnapshot(
            ThresholdMode.DEFAULT,
            DEFAULT_WARNING_MS,
            DEFAULT_CRITICAL_MS,
        )

    @staticmethod
    def _snapshot(
        mode: ThresholdMode,
        warning_after_ms: Milliseconds,
        critical_after_ms: Milliseconds,
    ) -> ThresholdSettingsSnapshot:
        try:
            pair = validate_thresholds(warning_after_ms, critical_after_ms)
        except (ThresholdOrderError, ThresholdValueError) as error:
            raise ThresholdSettingsError(_ORDER_INVALID) from error
        return ThresholdSettingsSnapshot(
            mode,
            pair.warning_after_ms,
            pair.restart_after_ms,
        )
