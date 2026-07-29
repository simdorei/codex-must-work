"""Validate Discord webhook URLs and deliver bounded messages."""

from __future__ import annotations

import http.client
import json
import re
from typing import TYPE_CHECKING, Final, NewType, Protocol, final
from urllib.parse import urlsplit

from scripts.notifications import NotificationDeliveryError

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.state_io import JsonValue

DiscordWebhookUrl = NewType("DiscordWebhookUrl", str)
_ALLOWED_HOSTS: Final = frozenset({"discord.com", "discordapp.com"})
_WEBHOOK_PATH = re.compile(r"/api/webhooks/[1-9][0-9]{5,24}/[^/?#]{6,256}\Z")
_HTTP_PORT: Final = 443
_HTTP_TIMEOUT_SECONDS: Final = 5.0
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_CONTENT_CHARS: Final = 2_000
_CONTENT_TOO_LARGE: Final = "discord_content_too_large"
_REQUEST_FAILED: Final = "discord_request_failed"
_RESPONSE_TOO_LARGE: Final = "discord_response_too_large"
_RESPONSE_INVALID: Final = "discord_response_invalid"
_URL_INVALID: Final = "discord_webhook_url_invalid"


class _JsonLoader(Protocol):
    def __call__(self, s: bytes) -> JsonValue:
        """Decode one JSON value."""
        ...


_LOAD_JSON: _JsonLoader = json.loads


class _Response(Protocol):
    status: int

    def read(self, amount: int) -> bytes:
        """Read at most the requested response bytes."""
        ...


class _Connection(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        """Send one request."""
        ...

    def getresponse(self) -> _Response:
        """Return the remote response."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


@final
class _StdlibResponse:
    def __init__(self, response: http.client.HTTPResponse) -> None:
        self._response = response
        self.status = response.status

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)


@final
class _StdlibConnection:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._connection = http.client.HTTPSConnection(host, port, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self._connection.request(method, path, body, headers)

    def getresponse(self) -> _Response:
        return _StdlibResponse(self._connection.getresponse())

    def close(self) -> None:
        self._connection.close()


class DiscordWebhookError(NotificationDeliveryError):
    """Report a safe Discord configuration or delivery failure."""


@final
class DiscordWebhookClient:
    """Post to one validated official Discord webhook without redirects."""

    def __init__(
        self,
        webhook_url: str | DiscordWebhookUrl,
        *,
        connection_factory: Callable[[str, int, float], _Connection] | None = None,
    ) -> None:
        """Validate and retain only the official host and webhook path."""
        self._host, self._path = _parse_webhook_url(str(webhook_url))
        self._connection_factory = connection_factory or _open_connection

    def send(self, content: str) -> str | None:
        """Send one mention-disabled payload and return its acknowledged id."""
        if len(content) > _MAX_CONTENT_CHARS:
            raise DiscordWebhookError(_CONTENT_TOO_LARGE)
        body = json.dumps(
            {"allowed_mentions": {"parse": []}, "content": content},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self._connection_factory(
            self._host,
            _HTTP_PORT,
            _HTTP_TIMEOUT_SECONDS,
        )
        try:
            connection.request(
                "POST",
                f"{self._path}?wait=true",
                body,
                {
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "codex-must-work/discord-notifier",
                },
            )
            response = connection.getresponse()
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException, UnicodeError):
            raise DiscordWebhookError(_REQUEST_FAILED) from None
        finally:
            connection.close()
        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise DiscordWebhookError(_RESPONSE_TOO_LARGE)
        if response.status != http.client.OK:
            reason = f"discord_http_{response.status}"
            raise DiscordWebhookError(reason)
        try:
            decoded = _LOAD_JSON(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DiscordWebhookError(_RESPONSE_INVALID) from None
        if not isinstance(decoded, dict):
            raise DiscordWebhookError(_RESPONSE_INVALID)
        message_id = decoded.get("id")
        if message_id is not None and not isinstance(message_id, str):
            raise DiscordWebhookError(_RESPONSE_INVALID)
        return message_id


def parse_discord_webhook_url(webhook_url: str) -> DiscordWebhookUrl:
    """Parse one official Discord webhook into a branded secret value."""
    _ = _parse_webhook_url(webhook_url)
    return DiscordWebhookUrl(webhook_url)


def _parse_webhook_url(webhook_url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(webhook_url)
        port = parsed.port
    except ValueError:
        raise DiscordWebhookError(_URL_INVALID) from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, _HTTP_PORT}
        or parsed.query
        or parsed.fragment
        or _WEBHOOK_PATH.fullmatch(parsed.path) is None
    ):
        raise DiscordWebhookError(_URL_INVALID)
    return host, parsed.path


def _open_connection(host: str, port: int, timeout: float) -> _Connection:
    return _StdlibConnection(host, port, timeout)
