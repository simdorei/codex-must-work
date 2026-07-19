"""Stable public activation failure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override


@dataclass(frozen=True, slots=True)
class ActivationError(Exception):
    """Report activation failure with a public-safe reason."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return f"Codex Must Work was not enabled: {self.reason_code}"
