"""Strict duration parsing for heartbeat thresholds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, NewType, override

Milliseconds = NewType("Milliseconds", int)

_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<value>(?:0|[1-9]\d*)(?:\.\d+)?)(?P<unit>[smh])"
)
_UNIT_MILLISECONDS: Final[dict[str, Decimal]] = {
    "s": Decimal(1_000),
    "m": Decimal(60_000),
    "h": Decimal(3_600_000),
}


class DurationErrorReason(StrEnum):
    """Stable reasons why a duration cannot be parsed."""

    FORMAT = "format"
    POSITIVE = "positive"
    WHOLE_MILLISECONDS = "whole_milliseconds"


@dataclass(frozen=True, slots=True)
class DurationParseError(Exception):
    """Report invalid duration input without losing its failure category."""

    raw: str
    reason: DurationErrorReason

    @override
    def __str__(self) -> str:
        return f"invalid duration {self.raw!r}: {self.reason.value}"


@dataclass(frozen=True, slots=True)
class ThresholdOrderError(Exception):
    """Report a restart threshold that is not later than its warning."""

    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds

    @override
    def __str__(self) -> str:
        return (
            "restart threshold must be later than warning threshold: "
            f"warning={self.warning_after_ms}, restart={self.restart_after_ms}"
        )


@dataclass(frozen=True, slots=True)
class ThresholdValueError(Exception):
    """Report non-positive warning or restart thresholds."""

    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds

    @override
    def __str__(self) -> str:
        return (
            "thresholds must be positive: "
            f"warning={self.warning_after_ms}, restart={self.restart_after_ms}"
        )


@dataclass(frozen=True, slots=True)
class Thresholds:
    """A validated warning and restart threshold pair."""

    warning_after_ms: Milliseconds
    restart_after_ms: Milliseconds


def parse_duration_ms(raw: str) -> Milliseconds:
    """Parse a positive unit-suffixed duration into exact milliseconds."""
    match = _DURATION_PATTERN.fullmatch(raw)
    if match is None:
        raise DurationParseError(raw=raw, reason=DurationErrorReason.FORMAT)

    milliseconds = Decimal(match.group("value")) * _UNIT_MILLISECONDS[match.group("unit")]
    if milliseconds <= 0:
        raise DurationParseError(raw=raw, reason=DurationErrorReason.POSITIVE)
    if milliseconds != milliseconds.to_integral_value():
        raise DurationParseError(
            raw=raw,
            reason=DurationErrorReason.WHOLE_MILLISECONDS,
        )
    return Milliseconds(int(milliseconds))


def validate_thresholds(
    warning_after_ms: Milliseconds,
    restart_after_ms: Milliseconds,
) -> Thresholds:
    """Return positive thresholds only when restart occurs after warning."""
    if warning_after_ms <= 0 or restart_after_ms <= 0:
        raise ThresholdValueError(
            warning_after_ms=warning_after_ms,
            restart_after_ms=restart_after_ms,
        )
    if restart_after_ms <= warning_after_ms:
        raise ThresholdOrderError(
            warning_after_ms=warning_after_ms,
            restart_after_ms=restart_after_ms,
        )
    return Thresholds(
        warning_after_ms=warning_after_ms,
        restart_after_ms=restart_after_ms,
    )
