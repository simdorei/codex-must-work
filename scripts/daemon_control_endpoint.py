"""Expose one private, bounded loopback request path to the resident daemon."""

from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Final, Protocol, cast, final

from scripts.daemon_control_endpoint_connection import EndpointConnectionHandler
from scripts.daemon_control_endpoint_identity import current_process_created_ns
from scripts.daemon_control_endpoint_models import (
    EndpointDependencies,
    EndpointError,
    EndpointLocator,
    ServerFactory,
    ThreadLike,
)
from scripts.mcp_protocol import (
    DaemonBackend,
    encode_message,
)
from scripts.state_io import (
    JsonValue,
    atomic_json_write,
    ensure_direct_regular_file,
)

__all__ = (
    "ControlEndpoint",
    "EndpointDependencies",
    "EndpointError",
    "EndpointLocator",
    "control_endpoint_path",
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

_LOCATOR_NAME: Final = "control-endpoint.json"
_SCHEMA_VERSION: Final = 1
_IO_TIMEOUT_SECONDS: Final = 2.0
_ACCEPT_TIMEOUT_SECONDS: Final = 0.2
_SOCKET_ADDRESS_PARTS: Final = 2
_WORKER_FAILURES: Final = (Exception, KeyboardInterrupt, SystemExit, GeneratorExit)


class _JsonLoader(Protocol):
    def __call__(self, value: str) -> JsonValue: ...


def _json_loader(value: str) -> JsonValue:
    return cast("JsonValue", json.loads(value))


_LOAD_JSON: Final[_JsonLoader] = _json_loader


def control_endpoint_path(plugin_data: Path) -> Path:
    """Return the private per-install endpoint locator path."""
    return plugin_data / _LOCATOR_NAME


@final
class ControlEndpoint:
    """Own one sequential loopback listener and its exact locator generation."""

    def __init__(
        self,
        service: DaemonBackend,
        control_key: bytes,
        plugin_data: Path,
        server_factory: ServerFactory,
        dependencies: EndpointDependencies | None = None,
    ) -> None:
        """Prepare an inactive endpoint around the shared daemon."""
        self._service = service
        self._control_key = control_key
        self._plugin_data = plugin_data
        self._server_factory = server_factory
        self._connection_handler = EndpointConnectionHandler(
            service,
            control_key,
            server_factory,
        )
        self._dependencies = dependencies or EndpointDependencies()
        self._listener: socket.socket | None = None
        self._thread: ThreadLike | None = None
        self._thread_started = False
        self._locator: EndpointLocator | None = None
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._retired = False

    def __enter__(self) -> EndpointLocator:
        """Start and return the published generation."""
        return self.start()

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the exact endpoint generation."""
        self.close()

    def start(self) -> EndpointLocator:
        """Bind exactly loopback, publish one generation, and start one worker."""
        if self._locator is not None:
            return self._locator
        if self._retired:
            reason = "control_endpoint_closed"
            raise EndpointError(reason)
        self._closed.clear()
        self._ready.clear()
        locator_identity = current_process_created_ns()
        locator: EndpointLocator | None = None
        try:
            listener = self._dependencies.socket_factory(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.settimeout(_ACCEPT_TIMEOUT_SECONDS)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            address = cast("tuple[str, int]", listener.getsockname())
            if (
                type(address) is not tuple
                or len(address) < _SOCKET_ADDRESS_PARTS
                or type(address[1]) is not int
            ):
                reason = "control_endpoint_start_failed"
                raise EndpointError(reason)
            locator = EndpointLocator(
                pid=os.getpid(),
                process_created_ns=locator_identity,
                port=address[1],
                endpoint_nonce=self._dependencies.nonce_factory(32),
            )
            self._locator = locator
            thread = self._dependencies.thread_factory(
                self._serve,
                "cmw-control-endpoint",
                daemon=True,
            )
            self._thread = thread
            self._thread_started = True
            thread.start()
            if not self._ready.wait(_IO_TIMEOUT_SECONDS) or not thread.is_alive():
                reason = "control_endpoint_start_failed"
                raise EndpointError(reason)
            self._publish(locator)
        except _WORKER_FAILURES:
            self._rollback_start(locator)
            raise
        else:
            return locator

    def close(self) -> None:
        """Stop the exact listener generation and remove only its own locator."""
        locator = self._locator
        self._locator = None
        if locator is not None:
            self._retired = True
        self._closed.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            with suppress(*_WORKER_FAILURES):
                listener.close()
        thread = self._thread
        self._thread = None
        started = self._thread_started
        self._thread_started = False
        with suppress(*_WORKER_FAILURES):
            if (
                thread is not None
                and started
                and thread is not threading.current_thread()
                and thread.is_alive()
            ):
                thread.join(_IO_TIMEOUT_SECONDS + _ACCEPT_TIMEOUT_SECONDS)
        if locator is not None:
            with suppress(*_WORKER_FAILURES):
                self._remove_locator(locator)

    def _serve(self) -> None:
        self._ready.set()
        try:
            while not self._closed.is_set():
                listener = self._listener
                if listener is None:
                    return
                try:
                    connection = listener.accept()[0]
                except TimeoutError:
                    continue
                except OSError:
                    return
                with connection:
                    connection.settimeout(_IO_TIMEOUT_SECONDS)
                    locator = self._locator
                    if locator is None:
                        return
                    response = self._connection_handler.handle(connection, locator)
                    if response is None:
                        continue
                    try:
                        connection.sendall((encode_message(response) + "\n").encode("utf-8"))
                    except (OSError, TimeoutError):
                        continue
        except _WORKER_FAILURES:
            self.close()

    def _rollback_start(self, locator: EndpointLocator | None) -> None:
        """Rollback every partially acquired startup resource without masking failure."""
        self._locator = locator
        self.close()
        self._retired = False

    def _publish(self, locator: EndpointLocator) -> None:
        path = control_endpoint_path(self._plugin_data)
        ensure_direct_regular_file(self._plugin_data, path)
        values: dict[str, JsonValue] = {
            "pid": locator.pid,
            "process_created_ns": locator.process_created_ns,
            "port": locator.port,
            "endpoint_nonce": locator.endpoint_nonce,
        }
        atomic_json_write(path, schema_version=_SCHEMA_VERSION, values=values)

    def _remove_locator(self, locator: EndpointLocator) -> None:
        path = control_endpoint_path(self._plugin_data)
        try:
            ensure_direct_regular_file(self._plugin_data, path)
            decoded = _LOAD_JSON(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if (
            type(decoded) is dict
            and decoded.get("pid") == locator.pid
            and decoded.get("process_created_ns") == locator.process_created_ns
            and decoded.get("endpoint_nonce") == locator.endpoint_nonce
        ):
            path.unlink(missing_ok=True)
