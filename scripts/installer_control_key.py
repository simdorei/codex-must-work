"""Provision the installer control key behind one typed failure boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.control_capability import ControlKeyError, provision_control_key
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from scripts.installer_data_root import DataRootPublication
    from scripts.installer_lock import InstallerLease


def prepare_control_key(
    lease: InstallerLease,
    publication: DataRootPublication,
) -> bytes:
    """Provision a key or expose only the stable installer reason code."""
    try:
        return provision_control_key(
            publication.path,
            lease.home / "codex-must-work",
        )
    except ControlKeyError as error:
        raise InstallPluginError(error.reason_code) from error
