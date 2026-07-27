from __future__ import annotations

import hashlib
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.daemon_recovery import discover_persisted_tasks
from scripts.manager_lease import (
    RecoveryLeaseIdentity,
    acquire_recovery_manager_lease,
    manager_lease_owner,
    release_manager_lease,
)
from scripts.setup import enable_session
from scripts.state import (
    StateDocument,
    load_state,
    mutate_existing_state,
    runtime_path,
    save_state,
)
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    activation_request,
    capabilities,
    session_files,
)

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier as BarrierType
    from multiprocessing.synchronize import Event as EventType

    from scripts.state_io import JsonValue


@dataclass(frozen=True, slots=True)
class _Contender:
    root: str
    runtime_name: str
    identity: RecoveryLeaseIdentity
    barrier: BarrierType
    done: EventType
    release: EventType
    receipt: str


@dataclass(frozen=True, slots=True)
class _Receipt:
    outcome: str
    before: str
    after: str
    pid: int


def _claim_recovery(values: _Contender) -> None:
    path = Path(values.root) / "runtime" / values.runtime_name
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _ = values.barrier.wait(10)
    claim = acquire_recovery_manager_lease(
        Path(values.root),
        values.runtime_name,
        values.identity,
    )
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    outcome = "loser" if claim is None else "winner"
    _ = Path(values.receipt).write_text(
        f"{outcome}\n{before}\n{after}\n{os.getpid()}\n",
        encoding="utf-8",
    )
    values.done.set()
    if claim is not None:
        assert values.release.wait(10)
        release_manager_lease(claim.lease)


def _crash_after_claim(
    root: str,
    runtime_name: str,
    identity: RecoveryLeaseIdentity,
    ready: EventType,
    receipt: str,
) -> None:
    claim = acquire_recovery_manager_lease(Path(root), runtime_name, identity)
    assert claim is not None
    path = Path(root) / "runtime" / runtime_name
    _ = Path(receipt).write_text(
        f"{os.getpid()}\n{hashlib.sha256(path.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    ready.set()
    os._exit(23)


def _runtime(root: Path) -> tuple[Path, RecoveryLeaseIdentity]:
    session_id = "session-1"
    transcript = "sessions/rollout.jsonl"
    path = runtime_path(root, session_id)
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": session_id,
                "transcript_path": transcript,
                "revision": 3,
            }
        ),
    )
    return path, RecoveryLeaseIdentity(3, session_id, transcript)


def _read_receipt(path: Path) -> _Receipt:
    outcome, before, after, pid = path.read_text(encoding="utf-8").splitlines()
    return _Receipt(outcome, before, after, int(pid))


@pytest.mark.parametrize("iteration", range(6))
def test_two_process_recovery_has_one_lease_and_generation_winner(
    tmp_path: Path,
    iteration: int,
) -> None:
    # Given: two daemon processes discovered the same activation generation.
    root = tmp_path / f"root-{iteration}"
    path, identity = _runtime(root)
    before = path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    release = context.Event()
    done = (context.Event(), context.Event())
    receipts = (tmp_path / "first.json", tmp_path / "second.json")
    processes = tuple(
        context.Process(
            target=_claim_recovery,
            args=(
                _Contender(
                    str(root),
                    path.name,
                    identity,
                    barrier,
                    done[index],
                    release,
                    str(receipts[index]),
                ),
            ),
        )
        for index in range(2)
    )

    # When: both processes cross the barrier and contend for the OS-held lease.
    for process in processes:
        process.start()
    _ = barrier.wait(10)
    assert all(signal.wait(10) for signal in done)
    results = tuple(_read_receipt(receipt) for receipt in receipts)
    release.set()
    for process in processes:
        process.join(10)

    # Then: one winner performs the sole generation transition and one loser writes none.
    expected = before.replace(b'"revision":3', b'"revision":4')
    expected_hash = hashlib.sha256(expected).hexdigest()
    assert sorted(result.outcome for result in results) == ["loser", "winner"]
    assert all(result.before == before_hash for result in results)
    assert all(result.after in {before_hash, expected_hash} for result in results)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    assert {result.pid for result in results} == {process.pid for process in processes}
    assert path.read_bytes() == expected
    assert all(process.exitcode == 0 for process in processes)


def test_crash_after_claim_allows_exactly_one_later_recovery(tmp_path: Path) -> None:
    # Given: a process claims the generation, then exits before activation-fence recovery.
    root = tmp_path / "root"
    path, identity = _runtime(root)
    before = path.read_bytes()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    receipt = tmp_path / "crash.json"
    crashed = context.Process(
        target=_crash_after_claim,
        args=(str(root), path.name, identity, ready, str(receipt)),
    )

    # When: the crashed OS lease releases and recovery discovers the claimed generation.
    crashed.start()
    assert ready.wait(10)
    crashed.join(10)
    assert crashed.exitcode == 23
    claimed = before.replace(b'"revision":3', b'"revision":4')
    crash_pid, crash_hash = receipt.read_text(encoding="utf-8").splitlines()
    assert int(crash_pid) == crashed.pid
    assert crash_hash == hashlib.sha256(claimed).hexdigest()
    assert path.read_bytes() == claimed
    stale_hash = hashlib.sha256(claimed).hexdigest()
    assert acquire_recovery_manager_lease(root, path.name, identity) is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == stale_hash
    next_identity = RecoveryLeaseIdentity(4, identity.session_id, identity.transcript_path)
    claim = acquire_recovery_manager_lease(root, path.name, next_identity)

    # Then: exactly one later owner claims the unchanged generation.
    assert claim is not None
    release_manager_lease(claim.lease)
    recovered = before.replace(b'"revision":3', b'"revision":5')
    assert path.read_bytes() == recovered
    recovered_hash = hashlib.sha256(recovered).hexdigest()
    assert acquire_recovery_manager_lease(root, path.name, next_identity) is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == recovered_hash


def test_discovery_to_lease_transcript_swap_writes_zero_bytes(tmp_path: Path) -> None:
    # Given: discovery captures the raw transcript identity of an enabled task.
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    _ = enable_session(root, activation_request(FIRST_SESSION, transcript), capabilities())
    saved = discover_persisted_tasks(root)
    assert len(saved) == 1
    discovered = saved[0]
    path = root / "runtime" / discovered.runtime_name

    def swap_transcript(values: dict[str, JsonValue]) -> None:
        values["transcript_path"] = f"{discovered.transcript_path}.replacement"

    _ = mutate_existing_state(root, path, swap_transcript)
    before = path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    # When: stale recovery acquires the OS lease and rereads the swapped state.
    claim = acquire_recovery_manager_lease(
        root,
        discovered.runtime_name,
        RecoveryLeaseIdentity(
            discovered.activation_generation,
            discovered.session_id,
            discovered.transcript_path,
        ),
    )

    # Then: the immutable discovery mismatch releases the lease with zero writes.
    assert claim is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert path.read_bytes() == before
    assert load_state(root, path).values["revision"] == discovered.activation_generation
    assert manager_lease_owner(root, discovered.runtime_name) is None
