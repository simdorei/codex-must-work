from __future__ import annotations

from threading import Event, Thread
from typing import TYPE_CHECKING

from scripts.watcher import acquire_watcher_lease, refresh_watcher_lease, release_watcher_lease

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_watcher_lease_allows_only_one_process(tmp_path: Path) -> None:
    root = tmp_path / "codex-must-work"
    root.mkdir()
    first = acquire_watcher_lease(root)
    assert first is not None
    try:
        assert acquire_watcher_lease(root, timeout_seconds=0.0) is None
    finally:
        release_watcher_lease(first)
        second = acquire_watcher_lease(root, timeout_seconds=0.0)
    assert second is not None
    release_watcher_lease(second)


def test_watcher_lease_waits_for_previous_process_handoff(tmp_path: Path) -> None:
    root = tmp_path / "codex-must-work"
    root.mkdir()
    first = acquire_watcher_lease(root)
    assert first is not None
    entered = Event()
    acquired: list[bool] = []

    def wait_for_lease() -> None:
        entered.set()
        lease = acquire_watcher_lease(root)
        acquired.append(lease is not None)
        if lease is not None:
            release_watcher_lease(lease)

    thread = Thread(target=wait_for_lease)
    thread.start()
    assert entered.wait(timeout=1.0)
    release_watcher_lease(first)
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert acquired == [True]


def test_watcher_lease_verifies_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex-must-work"
    root.mkdir()
    verified: list[Path] = []
    monkeypatch.setattr("scripts.watcher.ensure_private_root", verified.append)

    lease = acquire_watcher_lease(root)

    assert lease is not None
    assert verified == [root]
    release_watcher_lease(lease)


def test_abandoned_malformed_watcher_lease_is_recoverable(tmp_path: Path) -> None:
    root = tmp_path / "codex-must-work"
    root.mkdir()
    _ = (root / "watcher.lease").write_text("not-a-pid\n", encoding="ascii")

    lease = acquire_watcher_lease(root)

    assert lease is not None
    release_watcher_lease(lease)


def test_watcher_lease_refresh_works_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "codex-must-work"
    root.mkdir()
    lease = acquire_watcher_lease(root)
    assert lease is not None
    try:
        refresh_watcher_lease(lease)
        assert lease.path.read_text(encoding="ascii") == f"{lease.pid}\n"
    finally:
        release_watcher_lease(lease)
