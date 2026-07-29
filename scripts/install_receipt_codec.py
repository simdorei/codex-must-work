"""Typed wire codec for the protected CMW install receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_types import CacheIdentity
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import (
    MARKETPLACE_NAME,
    MARKETPLACE_REF,
    MARKETPLACE_SOURCE,
    PLUGIN_ID,
)

if TYPE_CHECKING:
    from scripts.protected_installer_state import JsonObject, JsonValue

_SCHEMA: Final = 1
_INVALID: Final = "uninstall_receipt_invalid"
_SHA256_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class ReceiptHook:
    """Bind one command and trusted hash to its exact config key."""

    key: str
    command: str
    trusted_hash: str


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    """Validated protected evidence for one exact live installation."""

    cache_path: Path
    cache_version: str
    cache_identity: CacheIdentity
    package_digest: str
    source_root: Path
    hooks: tuple[ReceiptHook, ...]
    runtime_path: Path
    runtime_identity: CacheIdentity
    runtime_generation: str

    @property
    def trusted_hooks(self) -> tuple[TrustedHookState, ...]:
        """Return the exact config trust projection."""
        return tuple(TrustedHookState(item.key, item.trusted_hash) for item in self.hooks)


def install_receipt_payload(receipt: InstallReceipt) -> JsonObject:
    """Encode one validated receipt into its signed-record payload."""
    return {
        "schema": _SCHEMA,
        "plugin_id": PLUGIN_ID,
        "marketplace": MARKETPLACE_NAME,
        "marketplace_source": MARKETPLACE_SOURCE,
        "marketplace_ref": MARKETPLACE_REF,
        "cache_path": str(receipt.cache_path),
        "cache_version": receipt.cache_version,
        "cache_device": receipt.cache_identity.device,
        "cache_inode": receipt.cache_identity.inode,
        "package_digest": receipt.package_digest,
        "source_root": str(receipt.source_root),
        "hooks": [
            {"key": item.key, "command": item.command, "trusted_hash": item.trusted_hash}
            for item in receipt.hooks
        ],
        "runtime_path": str(receipt.runtime_path),
        "runtime_device": receipt.runtime_identity.device,
        "runtime_inode": receipt.runtime_identity.inode,
        "runtime_generation": receipt.runtime_generation,
    }


def parse_install_receipt(raw: JsonObject) -> InstallReceipt:
    """Parse one authenticated signed-record payload."""
    expected = {
        "schema",
        "plugin_id",
        "marketplace",
        "marketplace_source",
        "marketplace_ref",
        "cache_path",
        "cache_version",
        "cache_device",
        "cache_inode",
        "package_digest",
        "source_root",
        "hooks",
        "runtime_path",
        "runtime_device",
        "runtime_inode",
        "runtime_generation",
    }
    if set(raw) != expected:
        _fail()
    if (
        raw["schema"] != _SCHEMA
        or raw["plugin_id"] != PLUGIN_ID
        or raw["marketplace"] != MARKETPLACE_NAME
        or raw["marketplace_source"] != MARKETPLACE_SOURCE
        or raw["marketplace_ref"] != MARKETPLACE_REF
    ):
        _fail()
    hooks_raw = raw["hooks"]
    if not isinstance(hooks_raw, list):
        _fail()
    return InstallReceipt(
        _path(raw["cache_path"]),
        _text(raw["cache_version"]),
        CacheIdentity(_integer(raw["cache_device"]), _integer(raw["cache_inode"])),
        _digest(raw["package_digest"]),
        _path(raw["source_root"]),
        tuple(_parse_hook(item) for item in hooks_raw),
        _path(raw["runtime_path"]),
        CacheIdentity(_integer(raw["runtime_device"]), _integer(raw["runtime_inode"])),
        _text(raw["runtime_generation"]),
    )


def _parse_hook(value: JsonValue) -> ReceiptHook:
    if not isinstance(value, dict) or set(value) != {"key", "command", "trusted_hash"}:
        _fail()
    return ReceiptHook(_text(value["key"]), _text(value["command"]), _text(value["trusted_hash"]))


def _path(value: JsonValue) -> Path:
    path = Path(_text(value))
    if not path.is_absolute():
        _fail()
    return path


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail()
    return value


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail()
    return value


def _digest(value: JsonValue) -> str:
    digest = _text(value)
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _fail()
    return digest


def _fail() -> Never:
    raise InstallPluginError(_INVALID)
