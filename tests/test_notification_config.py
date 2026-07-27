from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.discord_webhook import parse_discord_webhook_url
from scripts.notification_config import NotificationConfigStore
from scripts.state_io import UnsafeStatePathError

if TYPE_CHECKING:
    from pathlib import Path


def _webhook() -> str:
    return "https://discord.com/api/" + "webhooks/123456789/test-token-value"


def test_private_notification_config_round_trips_validated_webhook(tmp_path: Path) -> None:
    store = NotificationConfigStore(tmp_path / "plugin-data")
    webhook = parse_discord_webhook_url(_webhook())

    store.save_discord_webhook(webhook)

    assert store.load_discord_webhook() == webhook
    assert store.is_discord_configured() is True


def test_notification_config_rejects_redirected_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NotificationConfigStore(tmp_path / "plugin-data")
    webhook = parse_discord_webhook_url(_webhook())

    def reject(_root: Path, path: Path) -> None:
        raise UnsafeStatePathError(path.parent, path)

    monkeypatch.setattr("scripts.state.ensure_existing_components_are_direct", reject)

    with pytest.raises(UnsafeStatePathError):
        store.save_discord_webhook(webhook)


def test_missing_notification_config_is_disabled_without_creating_file(tmp_path: Path) -> None:
    root = tmp_path / "plugin-data"
    store = NotificationConfigStore(root)

    assert store.load_discord_webhook() is None
    assert store.is_discord_configured() is False
    assert not (root / "notifications.json").exists()
