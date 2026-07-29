"""Explicit fail-closed CMW uninstaller and cleanup receipt CLI."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config_publication import (
    ConfigSnapshot,
    read_config_bytes,
    write_config_bytes,
)
from scripts.install_errors import InstallPluginError
from scripts.install_receipt import load_install_receipt, remove_install_receipt
from scripts.installer_lock import installer_lock
from scripts.uninstall_completion import (
    load_uninstall_completion,
    mark_uninstall_complete,
)
from scripts.uninstall_config import has_uninstall_targets, render_config_removal
from scripts.uninstall_data import planned_data_roots, validate_bound_data_roots
from scripts.uninstall_paths import (
    delete_quarantined_root,
    plan_quarantine,
    planned_cache_generations,
    planned_runtime_roots,
    quarantine_owned_root,
)
from scripts.uninstall_pending import (
    PendingCounts,
    PendingPlan,
    cleanup_pending,
    current_phase,
    load_pending_uninstall,
    remove_pending_uninstall,
    rollback_pending,
    write_pending_uninstall,
)

_USAGE: Final = "usage: uninstall_plugin.py CODEX_HOME SOURCE_ROOT [--purge-data]\n"
_PATH_ARGUMENT_COUNT: Final = 2
_CACHE_UNKNOWN: Final = "uninstall_cache_ownership_unknown"

if TYPE_CHECKING:
    from scripts.installer_lock import InstallerLease
    from scripts.uninstall_types import OwnedRoot


@dataclass(frozen=True, slots=True)
class UninstallReceipt:
    """Machine-readable record of exact uninstall effects."""

    config_changed: bool
    removed_cache_generations: int
    removed_runtime_roots: int
    purged_data_roots: int
    preserved_data_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Carry one uninstall transaction without widening helper signatures."""

    replacement: bytes
    cache_roots: tuple[OwnedRoot, ...]
    runtime_roots: tuple[OwnedRoot, ...]
    data_roots: tuple[OwnedRoot, ...]
    completion_data_roots: tuple[OwnedRoot, ...]
    purge_data: bool


def uninstall(codex_home: Path, source_root: Path, *, purge_data: bool) -> UninstallReceipt:
    """Remove only validated CMW installation state under one installer lease."""
    with installer_lock(codex_home) as lease:
        snapshot = read_config_bytes(lease.home, lease)
        pending = load_pending_uninstall(lease)
        if pending is not None:
            phase = current_phase(pending, snapshot.data)
            if phase == "before":
                rollback_pending(pending)
                remove_pending_uninstall(lease)
                snapshot = read_config_bytes(lease.home, lease)
            else:
                cleanup_pending(pending)
                mark_uninstall_complete(
                    lease,
                    source_root,
                    pending.completion_data_roots,
                    data_purged=pending.purge_data,
                )
                remove_install_receipt(lease)
                remove_pending_uninstall(lease)
                preserved = () if pending.purge_data else _present_data_roots(lease.home)
                return UninstallReceipt(
                    config_changed=True,
                    removed_cache_generations=pending.cache_count,
                    removed_runtime_roots=pending.runtime_count,
                    purged_data_roots=pending.data_count,
                    preserved_data_roots=preserved,
                )
        try:
            install_receipt = load_install_receipt(lease, source_root)
        except InstallPluginError as error:
            completion = load_uninstall_completion(lease, source_root)
            if error.reason_code == "uninstall_receipt_reinstall_required" and completion:
                if purge_data:
                    roots = validate_bound_data_roots(lease.home, completion.data_roots)
                    if not roots:
                        return _empty_receipt(lease.home, purge_data=True)
                    return _execute_plan(
                        lease,
                        source_root,
                        snapshot,
                        ExecutionPlan(
                            replacement=snapshot.data,
                            cache_roots=(),
                            runtime_roots=(),
                            data_roots=roots,
                            completion_data_roots=completion.data_roots,
                            purge_data=True,
                        ),
                    )
                return _empty_receipt(lease.home, purge_data=False)
            if (
                error.reason_code == "uninstall_receipt_reinstall_required"
                and not purge_data
                and not _cache_installation_present(lease.home)
                and not has_uninstall_targets(snapshot)
            ):
                return _empty_receipt(lease.home, purge_data=False)
            raise
        cache_plan = planned_cache_generations(lease.home, install_receipt)
        runtimes = (
            () if purge_data else planned_runtime_roots(lease.home, source_root, install_receipt)
        )
        completion_data_roots = planned_data_roots(lease.home)
        data_roots = completion_data_roots if purge_data else ()
        replacement = render_config_removal(snapshot, cache_plan.evidence)
        return _execute_plan(
            lease,
            source_root,
            snapshot,
            ExecutionPlan(
                replacement,
                cache_plan.roots,
                runtimes,
                data_roots,
                completion_data_roots,
                purge_data,
            ),
        )


def _execute_plan(
    lease: InstallerLease,
    source_root: Path,
    snapshot: ConfigSnapshot,
    transaction: ExecutionPlan,
) -> UninstallReceipt:
    targets = (
        *transaction.cache_roots,
        *transaction.runtime_roots,
        *transaction.data_roots,
    )
    planned = tuple(plan_quarantine(target) for target in targets)
    counts = PendingCounts(
        len(transaction.cache_roots),
        len(transaction.runtime_roots),
        len(transaction.data_roots),
    )
    pending_plan = PendingPlan(
        snapshot.data,
        transaction.replacement,
        planned,
        counts,
        transaction.purge_data,
        transaction.completion_data_roots,
    )
    write_pending_uninstall(lease, pending_plan, "prepared")
    try:
        for root in planned:
            _ = quarantine_owned_root(root)
        if transaction.replacement != snapshot.data:
            _ = write_config_bytes(lease, snapshot, transaction.replacement)
        write_pending_uninstall(lease, pending_plan, "committed")
    except (OSError, InstallPluginError):
        current = read_config_bytes(lease.home, lease)
        pending = load_pending_uninstall(lease)
        if pending is not None and current_phase(pending, current.data) == "before":
            rollback_pending(pending)
            remove_pending_uninstall(lease)
        raise
    for root in planned:
        delete_quarantined_root(root)
    mark_uninstall_complete(
        lease,
        source_root,
        transaction.completion_data_roots,
        data_purged=transaction.purge_data,
    )
    remove_install_receipt(lease)
    remove_pending_uninstall(lease)
    return UninstallReceipt(
        config_changed=transaction.replacement != snapshot.data,
        removed_cache_generations=len(transaction.cache_roots),
        removed_runtime_roots=len(transaction.runtime_roots),
        purged_data_roots=len(transaction.data_roots),
        preserved_data_roots=(() if transaction.purge_data else _present_data_roots(lease.home)),
    )


def _empty_receipt(home: Path, *, purge_data: bool) -> UninstallReceipt:
    return UninstallReceipt(
        config_changed=False,
        removed_cache_generations=0,
        removed_runtime_roots=0,
        purged_data_roots=0,
        preserved_data_roots=() if purge_data else _present_data_roots(home),
    )


def _present_data_roots(home: Path) -> tuple[str, ...]:
    parent = home / "plugins" / "data"
    names = ("codex-must-work-simdorei", "codex-must-work-codex-must-work-local")
    candidates = (*(parent / name for name in names), home / "codex-must-work")
    return tuple(str(path) for path in candidates if path.exists())


def _cache_installation_present(home: Path) -> bool:
    versions = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    try:
        return any(versions.iterdir())
    except FileNotFoundError:
        return False
    except OSError as error:
        raise InstallPluginError(_CACHE_UNKNOWN) from error


def run_cli(argv: list[str] | None = None) -> int:
    """Parse the explicit uninstall command and print one JSON cleanup receipt."""
    values = sys.argv[1:] if argv is None else argv
    purge = values[-1:] == ["--purge-data"]
    paths = values[:-1] if purge else values
    if len(paths) != _PATH_ARGUMENT_COUNT:
        _ = sys.stderr.write(_USAGE)
        return 2
    try:
        receipt = uninstall(Path(paths[0]), Path(paths[1]), purge_data=purge)
    except InstallPluginError as error:
        _ = sys.stderr.write(json.dumps({"error_code": error.reason_code}) + "\n")
        return 1
    _ = sys.stdout.write(json.dumps(asdict(receipt), sort_keys=True) + "\n")
    return 0


def main() -> int:
    """Run the uninstall CLI."""
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
