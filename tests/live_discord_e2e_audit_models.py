"""Public-safe value models for the Discord continuation audit."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """Bind the allowlisted Discord thread and unique E2E marker."""

    discord_thread_id: str
    marker: str
    discord_bot_author_id: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Public-safe identities proving one automatic continuation."""

    session_id: str
    codex_thread_id: str
    activation_turn_id: str
    automatic_turn_id: str
    rollout_visible_item_id: str
    rollout_final_item_id: str
    discord_user_message_id: str
    discord_bot_message_id: str
    intervening_user_events: int

    def public_values(self) -> dict[str, str | int | bool]:
        """Return only public IDs, counts, and booleans."""
        return {
            "passed": True,
            "session_id": self.session_id,
            "codex_thread_id": self.codex_thread_id,
            "activation_turn_id": self.activation_turn_id,
            "automatic_turn_id": self.automatic_turn_id,
            "rollout_visible_item_id": self.rollout_visible_item_id,
            "rollout_final_item_id": self.rollout_final_item_id,
            "discord_user_message_id": self.discord_user_message_id,
            "discord_bot_message_id": self.discord_bot_message_id,
            "intervening_user_events": self.intervening_user_events,
        }
