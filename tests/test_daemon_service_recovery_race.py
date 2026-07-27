from __future__ import annotations

import hashlib
import multiprocessing
from typing import TYPE_CHECKING

import pytest

from scripts.manager_lease import manager_lease_owner
from scripts.setup import enable_session
from scripts.state import load_state, runtime_path
from tests.daemon_recovery_process_fixture import (
    ServiceContender,
    read_service_receipt,
    run_service_recovery,
)
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    activation_request,
    bind_pending_activation,
    capabilities,
    session_files,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("iteration", range(4))
def test_two_daemon_services_have_one_full_recovery_owner(
    tmp_path: Path,
    iteration: int,
) -> None:
    # Given: two real services discover one managed activation before either claims it.
    root, transcript = session_files(tmp_path / f"iteration-{iteration}", FIRST_SESSION)
    _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
    bind_pending_activation(root, FIRST_SESSION, transcript)
    path = runtime_path(root, FIRST_SESSION)
    initial_revision = load_state(root, path).values["revision"]
    assert type(initial_revision) is int
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    release = context.Event()
    ready = (context.Event(), context.Event())
    receipts = (tmp_path / f"service-{iteration}-first", tmp_path / f"service-{iteration}-second")
    processes = tuple(
        context.Process(
            target=run_service_recovery,
            args=(
                ServiceContender(
                    str(root),
                    path.name,
                    barrier,
                    ready[index],
                    release,
                    str(receipts[index]),
                ),
            ),
        )
        for index in range(2)
    )

    # When: both services cross the claim barrier and the winner holds its lease.
    for process in processes:
        process.start()
    assert all(signal.wait(10) for signal in ready)
    results = tuple(read_service_receipt(receipt) for receipt in receipts)
    winner = next(result for result in results if result.role == "winner")
    loser = next(result for result in results if result.role == "loser")

    # Then: only the exact winning child owns a task, state writes, and live lease.
    assert sorted(result.role for result in results) == ["loser", "winner"]
    assert {result.pid for result in results} == {process.pid for process in processes}
    assert winner.task_count == 1
    assert winner.lease_owner == winner.pid
    assert winner.state_writes == 2
    assert loser.task_count == 0
    assert loser.state_writes == 0
    assert loser.failure_cleanups == 0
    assert manager_lease_owner(root, path.name) == winner.pid
    assert hashlib.sha256(path.read_bytes()).hexdigest() == winner.state_sha256

    release.set()
    for process in processes:
        process.join(10)
    final = load_state(root, path).values
    assert all(process.exitcode == 0 and not process.is_alive() for process in processes)
    assert manager_lease_owner(root, path.name) is None
    assert not list((root / "managers").glob("*.lease"))
    assert final["manager_ready"] is False
    assert final["manager_pid"] is None
    assert final["revision"] == initial_revision + 3
