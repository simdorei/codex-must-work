"""Own the short-lived loopback listener used for Discord webhook setup."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, final

from scripts.notification_config import NotificationConfigStore
from scripts.notification_setup_http import (
    WebhookClient,
    WebhookClientFactory,
    create_setup_server,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.server import HTTPServer
    from pathlib import Path

_HOST: Final = "127.0.0.1"
_TTL_SECONDS: Final = 300
_REQUEST_TIMEOUT_SECONDS: Final = 0.2


class NotificationSetupLauncher(Protocol):
    """Start or reuse a local notification setup page."""

    def start(self) -> NotificationSetupLaunch:
        """Return only a loopback URL and its lifetime."""
        ...


@dataclass(frozen=True, slots=True)
class NotificationSetupLaunch:
    """Public MCP result fields for one local setup page."""

    setup_url: str
    expires_in_seconds: int


@final
class NotificationSetupCoordinator:
    """Own at most one temporary listener and worker thread."""

    def __init__(
        self,
        plugin_data: Path,
        *,
        client_factory: WebhookClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the private config root and optional test client factory."""
        self._store = NotificationConfigStore(plugin_data)
        self._client_factory = client_factory or _default_client
        self._clock = clock
        self._lock = threading.RLock()
        self._server: HTTPServer | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._deadline = 0.0
        self._launch: NotificationSetupLaunch | None = None

    def start(self) -> NotificationSetupLaunch:
        """Start or reuse the one active setup page."""
        with self._lock:
            if self.is_active() and self._launch is not None:
                return self._launch
            stop = threading.Event()
            path = f"/setup/{secrets.token_urlsafe(24)}"
            server = create_setup_server(
                path,
                secrets.token_urlsafe(32),
                self._store,
                self._client_factory,
                stop.set,
            )
            port = server.server_address[1]
            origin = f"http://{_HOST}:{port}"
            launch = NotificationSetupLaunch(f"{origin}{path}", _TTL_SECONDS)
            deadline = self._clock() + _TTL_SECONDS
            thread = threading.Thread(
                target=self._serve,
                args=(server, stop, deadline),
                name="cmw-notification-setup",
                daemon=True,
            )
            self._server = server
            self._stop = stop
            self._thread = thread
            self._deadline = deadline
            self._launch = launch
            thread.start()
            return launch

    def is_active(self) -> bool:
        """Report whether the current listener is alive and unexpired."""
        with self._lock:
            thread = self._thread
            return thread is not None and thread.is_alive() and self._clock() < self._deadline

    def close(self) -> None:
        """Stop and join the temporary listener without touching daemon tasks."""
        with self._lock:
            stop = self._stop
            thread = self._thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _serve(self, server: HTTPServer, stop: threading.Event, deadline: float) -> None:
        with server:
            while not stop.is_set() and self._clock() < deadline:
                server.timeout = _REQUEST_TIMEOUT_SECONDS
                server.handle_request()
        with self._lock:
            if self._server is server:
                self._server = None
                self._stop = None
                self._thread = None
                self._launch = None
                self._deadline = 0.0


def _default_client(webhook_url: str) -> WebhookClient:
    from scripts.discord_webhook import DiscordWebhookClient  # noqa: PLC0415

    return DiscordWebhookClient(webhook_url)
