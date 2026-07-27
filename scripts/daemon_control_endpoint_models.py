"""Typed construction and lifecycle models for the private control endpoint."""

from __future__ import annotations

import secrets
import socket
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.mcp_protocol import DaemonBackend, JsonRpcResponse


class SocketFactory(Protocol):
    """Create the endpoint listener socket."""

    def __call__(self, family: int, kind: int) -> socket.socket:
        """Return one listener socket."""
        ...


class NonceFactory(Protocol):
    """Create a private generation nonce."""

    def __call__(self, byte_count: int) -> str:
        """Return a nonce with the requested entropy."""
        ...


class ServerSession(Protocol):
    """Handle one initialized MCP session."""

    def handle_line(self, raw_line: str) -> JsonRpcResponse | None:
        """Return a response when the request requires one."""
        ...


class ServerFactory(Protocol):
    """Create a fresh session around the resident daemon."""

    def __call__(self, service: DaemonBackend, control_key: bytes) -> ServerSession:
        """Return a fresh uninitialized MCP session."""
        ...


class ThreadLike(Protocol):
    """Minimal owned worker thread lifecycle."""

    def start(self) -> None:
        """Start the worker."""
        ...

    def join(self, timeout: float | None = None) -> None:
        """Wait at most the supplied duration."""
        ...

    def is_alive(self) -> bool:
        """Return whether the worker is running."""
        ...


class ThreadFactory(Protocol):
    """Construct an endpoint worker without starting it."""

    def __call__(
        self,
        target: Callable[[], None],
        name: str,
        *,
        daemon: bool,
    ) -> ThreadLike:
        """Return one unstarted worker."""
        ...


def socket_factory(family: int, kind: int) -> socket.socket:
    """Create a native TCP socket."""
    return socket.socket(family, kind)


def nonce_factory(byte_count: int) -> str:
    """Create a cryptographically random URL-safe nonce."""
    return secrets.token_urlsafe(byte_count)


def thread_factory(
    target: Callable[[], None],
    name: str,
    *,
    daemon: bool,
) -> threading.Thread:
    """Construct a native worker thread."""
    return threading.Thread(target=target, name=name, daemon=daemon)


@dataclass(frozen=True, slots=True)
class EndpointDependencies:
    """Inject socket and randomness boundaries for deterministic tests."""

    socket_factory: SocketFactory = socket_factory
    nonce_factory: NonceFactory = nonce_factory
    thread_factory: ThreadFactory = thread_factory


@dataclass(frozen=True, slots=True)
class EndpointLocator:
    """Public-safe coordinates for one exact endpoint generation."""

    pid: int
    process_created_ns: int
    port: int
    endpoint_nonce: str


@final
class EndpointError(RuntimeError):
    """Report a public-safe endpoint lifecycle failure."""

    def __init__(self, reason_code: str) -> None:
        """Retain only one stable public reason."""
        super().__init__(reason_code)
        self.reason_code = reason_code
