"""Spawn-safe full-service recovery race helpers."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from scripts.daemon_registry import DaemonRegistry
from scripts.manager_failure import record_manager_failure
from scripts.manager_lease import (
    RecoveryLeaseIdentity,
    RecoveryManagerLease,
    acquire_recovery_manager_lease,
    manager_lease_owner,
)
from scripts.state_io import atomic_json_write
from tests.daemon_service_fixture import create_service

if TYPE_CHECKING:
    from collections.abc import Mapping
    from multiprocessing.synchronize import Barrier as BarrierType
    from multiprocessing.synchronize import Event as EventType

    from scripts.daemon_task import DaemonTask
    from scripts.state_io import JsonValue


@dataclass(frozen=True, slots=True)
class ServiceContender:
    """Inputs shared with one spawned daemon-service contender."""

    root: str
    runtime_name: str
    barrier: BarrierType
    ready: EventType
    release: EventType
    receipt: str


@dataclass(frozen=True, slots=True)
class ServiceReceipt:
    """Durable ownership and write counters from one spawned service."""

    role: str
    pid: int
    task_count: int
    lease_owner: int | None
    state_sha256: str
    state_writes: int
    failure_cleanups: int


def run_service_recovery(contender: ServiceContender) -> None:
    """Recover through DaemonService after both processes finish discovery."""
    root = Path(contender.root)
    runtime_path = root / "runtime" / contender.runtime_name
    original_recover = DaemonRegistry.recover
    state_writes = 0
    failure_cleanups = 0
    recovered_count = [0]

    def barrier_claim(
        claim_root: Path,
        runtime_name: str,
        expected: RecoveryLeaseIdentity,
    ) -> RecoveryManagerLease | None:
        _ = contender.barrier.wait(10)
        return acquire_recovery_manager_lease(claim_root, runtime_name, expected)

    def count_failure(failure_root: Path, path: Path, reason_code: str) -> None:
        nonlocal failure_cleanups
        failure_cleanups += 1
        record_manager_failure(failure_root, path, reason_code)

    def count_write(
        path: Path,
        *,
        schema_version: int,
        values: Mapping[str, JsonValue],
    ) -> None:
        nonlocal state_writes
        state_writes += 1
        atomic_json_write(path, schema_version=schema_version, values=values)

    def count_recover(registry: DaemonRegistry) -> tuple[DaemonTask, ...]:
        recovered = original_recover(registry)
        recovered_count[0] = len(recovered)
        return recovered

    with (
        patch("scripts.daemon_registry.acquire_recovery_manager_lease", barrier_claim),
        patch("scripts.daemon_registry.record_manager_failure", count_failure),
        patch("scripts.state.atomic_json_write", count_write),
        patch.object(DaemonRegistry, "recover", count_recover),
    ):
        service = create_service(root, [])
    try:
        task_count = recovered_count[0]
        role = "winner" if task_count == 1 else "loser"
        receipt = ServiceReceipt(
            role,
            os.getpid(),
            task_count,
            manager_lease_owner(root, contender.runtime_name),
            hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            state_writes,
            failure_cleanups,
        )
        _ = Path(contender.receipt).write_text(_serialize(receipt), encoding="utf-8")
        contender.ready.set()
        if role == "winner":
            assert contender.release.wait(10)
    finally:
        service.close()


def read_service_receipt(path: Path) -> ServiceReceipt:
    """Parse one child receipt without trusting implicit field order."""
    role, pid, task_count, lease_owner, state_hash, writes, failures = path.read_text(
        encoding="utf-8"
    ).splitlines()
    return ServiceReceipt(
        role,
        int(pid),
        int(task_count),
        None if lease_owner == "none" else int(lease_owner),
        state_hash,
        int(writes),
        int(failures),
    )


def _serialize(receipt: ServiceReceipt) -> str:
    owner = "none" if receipt.lease_owner is None else str(receipt.lease_owner)
    return (
        f"{receipt.role}\n{receipt.pid}\n{receipt.task_count}\n{owner}\n"
        f"{receipt.state_sha256}\n{receipt.state_writes}\n"
        f"{receipt.failure_cleanups}\n"
    )
