from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tests.daemon_service_fixture import (
    FIRST_SESSION,
    FakeAppServer,
    create_service,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_observe_only_never_creates_an_app_server_client(tmp_path: Path) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)

    try:
        result = service.start(
            replace(
                start_request(FIRST_SESSION, transcript),
                auto_restart=False,
                observe_only=True,
            )
        )

        assert result.enabled is True
        assert clients == []
    finally:
        service.close()
