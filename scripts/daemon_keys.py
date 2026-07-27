"""Build collision-free scheduler keys for daemon lifecycle work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scripts.daemon_scheduler import SchedulerKey

if TYPE_CHECKING:
    from scripts.app_server_protocol import AppServerActivity

MANAGER_KEY: Final = SchedulerKey("manager-drive")
RECONCILE_KEY: Final = SchedulerKey("watcher-reconcile")


def monitor_key(session_id: str) -> SchedulerKey:
    """Return one task threshold key."""
    return SchedulerKey(f"monitor:{session_id}")


def activation_key(session_id: str) -> SchedulerKey:
    """Return one task activation-fence key."""
    return SchedulerKey(f"activation:{session_id}")


def activity_key(activity: AppServerActivity) -> SchedulerKey:
    """Return one coalesced app-server activity key."""
    return SchedulerKey(f"activity:{activity.kind}:{activity.thread_id}:{activity.turn_id}")
