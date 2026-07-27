"""Handle bounded loopback HTTP requests for Discord webhook setup."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Final, Protocol, final, override
from urllib.parse import parse_qs

from scripts.discord_webhook import DiscordWebhookError, parse_discord_webhook_url
from scripts.notification_setup_page import render_setup_page
from scripts.private_root import PrivateRootError
from scripts.state_io import StateError

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable
    from socketserver import BaseServer

    from scripts.notification_config import NotificationConfigStore

_HOST: Final = "127.0.0.1"
_MAX_FORM_BYTES: Final = 4_096
_TEST_MESSAGE: Final = "CMW Discord 알림 연결 테스트가 완료되었습니다."


class WebhookClient(Protocol):
    """Minimal Discord delivery surface needed by setup."""

    def send(self, content: str) -> str | None:
        """Send one test message."""
        ...


type WebhookClientFactory = Callable[[str], WebhookClient]


@dataclass(frozen=True, slots=True)
class _SetupContext:
    path: str
    netloc: str
    origin: str
    csrf_token: str
    store: NotificationConfigStore
    client_factory: WebhookClientFactory
    complete: Callable[[], None]


@final
class _SetupRequestHandler(BaseHTTPRequestHandler):
    """Handle setup requests without using the default URL logger."""

    server_version = "CMWSetup"
    sys_version = ""

    def __init__(
        self,
        context: _SetupContext,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
    ) -> None:
        self._context = context
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        """Render the setup form only at the active one-time path."""
        if not self._request_is_local():
            self._send_json(HTTPStatus.FORBIDDEN, "forbidden")
            return
        nonce = secrets.token_urlsafe(18)
        body = render_setup_page(self._context.csrf_token, nonce)
        self.send_response(HTTPStatus.OK)
        policy = "; ".join(
            (
                "default-src 'none'",
                "style-src 'unsafe-inline'",
                f"script-src 'nonce-{nonce}'",
                "connect-src 'self'",
                "form-action 'self'",
                "base-uri 'none'",
                "frame-ancestors 'none'",
            )
        )
        self._security_headers(policy)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    def do_POST(self) -> None:
        """Validate, test, and persist one webhook without reflecting it."""
        if not self._request_is_local() or self.headers.get("Origin") != self._context.origin:
            self._send_json(HTTPStatus.FORBIDDEN, "forbidden")
            return
        submission = self._read_submission()
        if submission is None:
            return
        csrf_token, webhook_url = submission
        if not secrets.compare_digest(csrf_token, self._context.csrf_token):
            self._send_json(HTTPStatus.FORBIDDEN, "forbidden")
            return
        try:
            validated = parse_discord_webhook_url(webhook_url)
            client = self._context.client_factory(str(validated))
            _ = client.send(_TEST_MESSAGE)
            self._context.store.save_discord_webhook(validated)
        except DiscordWebhookError:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                "Discord 연결을 확인하지 못했습니다. 웹훅 주소와 Discord 설정을 확인하세요.",
            )
            return
        except (OSError, PrivateRootError, StateError):
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "이 PC에 설정을 저장하지 못했습니다. Codex에서 설정 링크를 다시 열어주세요.",
            )
            return
        self._send_json(HTTPStatus.OK, status="connected")
        self._context.complete()

    @override
    def log_message(self, format: str, *args: str | float) -> None:
        """Suppress request logging so the setup path stays private."""
        _ = format, args

    def _request_is_local(self) -> bool:
        return self.path == self._context.path and self.headers.get("Host") == self._context.netloc

    def _read_submission(self) -> tuple[str, str] | None:
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "지원하지 않는 요청 형식입니다.")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            content_length = -1
        if content_length < 1 or content_length > _MAX_FORM_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "입력 길이를 확인하세요.")
            return None
        try:
            body = self.rfile.read(content_length).decode("utf-8")
            fields = parse_qs(
                body,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
            )
        except (UnicodeDecodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, "입력 내용을 확인하세요.")
            return None
        csrf_values = fields.get("csrf_token")
        webhook_values = fields.get("webhook_url")
        if (
            csrf_values is None
            or webhook_values is None
            or len(csrf_values) != 1
            or len(webhook_values) != 1
        ):
            self._send_json(HTTPStatus.BAD_REQUEST, "웹훅 주소를 다시 붙여넣으세요.")
            return None
        return csrf_values[0], webhook_values[0]

    def _send_json(
        self,
        status_code: HTTPStatus,
        message: str | None = None,
        *,
        status: str | None = None,
    ) -> None:
        payload = {"status": status} if status is not None else {"message": message or "error"}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self._security_headers("default-src 'none'; frame-ancestors 'none'")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    def _security_headers(self, content_security_policy: str) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", content_security_policy)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")


def create_setup_server(
    path: str,
    csrf_token: str,
    store: NotificationConfigStore,
    client_factory: WebhookClientFactory,
    complete: Callable[[], None],
) -> HTTPServer:
    """Bind one loopback HTTP server and inject its final origin into handlers."""
    server_holder: list[HTTPServer] = []

    def handler(
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
    ) -> _SetupRequestHandler:
        port = server_holder[0].server_address[1]
        netloc = f"{_HOST}:{port}"
        context = _SetupContext(
            path,
            netloc,
            f"http://{netloc}",
            csrf_token,
            store,
            client_factory,
            complete,
        )
        return _SetupRequestHandler(context, request, client_address, server)

    server = HTTPServer((_HOST, 0), handler)
    server_holder.append(server)
    return server
