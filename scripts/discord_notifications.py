"""Format and route CMW lifecycle notifications to Discord."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Protocol, assert_never, final

from scripts.agent_identity import AgentIdentityResolver
from scripts.discord_webhook import (
    DiscordWebhookClient,
    DiscordWebhookError,
)
from scripts.notifications import (
    LifecycleNotification,
    NotificationKind,
    NotificationSink,
    NotificationSubject,
    NullNotificationSink,
)
from scripts.state import StateError
from scripts.thread_title import ThreadTitleResolver
from scripts.threshold_settings import DEFAULT_CRITICAL_MS, DEFAULT_WARNING_MS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CMW_DISCORD_WEBHOOK_ENV: Final = "CMW_DISCORD_WEBHOOK_URL"
_MAX_TITLE_CHARS: Final = 180
_MAX_SESSION_ID_CHARS: Final = 180
_MAX_AGENT_CHARS: Final = 180
_URL_INVALID: Final = "discord_webhook_url_invalid"
_CONFIG_UNAVAILABLE: Final = "discord_config_unavailable"


class _WebhookClient(Protocol):
    def send(self, content: str) -> str | None:
        """Send one message and return its Discord message id."""
        ...


@final
class DiscordNotificationSink:
    """Resolve the local title and deliver one bounded lifecycle message."""

    def __init__(
        self,
        client: _WebhookClient,
        title_lookup: Callable[[str], str | None],
        agent_lookup: Callable[[NotificationSubject], str] | None = None,
    ) -> None:
        """Bind delivery to an injected client and read-only lookups."""
        self._client = client
        self._title_lookup = title_lookup
        self._agent_lookup = agent_lookup or AgentIdentityResolver().resolve

    def notify(self, event: LifecycleNotification) -> None:
        """Send one lifecycle message with mentions disabled by the client."""
        title = self._title_lookup(event.session_id)
        agent = self._agent_lookup(event.subject)
        _ = self._client.send(_format_message(event, title, agent))


def notification_sink_from_environment() -> NotificationSink:
    """Create a Discord sink only when the inherited environment opts in."""
    webhook_url = os.environ.get(CMW_DISCORD_WEBHOOK_ENV)
    if webhook_url is None:
        return NullNotificationSink()
    if not webhook_url.strip():
        raise DiscordWebhookError(_URL_INVALID)
    resolver = ThreadTitleResolver()
    return DiscordNotificationSink(
        DiscordWebhookClient(webhook_url),
        resolver.resolve,
    )


@final
class ConfiguredDiscordNotificationSink:
    """Load private configuration only when a lifecycle event needs delivery."""

    def __init__(self, plugin_data: Path) -> None:
        """Bind the private plugin data root without caching its secret."""
        self._plugin_data = plugin_data
        self._title_resolver = ThreadTitleResolver()
        self._agent_resolver = AgentIdentityResolver()

    def notify(self, event: LifecycleNotification) -> None:
        """Resolve current environment or private configuration and deliver once."""
        webhook_url = os.environ.get(CMW_DISCORD_WEBHOOK_ENV)
        if webhook_url is None:
            from scripts.notification_config import (  # noqa: PLC0415
                NotificationConfigError,
                NotificationConfigStore,
            )

            try:
                configured = NotificationConfigStore(self._plugin_data).load_discord_webhook()
            except NotificationConfigError as error:
                raise DiscordWebhookError(error.reason_code) from error
            except (OSError, StateError) as error:
                raise DiscordWebhookError(_CONFIG_UNAVAILABLE) from error
            if configured is None:
                return
            webhook_url = str(configured)
        if not webhook_url.strip():
            raise DiscordWebhookError(_URL_INVALID)
        DiscordNotificationSink(
            DiscordWebhookClient(webhook_url),
            self._title_resolver.resolve,
            self._agent_resolver.resolve,
        ).notify(event)


def notification_sink_from_configuration(plugin_data: Path | None) -> NotificationSink:
    """Use dynamic private configuration when plugin data is available."""
    if plugin_data is None:
        return notification_sink_from_environment()
    return ConfiguredDiscordNotificationSink(plugin_data)


def _format_message(
    event: LifecycleNotification,
    title: str | None,
    agent_display: str,
) -> str:
    display_title = _display_title(title)
    status, detail = _status_detail(event)
    display_session_id = _display_session_id(event.session_id)
    display_agent = _display_agent(agent_display)
    return (
        f"{status}\n"
        f"스레드: {display_title}\n"
        f"Codex 스레드 ID: {display_session_id}\n"
        f"대상: {display_agent}\n"
        f"{detail}"
    )


def _status_detail(event: LifecycleNotification) -> tuple[str, str]:
    match event.kind:
        case NotificationKind.BOTTLENECK_SUSPECTED:
            duration_ms = (
                int(DEFAULT_WARNING_MS) if event.threshold_ms is None else event.threshold_ms
            )
            return (
                "CMW 병목 의심",
                f"{_format_duration_ms(duration_ms)} 동안 관찰 가능한 진행이 없습니다.",
            )
        case NotificationKind.BOTTLENECK_CRITICAL:
            duration_ms = (
                int(DEFAULT_CRITICAL_MS) if event.threshold_ms is None else event.threshold_ms
            )
            return (
                "🚨 CMW 심각 정체",
                f"{_format_duration_ms(duration_ms)} 동안 관찰 가능한 진행이 없습니다.",
            )
        case NotificationKind.PROGRESS_RECOVERED:
            return (
                "✅ CMW 진행 회복",
                "관찰 가능한 진행 신호가 다시 확인되었습니다.",
            )
        case NotificationKind.COMPLETED:
            return (
                "🏁 CMW 정상 완료",
                "정상 완료가 확인되어 감시를 종료했습니다.",
            )
        case _:
            assert_never(event.kind)


def _format_duration_ms(duration_ms: int) -> str:
    """Render a configured duration without losing sub-second precision."""
    duration = max(0, duration_ms)
    seconds, milliseconds = divmod(duration, 1000)
    if milliseconds == 0:
        return f"{seconds}초"
    if seconds == 0:
        return f"{milliseconds}밀리초"
    fraction = f"{milliseconds:03d}".rstrip("0")
    return f"{seconds}.{fraction}초"


def _display_title(title: str | None) -> str:
    if title is None:
        return "제목 조회 실패"
    normalized = " ".join(title.split())
    if not normalized:
        return "제목 조회 실패"
    if len(normalized) <= _MAX_TITLE_CHARS:
        return normalized
    return f"{normalized[: _MAX_TITLE_CHARS - 1]}…"


def _display_session_id(session_id: str) -> str:
    """Keep the opaque thread ID on one bounded Discord line."""
    normalized = " ".join(session_id.split())
    if len(normalized) <= _MAX_SESSION_ID_CHARS:
        return normalized
    return f"{normalized[: _MAX_SESSION_ID_CHARS - 1]}…"


def _display_agent(agent_display: str) -> str:
    """Keep the resolved local agent label on the target line."""
    normalized = " ".join(agent_display.split())
    if not normalized:
        return "대상 확인 실패"
    if len(normalized) <= _MAX_AGENT_CHARS:
        return normalized
    return f"{normalized[: _MAX_AGENT_CHARS - 1]}…"
