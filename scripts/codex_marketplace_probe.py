"""Probe repository-root marketplace support in isolated temporary homes."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Final, Never, Protocol

from scripts.codex_compatibility_types import MarketplaceSnapshot
from scripts.install_errors import InstallPluginError

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, value: str, /) -> JsonValue: ...


class _CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


class _CommandInvoker(Protocol):
    def __call__(
        self, argv: tuple[str, ...], env: dict[str, str], reason: str
    ) -> _CommandResult: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()
_MARKETPLACE: Final = "codex-must-work-local"
_PLUGIN: Final = "codex-must-work"
_UNSUPPORTED: Final = "unsupported_codex_marketplace_root"


def root_marketplace_snapshot(
    runtime: Path,
    source: Path,
    target_env: dict[str, str],
    invoke: _CommandInvoker,
) -> MarketplaceSnapshot:
    """Validate root source ``./`` and return normalized output identity."""
    with tempfile.TemporaryDirectory(prefix="cmw-marketplace-preflight-") as temporary:
        home = Path(temporary).resolve()
        config = (
            "[features]\nplugins = true\n\n[notice]\n"
            "hide_world_writable_warning = true\nhide_full_access_warning = true\n\n"
            f'[marketplaces.{_MARKETPLACE}]\nsource_type = "local"\n'
            f"source = {json.dumps(str(source))}\n"
        )
        _ = (home / "config.toml").write_text(config, encoding="utf-8", newline="\n")
        env = dict(target_env)
        env["CODEX_HOME"] = str(home)
        result = invoke(
            (
                str(runtime),
                "plugin",
                "list",
                "--available",
                "--json",
                "--marketplace",
                _MARKETPLACE,
            ),
            env,
            _UNSUPPORTED,
        )
        try:
            parsed = _LOAD_JSON(result.stdout)
        except json.JSONDecodeError as error:
            raise InstallPluginError(_UNSUPPORTED) from error
        if result.returncode or not _marketplace_matches(parsed, source):
            _fail(_UNSUPPORTED)
        normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
        return MarketplaceSnapshot(runtime, hashlib.sha256(normalized).hexdigest())


def _marketplace_matches(value: JsonValue, expected_source: Path) -> bool:
    plugins = (
        value
        if isinstance(value, list)
        else value.get("available", value.get("plugins"))
        if isinstance(value, dict)
        else None
    )
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        return False
    plugin = plugins[0]
    source = plugin.get("source")
    marketplace = plugin.get(
        "marketplace",
        plugin.get("marketplace_name", plugin.get("marketplaceName", _MARKETPLACE)),
    )
    source_path = source.get("path") if isinstance(source, dict) else None
    try:
        resolved_source = (
            Path(source_path).resolve(strict=True) if isinstance(source_path, str) else None
        )
    except (OSError, RuntimeError):
        return False
    return (
        plugin.get("name") == _PLUGIN
        and plugin.get("pluginId", f"{_PLUGIN}@{_MARKETPLACE}") == f"{_PLUGIN}@{_MARKETPLACE}"
        and marketplace == _MARKETPLACE
        and isinstance(source, dict)
        and source.get("source") == "local"
        and resolved_source == expected_source
    )


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
