from __future__ import annotations

import json
import os
import socket
import threading
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from scripts.daemon_control_endpoint import (
    ControlEndpoint,
    EndpointLocator,
    control_endpoint_path,
)
from scripts.daemon_control_endpoint_identity import process_created_ns
from scripts.mcp_server import McpServer
from scripts.work_on_activation import ActivationIdentity, ActivationTicketStore
from tests.cmw_process_probe_endpoint import (
    EndpointAttachError,
    EndpointClient,
    load_control_endpoint,
)
from tests.cmw_process_probe_io import SessionLocator
from tests.cmw_process_probe_live import LiveDependencies
from tests.test_daemon_control_endpoint import FakeDaemon

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"k" * 32


def _server_factory(plugin_data: Path) -> partial[McpServer]:
    return partial(
        McpServer,
        activation_tickets=ActivationTicketStore(plugin_data, _KEY),
    )


def test_live_probe_activation_uses_supported_work_on_contract_at_real_endpoint(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    session = "session-a"
    turn_id = "turn-a"
    plugin_data = tmp_path / "plugin-data"
    _ = ActivationTicketStore(plugin_data, _KEY).issue(
        ActivationIdentity(session, turn_id, str(tmp_path / "rollout.jsonl"))
    )
    locator_data = SessionLocator(
        session_id=session,
        transcript_path=tmp_path / "rollout.jsonl",
        plugin_root=tmp_path,
        plugin_data=plugin_data,
        permission_mode="dontAsk",
    )

    # When
    with ControlEndpoint(daemon, _KEY, plugin_data, _server_factory(plugin_data)) as published:
        endpoint = load_control_endpoint(plugin_data, published.pid)
        dependencies = LiveDependencies(
            published.pid,
            locator_data,
            EndpointClient(endpoint),
            0.01,
            tmp_path,
        )
        dependencies.authorize_start(turn_id)
        status = dependencies.control("start")

    # Then
    assert status == "started"
    assert daemon.calls == ["start"]


class _InetAddress(Protocol):
    def getsockname(self) -> tuple[str, int]: ...


def _inet_port(listener: _InetAddress) -> int:
    return listener.getsockname()[1]


def test_client_attaches_to_exact_resident_identity_for_repeated_calls(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    # When
    with ControlEndpoint(daemon, _KEY, tmp_path, _server_factory(tmp_path)) as published:
        locator = load_control_endpoint(tmp_path, published.pid)
        client = EndpointClient(locator)
        responses = tuple(
            client.call(
                "cmw.status",
                {
                    "session_id": "session-a",
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
    with ControlEndpoint(FakeDaemon(), _KEY, tmp_path, _server_factory(tmp_path)) as locator:
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
    with ControlEndpoint(FakeDaemon(), _KEY, tmp_path, _server_factory(tmp_path)) as published:
        locator = load_control_endpoint(tmp_path, published.pid)
    client = EndpointClient(locator)

    # When / Then
    with pytest.raises(EndpointAttachError) as raised:
        _ = client.call(
            "cmw.status",
            {"session_id": "session-a"},
        )
    rendered = str(raised.value)
    assert locator.endpoint_nonce not in rendered


def test_client_authenticates_server_before_sending_session_identity() -> None:
    # Given
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
                },
            )
        worker.join(1.0)
    assert len(captured) == 1
    assert b"session-a" not in captured[0]
    assert endpoint_nonce.encode() not in captured[0]
