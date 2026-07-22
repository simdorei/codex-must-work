from __future__ import annotations

import errno
import multiprocessing
import os
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

try:
    import _winapi
except ImportError:
    _winapi = None

from scripts.install_errors import InstallPluginError
from scripts.installer_lock import (
    file_identity,
    home_lock_key,
    installer_lock,
    require_live_lease,
)

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event as EventType


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock_temp = tmp_path / "temp"
    lock_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(lock_temp))
    value = tmp_path / "home"
    value.mkdir()
    return value


def _contend(home: str, temp: str, ready: EventType, marker: str) -> None:
    tempfile.tempdir = temp
    ready.set()
    with installer_lock(Path(home)):
        _ = Path(marker).write_text("entered", encoding="utf-8")


def _hold(
    home: str,
    temp: str,
    ready: EventType,
    entered: EventType,
    release: EventType,
) -> None:
    tempfile.tempdir = temp
    ready.set()
    with installer_lock(Path(home)):
        entered.set()
        assert release.wait(30)


def _reason(caught: pytest.ExceptionInfo[InstallPluginError]) -> str:
    return caught.value.reason_code


def _create_junction(target: Path, junction: Path) -> None:
    if _winapi is None:
        pytest.fail("Windows junction API unavailable")
    _winapi.CreateJunction(str(target), str(junction))


def test_persistent_lock_preserves_inode_and_byte_zero(home: Path) -> None:
    with installer_lock(home) as first:
        require_live_lease(first)
        first_identity = first.lock_identity
        first_path = first.lock_path
        _ = os.lseek(first.descriptor, 0, os.SEEK_SET)
        assert os.read(first.descriptor, 1) == b"\0"
        assert not os.get_inheritable(first.descriptor)
    assert first_path.exists()
    with installer_lock(home) as second:
        assert second.lock_path == first_path
        assert second.lock_identity == first_identity
        assert file_identity(second.lock_path.lstat()) == first_identity


def test_reentry_is_rejected_without_releasing_outer_lease(home: Path) -> None:
    with installer_lock(home) as lease:
        with pytest.raises(InstallPluginError) as caught, installer_lock(home):
            pytest.fail("reentry unexpectedly succeeded")
        assert _reason(caught) == "installer_lock_reentry"
        require_live_lease(lease)


def test_released_lease_is_rejected(home: Path) -> None:
    with installer_lock(home) as lease:
        require_live_lease(lease)
    with pytest.raises(InstallPluginError) as caught:
        require_live_lease(lease)
    assert _reason(caught) == "installer_lock_lease_invalid"


def test_canonical_alias_uses_same_lock_key(home: Path) -> None:
    alias = home.parent / "alias"
    alias.symlink_to(home, target_is_directory=True)
    assert home_lock_key(alias) == home_lock_key(home)


def test_canonical_home_identity_comes_from_opened_directory(
    home: Path,
) -> None:
    with installer_lock(home) as lease:
        opened = os.fstat(lease.home_descriptor)
        assert stat.S_ISDIR(opened.st_mode)
        assert file_identity(opened) == lease.home_identity
        assert not os.get_inheritable(lease.home_descriptor)
        require_live_lease(lease)
    with pytest.raises(OSError, match=r".") as caught:
        _ = os.fstat(lease.home_descriptor)
    assert caught.value.errno == errno.EBADF


def test_two_process_serializes_beyond_eleven_seconds(home: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    marker = home.parent / "entered"
    with installer_lock(home):
        process = context.Process(
            target=_contend,
            args=(str(home), tempfile.gettempdir(), ready, str(marker)),
        )
        process.start()
        assert ready.wait(10)
        process.join(11.2)
        assert process.is_alive()
        assert not marker.exists()
    process.join(10)
    assert process.exitcode == 0
    assert marker.read_text(encoding="utf-8") == "entered"


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
def test_case_and_junction_aliases_really_contend(home: Path) -> None:
    junction = home.parent / "junction"
    _create_junction(home, junction)
    context = multiprocessing.get_context("spawn")
    for index, alias in enumerate((Path(str(home).upper()), junction)):
        ready, marker = context.Event(), home.parent / f"alias-{index}"
        with installer_lock(home):
            process = context.Process(
                target=_contend,
                args=(str(alias), tempfile.gettempdir(), ready, str(marker)),
            )
            process.start()
            assert ready.wait(10)
            process.join(1)
            assert process.is_alive()
            assert not marker.exists()
        process.join(10)
        assert process.exitcode == 0
        assert marker.is_file()
    junction.rmdir()


def test_three_process_handoff_never_splits_lock_domain(home: Path) -> None:
    context = multiprocessing.get_context("spawn")
    b_ready, b_entered, b_release = (context.Event() for _ in range(3))
    c_ready, c_entered, c_release = (context.Event() for _ in range(3))
    args = str(home), tempfile.gettempdir()
    with installer_lock(home):
        process_b = context.Process(target=_hold, args=(*args, b_ready, b_entered, b_release))
        process_b.start()
        assert b_ready.wait(10)
        assert not b_entered.wait(1)
    assert b_entered.wait(10)
    process_c = context.Process(target=_hold, args=(*args, c_ready, c_entered, c_release))
    process_c.start()
    assert c_ready.wait(10)
    assert not c_entered.wait(1)
    b_release.set()
    process_b.join(10)
    assert process_b.exitcode == 0
    assert c_entered.wait(10)
    c_release.set()
    process_c.join(10)
    assert process_c.exitcode == 0


@pytest.mark.parametrize(
    "kind", ["root_redirect", "root_insecure", "file_redirect", "file_multilink"]
)
def test_unsafe_lock_artifacts_are_rejected(home: Path, kind: str) -> None:
    with installer_lock(home) as lease:
        lock_path = lease.lock_path
    root = lock_path.parent
    if kind == "root_redirect":
        moved = root.with_name("moved-lock-root")
        _ = root.replace(moved)
        root.symlink_to(moved, target_is_directory=True)
    elif kind == "root_insecure":
        moved = root.with_name("secured-lock-root")
        _ = root.replace(moved)
        root.mkdir()
        if os.name != "nt":
            root.chmod(0o777)
    elif kind == "file_redirect":
        moved = lock_path.with_suffix(".moved")
        _ = lock_path.replace(moved)
        lock_path.symlink_to(moved)
    else:
        os.link(lock_path, lock_path.with_suffix(".alias"))
    with pytest.raises(InstallPluginError) as caught, installer_lock(home):
        pytest.fail("unsafe lock artifact was accepted")
    assert _reason(caught) == "installer_lock_unsafe"


@pytest.mark.skipif(os.name != "nt", reason="Windows retry primitive")
def test_windows_contention_retries_at_byte_zero(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    pauses: list[float] = []

    def try_lock(descriptor: int) -> None:
        attempts.append(os.lseek(descriptor, 0, os.SEEK_CUR))
        if len(attempts) < 3:
            raise OSError(errno.EACCES, "contended")

    def unlock(descriptor: int) -> None:
        _ = os.lseek(descriptor, 0, os.SEEK_CUR)

    operations = SimpleNamespace(
        try_lock=try_lock,
        unlock=unlock,
        clock=time.monotonic,
        pause=pauses.append,
    )
    monkeypatch.setattr("scripts.installer_lock._windows_lock_ops", lambda: operations)
    with installer_lock(home):
        pass
    assert attempts == [0, 0, 0]
    assert pauses == [0.1, 0.1]


@pytest.mark.skipif(os.name != "nt", reason="Windows retry primitive")
@pytest.mark.parametrize(
    ("number", "reason"),
    [(errno.ENOSPC, "installer_lock_failed"), (errno.EACCES, "installer_lock_timeout")],
)
def test_windows_rejects_permanent_error_or_timeout(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    number: int,
    reason: str,
) -> None:
    attempts = 0

    def try_lock(_descriptor: int) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(number, "injected")

    def unlock(_descriptor: int) -> None:
        return None

    def clock() -> float:
        return 0.0

    def pause(_seconds: float) -> None:
        return None

    operations = SimpleNamespace(
        try_lock=try_lock,
        unlock=unlock,
        clock=clock,
        pause=pause,
    )
    monkeypatch.setattr("scripts.installer_lock._windows_lock_ops", lambda: operations)
    with (
        pytest.raises(InstallPluginError) as caught,
        installer_lock(home, timeout_seconds=0),
    ):
        pytest.fail("failure path unexpectedly acquired the lock")
    assert _reason(caught) == reason
    assert attempts == 1
