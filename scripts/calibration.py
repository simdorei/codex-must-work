"""Calculate privacy-safe heartbeat threshold recommendations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import timedelta

from scripts.durations import Milliseconds

MIN_SAMPLE_COUNT: Final = 20
DEFAULT_WARNING_MS: Final = Milliseconds(10 * 60 * 1000)
DEFAULT_RESTART_MS: Final = Milliseconds(20 * 60 * 1000)


@unique
class CalibrationStatus(StrEnum):
    """Persisted first-thread calibration states."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CalibrationRecommendation:
    """Thresholds calculated from enough valid progress gaps."""

    sample_count: int
    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds


@dataclass(frozen=True, slots=True)
class CalibrationInsufficient:
    """A scan that did not contain enough valid gaps."""

    sample_count: int


@dataclass(frozen=True, slots=True)
class CalibrationUnavailable:
    """A scan that failed with a stable public-safe reason."""

    reason_code: str


type CalibrationOutcome = (
    CalibrationRecommendation | CalibrationInsufficient | CalibrationUnavailable
)


def recommend_thresholds(gaps: Sequence[timedelta]) -> CalibrationOutcome:
    """Return percentile thresholds or an insufficient-data result."""
    sample_count = len(gaps)
    if sample_count < MIN_SAMPLE_COUNT:
        return CalibrationInsufficient(sample_count=sample_count)
    seconds = sorted(gap.total_seconds() for gap in gaps)
    if any(value <= 0 for value in seconds):
        return CalibrationUnavailable(reason_code="nonpositive_progress_gap")
    warning_seconds = _rounded_minute(_nearest_rank(seconds, 0.95))
    restart_seconds = _rounded_minute(
        max(_nearest_rank(seconds, 0.99), warning_seconds * 2),
    )
    return CalibrationRecommendation(
        sample_count=sample_count,
        warning_after_ms=Milliseconds(warning_seconds * 1000),
        restart_after_ms=Milliseconds(restart_seconds * 1000),
    )


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return values[index]


def _rounded_minute(seconds: float) -> int:
    return max(60, math.ceil(seconds / 60) * 60)
