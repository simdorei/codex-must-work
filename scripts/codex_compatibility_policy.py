"""Inspect every release-supported managed Codex policy source."""

from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Final, Never, Protocol

from scripts.codex_managed_sources import managed_preference as _managed_preference
from scripts.codex_managed_sources import platform_name as _platform_name
from scripts.codex_managed_sources import windows_program_data as _windows_program_data
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import FileIdentity, file_identity
from scripts.state_io import open_direct_file

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


def _toml_loader() -> _TomlLoader:
    return tomllib.loads


_LOAD_TOML: Final = _toml_loader()
_UNVERIFIABLE: Final = "managed_hook_policy_unverifiable"


@dataclass(frozen=True, slots=True)
class PolicySourceSpec:
    """Name one managed source and its host representation."""

    name: str
    path: Path | None
    value_key: str | None = None
    opaque_when_present: bool = False
    logical_value: str | None = None
    logical_location: str | None = None
    source_kind: str = "Unknown"


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Record a managed source's presence, identity, and digest."""

    name: str
    location: str | None
    present: bool
    identity: FileIdentity | None
    digest: str | None


MANAGED_SOURCE_ORDER: Final = (
    "system_config",
    "system_requirements",
    "cloud_config",
    "cloud_requirements",
    "legacy_managed_config",
    "mdm_config",
    "mdm_requirements",
)
MANAGED_SOURCE_SEARCH_BY_RELEASE: Final = dict.fromkeys(
    ("0.144.0-alpha.4", "0.144.0", "0.145.0-alpha.18"), MANAGED_SOURCE_ORDER
)
MANAGED_SOURCE_KIND_ORDER: Final = (
    ("system_config", "System"),
    ("system_requirements", "SystemRequirementsToml"),
    ("cloud_config", "EnterpriseManaged"),
    ("cloud_requirements", "EnterpriseManaged"),
    ("legacy_managed_config", "LegacyManagedConfigTomlFromFile"),
    ("mdm_config", "LegacyManagedConfigTomlFromMdm"),
    ("mdm_requirements", "MdmManagedPreferences"),
)
MANAGED_SOURCE_KINDS_BY_RELEASE: Final[dict[str, tuple[tuple[str, str], ...]]] = dict.fromkeys(
    ("0.144.0-alpha.4", "0.144.0", "0.145.0-alpha.18"),
    MANAGED_SOURCE_KIND_ORDER,
)


def policy_source_specs(codex_home: Path, version: str) -> tuple[PolicySourceSpec, ...]:
    """Return the source-pinned managed search for an allowed release."""
    if not codex_home.is_absolute() or version not in MANAGED_SOURCE_SEARCH_BY_RELEASE:
        _fail(_UNVERIFIABLE)
    platform = _platform_name()
    system = (
        _windows_program_data() / "OpenAI" / "Codex"
        if platform == "windows"
        else Path("/etc/codex")
    )
    legacy = system / "managed_config.toml"
    cloud = codex_home / "cloud-config-bundle-cache.json"
    config_value = _managed_preference("config_toml_base64") if platform == "darwin" else None
    requirements_value = (
        _managed_preference("requirements_toml_base64") if platform == "darwin" else None
    )
    return (
        PolicySourceSpec("system_config", system / "config.toml", source_kind="System"),
        PolicySourceSpec(
            "system_requirements",
            system / "requirements.toml",
            source_kind="SystemRequirementsToml",
        ),
        PolicySourceSpec(
            "cloud_config", cloud, "cloud_config", source_kind="EnterpriseManaged"
        ),
        PolicySourceSpec(
            "cloud_requirements",
            cloud,
            "cloud_requirements",
            source_kind="EnterpriseManaged",
        ),
        PolicySourceSpec(
            "legacy_managed_config",
            legacy,
            source_kind="LegacyManagedConfigTomlFromFile",
        ),
        PolicySourceSpec(
            "mdm_config",
            None,
            "base64_toml",
            logical_value=config_value,
            logical_location="cfpreferences:com.openai.codex:config_toml_base64",
            source_kind="LegacyManagedConfigTomlFromMdm",
        ),
        PolicySourceSpec(
            "mdm_requirements",
            None,
            "base64_toml",
            logical_value=requirements_value,
            logical_location="cfpreferences:com.openai.codex:requirements_toml_base64",
            source_kind="MdmManagedPreferences",
        ),
    )


def inspect_managed_policy(codex_home: Path, version: str) -> tuple[PolicySnapshot, ...]:
    """Validate and snapshot the complete release-specific source search."""
    specs = policy_source_specs(codex_home, version)
    if tuple(spec.name for spec in specs) != MANAGED_SOURCE_ORDER:
        _fail(_UNVERIFIABLE)
    expected_kinds = MANAGED_SOURCE_KINDS_BY_RELEASE[version]
    if tuple((spec.name, spec.source_kind) for spec in specs) != expected_kinds:
        _fail(_UNVERIFIABLE)
    snapshots = tuple(_inspect(spec) for spec in specs)
    seen: dict[str, tuple[object, str | None]] = {}
    for snapshot in snapshots:
        if snapshot.present and snapshot.location is not None:
            current = snapshot.identity, snapshot.digest
            if snapshot.location in seen and seen[snapshot.location] != current:
                _fail(_UNVERIFIABLE)
            seen[snapshot.location] = current
    return snapshots


def _inspect(spec: PolicySourceSpec) -> PolicySnapshot:
    if spec.logical_location is not None:
        return _inspect_logical(spec)
    if spec.path is None:
        return PolicySnapshot(
            name=spec.name, location=None, present=False, identity=None, digest=None
        )
    path = spec.path.absolute()
    try:
        named = path.lstat()
    except FileNotFoundError:
        return PolicySnapshot(
            name=spec.name,
            location=str(path),
            present=False,
            identity=None,
            digest=None,
        )
    except OSError as error:
        raise InstallPluginError(_UNVERIFIABLE) from error
    if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or _is_reparse(named):
        _fail(_UNVERIFIABLE)
    try:
        descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
            opened = os.fstat(handle.fileno())
    except OSError as error:
        raise InstallPluginError(_UNVERIFIABLE) from error
    if file_identity(named) != file_identity(opened) or spec.opaque_when_present:
        _fail(_UNVERIFIABLE)
    if spec.value_key in {"cloud_config", "cloud_requirements"}:
        _fail(_UNVERIFIABLE)
    policy = _decode_policy(data, spec.value_key)
    _validate_policy(policy)
    return PolicySnapshot(
        name=spec.name,
        location=str(path),
        present=True,
        identity=file_identity(opened),
        digest=hashlib.sha256(data).hexdigest(),
    )


def _inspect_logical(spec: PolicySourceSpec) -> PolicySnapshot:
    value = spec.logical_value
    if value is None:
        return PolicySnapshot(
            name=spec.name,
            location=spec.logical_location,
            present=False,
            identity=None,
            digest=None,
        )
    data = value.encode("utf-8")
    policy = _decode_policy(data, spec.value_key)
    _validate_policy(policy)
    return PolicySnapshot(
        name=spec.name,
        location=spec.logical_location,
        present=True,
        identity=None,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _decode_policy(data: bytes, value_key: str | None) -> TomlTable:
    try:
        if value_key is None:
            loaded = _LOAD_TOML(data.decode("utf-8"))
        elif value_key == "base64_toml":
            loaded = _LOAD_TOML(b64decode(data, validate=True).decode("utf-8"))
        else:
            _fail(_UNVERIFIABLE)
    except (UnicodeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise InstallPluginError(_UNVERIFIABLE) from error
    return loaded


def _validate_policy(policy: TomlTable) -> None:
    if _optional_bool(policy, "allow_managed_hooks_only") is True:
        _fail("managed_hooks_only")
    features = _optional_table(policy, "features")
    requirements = _optional_table(policy, "feature_requirements")
    for table in (features, requirements):
        if table is not None and _optional_bool(table, "hooks") is False:
            _fail("codex_hooks_disabled")


def _optional_table(policy: TomlTable, key: str) -> TomlTable | None:
    value = policy.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        _fail(_UNVERIFIABLE)
    return value


def _optional_bool(policy: TomlTable, key: str) -> bool | None:
    value = policy.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        _fail(_UNVERIFIABLE)
    return value


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
