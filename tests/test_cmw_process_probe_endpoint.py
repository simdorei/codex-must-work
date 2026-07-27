from __future__ import annotations

import json
import os
import socket
import threading
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from scripts.control_capability import derive_control_capability
from scripts.daemon_control_endpoint import (
    ControlEndpoint,
    EndpointLocator,
    control_endpoint_path,
)
from scripts.daemon_control_endpoint_identity import process_created_ns
from scripts.mcp_server import McpServer
from tests.cmw_process_probe_endpoint import (
    EndpointAttachError,
    EndpointClient,
    load_control_endpoint,
)
from tests.test_daemon_control_endpoint import FakeDaemon

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"k" * 32


class _InetAddress(Protocol):
    def getsockname(self) -> tuple[str, int]: ...


def _inet_port(listener: _InetAddress) -> int:
    return listener.getsockname()[1]


def test_client_attaches_to_exact_resident_identity_for_repeated_calls(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    capability = derive_control_capability(_KEY, "session-a")

    # When
    with ControlEndpoint(daemon, _KEY, tmp_path, McpServer) as published:
        locator = load_control_endpoint(tmp_path, published.pid)
        client = EndpointClient(locator)
        responses = tuple(
            client.call(
                "cmw.status",
                {
                    "session_id": "session-a",
                    "control_capability": capability,
                },
            )
            for _ in range(100)
        )

    # Then
    assert locator.process_created_ns == process_created_ns(locator.pid)
    assert len(responses) == 100
    assert daemon.calls == ["status"] * 100


@pytest.mark.parametrize("field", ["pid", "created", "port", "nonce", "schema"])
def test_client_rejects_stale_or_malformed_locator(
    tmp_path: Path,
    field: str,
) -> None:
    # Given
    with ControlEndpoint(FakeDaemon(), _KEY, tmp_path, McpServer) as locator:
        path = control_endpoint_path(tmp_path)
        values = cast(
            "dict[str, object]",
            json.loads(path.read_text(encoding="utf-8")),
        )
        if field == "pid":
            values["pid"] = 2_147_483_647
        elif field == "created":
            values["process_created_ns"] = cast("int", values["process_created_ns"]) + 1
        elif field == "port":
            values["port"] = 0
        elif field == "nonce":
            values["endpoint_nonce"] = ""
        else:
            values["schema_version"] = 2
        _ = path.write_text(json.dumps(values), encoding="utf-8")

        # When / Then
        with pytest.raises(EndpointAttachError):
            _ = load_control_endpoint(tmp_path, locator.pid)


def test_client_error_never_discloses_endpoint_or_session_secrets(tmp_path: Path) -> None:
    # Given
    capability = derive_control_capability(_KEY, "session-a")
    with ControlEndpoint(FakeDaemon(), _KEY, tmp_path, McpServer) as published:
        locator = load_control_endpoint(tmp_path, published.pid)
    client = EndpointClient(locator)

    # When / Then
    with pytest.raises(EndpointAttachError) as raised:
        _ = client.call(
            "cmw.status",
            {"session_id": "session-a", "control_capability": capability},
        )
    rendered = str(raised.value)
    assert capability not in rendered
    assert locator.endpoint_nonce not in rendered


def test_client_authenticates_server_before_sending_either_secret() -> None:
    # Given
    capability = derive_control_capability(_KEY, "session-a")
    endpoint_nonce = "n" * 43
    captured: list[bytes] = []
    ready = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = _inet_port(listener)

        def serve_rogue() -> None:
            ready.set()
            connection = listener.accept()[0]
            with connection:
                captured.append(connection.makefile("rb").readline())
                connection.sendall(b'{"schema_version":1,"server_proof":"wrong"}\n')

        worker = threading.Thread(target=serve_rogue)
        worker.start()
        assert ready.wait(1.0)
        locator = EndpointLocator(
            pid=os.getpid(),
            process_created_ns=process_created_ns(os.getpid()),
            port=port,
            endpoint_nonce=endpoint_nonce,
        )

        # When / Then
        with pytest.raises(EndpointAttachError):
            _ = EndpointClient(locator).call(
                "cmw.status",
                {
                    "session_id": "session-a",
                    "control_capability": capability,
                },
            )
        worker.join(1.0)
    assert len(captured) == 1
    assert capability.encode() not in captured[0]
    assert endpoint_nonce.encode() not in captured[0]
