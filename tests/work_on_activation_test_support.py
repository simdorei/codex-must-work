from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scripts.control_capability import derive_control_capability
from scripts.work_on_activation import ActivationIdentity, ActivationTicketStore

if TYPE_CHECKING:
    from pathlib import Path

TEST_KEY: Final = b"a" * 32


@dataclass(frozen=True, slots=True)
class ActivationFixture:
    root: Path

    @property
    def identity(self) -> ActivationIdentity:
        return ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl")

    @property
    def capability(self) -> str:
        return derive_control_capability(TEST_KEY, "session-a")

    def store(self) -> ActivationTicketStore:
        return ActivationTicketStore(
            self.root / "plugin-data",
            TEST_KEY,
            clock=lambda: 1_000.0,
        )
