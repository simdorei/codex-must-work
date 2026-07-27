from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.discord_webhook import parse_discord_webhook_url
from scripts.notification_config import NotificationConfigStore
from scripts.notification_onboarding import (
    NotificationOnboardingAction,
    claim_notification_onboarding,
)

if TYPE_CHECKING:
    from pathlib import Path


def _webhook() -> str:
    return "https://discord.com/api/" + "webhooks/123456789/test-token-value"


def test_notification_onboarding_is_offered_once_then_remains_available(
    tmp_path: Path,
) -> None:
    plugin_data = tmp_path / "plugin-data"

    first = claim_notification_onboarding(plugin_data)
    second = claim_notification_onboarding(plugin_data)

    assert first.action is NotificationOnboardingAction.OFFER_SETUP
    assert second.action is NotificationOnboardingAction.AVAILABLE
    assert first.configured is False
    assert second.configured is False


def test_notification_onboarding_reports_configured_after_secure_save(tmp_path: Path) -> None:
    plugin_data = tmp_path / "plugin-data"
    store = NotificationConfigStore(plugin_data)
    store.save_discord_webhook(parse_discord_webhook_url(_webhook()))

    snapshot = claim_notification_onboarding(plugin_data)

    assert snapshot.action is NotificationOnboardingAction.CONFIGURED
    assert snapshot.configured is True
