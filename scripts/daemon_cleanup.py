"""Run bounded daemon cleanup without replacing the triggering failure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Callable

type CleanupStep = tuple[str, Callable[[], None]]


@final
@dataclass(frozen=True, slots=True)
class CleanupReport:
    """Fixed diagnostics from one exception-resilient cleanup pass."""

    diagnostics: tuple[str, ...]

    def annotate(self, primary: BaseException) -> None:
        """Attach bounded cleanup diagnostics without replacing the primary."""
        if self.diagnostics:
            primary.add_note(f"daemon_cleanup={','.join(self.diagnostics)}")


def run_cleanup(*steps: CleanupStep) -> CleanupReport:
    """Run every fixed cleanup step even when a prior step fails."""
    diagnostics: list[str] = []
    for label, step in steps:
        try:
            step()
        except BaseException:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            diagnostics.append(label)
    return CleanupReport(tuple(diagnostics))
