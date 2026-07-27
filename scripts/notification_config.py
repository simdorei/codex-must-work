"""Persist optional notification secrets inside the private plugin data root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final, override

from scripts.discord_webhook import (
    DiscordWebhookError,
    DiscordWebhookUrl,
    parse_discord_webhook_url,
)
from scripts.private_root import ensure_private_root
from scripts.state import CorruptReason, CorruptStateError, StateDocument, load_state, save_state

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG_NAME: Final = "notifications.json"
_WEBHOOK_FIELD: Final = "discord_webhook_url"
_CONFIG_INVALID: Final = "discord_config_invalid"


@dataclass(frozen=True, slots=True)
class NotificationConfigError(RuntimeError):
    """Expose one safe configuration reason without retaining a webhook."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


@final
class NotificationConfigStore:
    """Read and atomically write one private Discord webhook configuration."""

    def __init__(self, plugin_data: Path) -> None:
        """Bind one private root and its fixed notification config path."""
        self._root = plugin_data
        self._path = plugin_data / _CONFIG_NAME

    def load_discord_webhook(self) -> DiscordWebhookUrl | None:
        """Return the configured validated webhook, if present."""
        ensure_private_root(self._root)
        if not self._path.exists():
            return None
        document = load_state(self._root, self._path)
        value = document.values.get(_WEBHOOK_FIELD)
        if type(value) is not str:
            raise CorruptStateError(self._path, CorruptReason.INVALID_VALUE)
        try:
            return parse_discord_webhook_url(value)
        except DiscordWebhookError as error:
            raise NotificationConfigError(_CONFIG_INVALID) from error

    def is_discord_configured(self) -> bool:
        """Report whether a valid webhook is available."""
        return self.load_discord_webhook() is not None

    def save_discord_webhook(self, webhook: DiscordWebhookUrl) -> None:
        """Atomically replace the private notification configuration."""
        ensure_private_root(self._root)
        save_state(
            self._root,
            self._path,
            StateDocument(values={_WEBHOOK_FIELD: str(webhook)}),
        )
