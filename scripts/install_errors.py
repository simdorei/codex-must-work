"""Shared typed failures for the trust-aware installer."""

from __future__ import annotations

from typing import override


class InstallPluginError(Exception):
    """Expose one stable installer failure code with optional safe detail."""

    __slots__: tuple[str, str] = ("detail", "reason_code")

    reason_code: str
    detail: str | None

    def __init__(self, reason_code: str, detail: str | None = None) -> None:
        """Store public-safe error context while leaving traceback state mutable."""
        super().__init__(reason_code, detail)
        self.reason_code = reason_code
        self.detail = detail

    @override
    def __str__(self) -> str:
        if self.detail is None:
            return self.reason_code
        return f"{self.reason_code}: {self.detail}"
