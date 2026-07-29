from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts.private_root import ensure_private_root
from scripts.work_on_activation import (
    ActivationIdentity,
    ActivationTicketError,
    ActivationTicketStore,
)
from tests.work_on_activation_test_support import TEST_KEY, ActivationFixture


@pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation is not generally available on Windows",
)
def test_redirected_ticket_directory_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    (plugin_data / "work-on-tickets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ActivationTicketError, match="work_on_authorization_state_invalid"):
        _ = (
            ActivationFixture(plugin_data)
            .store()
            .issue(
                ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"),
            )
        )

    assert not any(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows junction integration")
def test_windows_ticket_directory_junction_fails_closed_without_outside_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    _ = sentinel.write_bytes(b"unchanged")
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    ticket_directory = plugin_data / "work-on-tickets"
    creation = _windows_mklink(ticket_directory, outside, junction=True)
    assert creation.returncode == 0, "Windows junction creation failed"
    assert ticket_directory.is_junction()
    assert ticket_directory.samefile(outside)

    try:
        store = ActivationTicketStore(plugin_data, TEST_KEY)
        identity = ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl")
        activation = ActivationFixture(tmp_path)
        with pytest.raises(
            ActivationTicketError,
            match="work_on_authorization_state_invalid",
        ):
            _ = store.issue(identity)
        with pytest.raises(
            ActivationTicketError,
            match="work_on_authorization_state_invalid",
        ):
            store.consume(identity, activation.capability)
        assert sentinel.read_bytes() == b"unchanged"
        assert tuple(path.name for path in outside.iterdir()) == ("keep.txt",)
        assert ticket_directory.samefile(outside)
    finally:
        ticket_directory.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows file-link integration")
@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_windows_ticket_file_link_fails_closed_without_outside_mutation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    outside = tmp_path / "outside-ticket.json"
    original = b'{"outside":"unchanged"}'
    _ = outside.write_bytes(original)
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    ticket_directory = plugin_data / "work-on-tickets"
    ticket_directory.mkdir()
    name = hashlib.sha256(b"session-a").hexdigest()
    ticket_path = ticket_directory / f"{name}.json"
    if link_kind == "hardlink":
        os.link(outside, ticket_path)
        assert not ticket_path.is_symlink()
        assert ticket_path.stat().st_nlink >= 2
    else:
        creation = _windows_mklink(ticket_path, outside, junction=False)
        if creation.returncode != 0:
            pytest.skip(f"Windows file symlink unavailable (exit {creation.returncode})")
        assert ticket_path.is_symlink()
    assert ticket_path.samefile(outside)

    store = ActivationTicketStore(plugin_data, TEST_KEY)
    identity = ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl")
    activation = ActivationFixture(tmp_path)
    with pytest.raises(
        ActivationTicketError,
        match="work_on_authorization_state_invalid",
    ):
        _ = store.issue(identity)
    with pytest.raises(
        ActivationTicketError,
        match="work_on_authorization_state_invalid",
    ):
        store.consume(identity, activation.capability)
    assert outside.read_bytes() == original
    assert ticket_path.samefile(outside)


def _windows_mklink(
    link: Path,
    target: Path,
    *,
    junction: bool,
) -> subprocess.CompletedProcess[str]:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    command = system_root / "System32" / "cmd.exe"
    arguments = [str(command), "/d", "/c", "mklink"]
    if junction:
        arguments.append("/J")
    arguments.extend((str(link), str(target)))
    return subprocess.run(  # noqa: S603 - exact Windows system binary and temp paths.
        arguments,
        check=False,
        capture_output=True,
        text=True,
    )
