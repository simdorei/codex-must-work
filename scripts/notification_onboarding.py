"""Claim the one-time post-install Discord setup suggestion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from scripts.notification_config import NotificationConfigStore
from scripts.private_root import ensure_private_root
from scripts.state import (
    SCHEMA_VERSION,
    CorruptReason,
    CorruptStateError,
    load_state,
)
from scripts.state_io import (
    ExclusiveWriteLock,
    atomic_json_write,
    prepare_parent_directories,
    safe_absolute_path,
)

if TYPE_CHECKING:
    from pathlib import Path

_STATE_NAME: Final = "notification-onboarding.json"


@unique
class NotificationOnboardingAction(StrEnum):
    """SessionStart guidance for optional Discord notifications."""

    OFFER_SETUP = "offer_setup"
    AVAILABLE = "available"
    CONFIGURED = "configured"


@dataclass(frozen=True, slots=True)
class NotificationOnboardingSnapshot:
    """One structural onboarding decision for injected session context."""

    action: NotificationOnboardingAction
    configured: bool


def claim_notification_onboarding(plugin_data: Path) -> NotificationOnboardingSnapshot:
    """Offer setup once, then keep the tool discoverable without repeated prompts."""
    store = NotificationConfigStore(plugin_data)
    if store.is_discord_configured():
        return NotificationOnboardingSnapshot(
            NotificationOnboardingAction.CONFIGURED,
            configured=True,
        )
    ensure_private_root(plugin_data)
    _, state_path = safe_absolute_path(plugin_data, plugin_data / _STATE_NAME)
    prepare_parent_directories(plugin_data, state_path)
    with ExclusiveWriteLock(state_path):
        if state_path.exists():
            document = load_state(plugin_data, state_path)
            if document.values.get("offered") is not True:
                raise CorruptStateError(state_path, CorruptReason.INVALID_VALUE)
            return NotificationOnboardingSnapshot(
                NotificationOnboardingAction.AVAILABLE,
                configured=False,
            )
        atomic_json_write(
            state_path,
            schema_version=SCHEMA_VERSION,
            values={"offered": True},
        )
    return NotificationOnboardingSnapshot(
        NotificationOnboardingAction.OFFER_SETUP,
        configured=False,
    )
