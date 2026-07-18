from __future__ import annotations

import pytest

from scripts.durations import (
    DurationParseError,
    Milliseconds,
    ThresholdOrderError,
    ThresholdValueError,
    parse_duration_ms,
    validate_thresholds,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90s", 90_000),
        ("2m", 120_000),
        ("0.5h", 1_800_000),
    ],
)
def test_parse_duration_accepts_one_explicit_unit(raw: str, expected: int) -> None:
    # Given / When
    result = parse_duration_ms(raw)

    # Then
    assert result == expected


@pytest.mark.parametrize(
    "raw",
    ["", "2", "0s", "-2m", "39m40s", "1h2m3s", "1m1h", "1.2m3s", "nanh"],
)
def test_parse_duration_rejects_invalid_or_compound_values(raw: str) -> None:
    # Given / When / Then
    with pytest.raises(DurationParseError):
        _ = parse_duration_ms(raw)


def test_parse_duration_rejects_sub_millisecond_values() -> None:
    # Given / When / Then
    with pytest.raises(DurationParseError):
        _ = parse_duration_ms("0.0001s")


@pytest.mark.parametrize(
    ("warning", "restart"),
    [
        (Milliseconds(0), Milliseconds(1)),
        (Milliseconds(-1), Milliseconds(1)),
        (Milliseconds(1), Milliseconds(0)),
    ],
)
def test_thresholds_require_positive_values(
    warning: Milliseconds,
    restart: Milliseconds,
) -> None:
    # Given / When / Then
    with pytest.raises(ThresholdValueError):
        _ = validate_thresholds(warning, restart)


@pytest.mark.parametrize(
    ("warning", "restart"),
    [
        (Milliseconds(90_000), Milliseconds(90_000)),
        (Milliseconds(120_000), Milliseconds(90_000)),
    ],
)
def test_restart_threshold_must_be_later(
    warning: Milliseconds,
    restart: Milliseconds,
) -> None:
    # Given / When / Then
    with pytest.raises(ThresholdOrderError):
        _ = validate_thresholds(warning, restart)
