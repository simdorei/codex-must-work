from __future__ import annotations

from datetime import timedelta

from scripts.calibration import (
    CalibrationInsufficient,
    CalibrationRecommendation,
    recommend_thresholds,
)


def test_recommend_thresholds_when_samples_are_sufficient_uses_percentiles() -> None:
    # Given: twenty valid gaps with two slower tail observations.
    gaps = [timedelta(seconds=60)] * 18
    gaps.extend((timedelta(seconds=121), timedelta(seconds=421)))

    # When: thresholds are calibrated.
    result = recommend_thresholds(gaps)

    # Then: P95 drives warning and max(P99, warning*2) drives restart.
    assert isinstance(result, CalibrationRecommendation)
    assert result.sample_count == 20
    assert result.warning_after_ms == 180_000
    assert result.restart_after_ms == 480_000


def test_recommend_thresholds_when_samples_are_insufficient_keeps_defaults() -> None:
    # Given: fewer than twenty valid progress gaps.
    gaps = [timedelta(seconds=30)] * 19

    # When: thresholds are calibrated.
    result = recommend_thresholds(gaps)

    # Then: no fabricated recommendation is returned.
    assert result == CalibrationInsufficient(sample_count=19)
