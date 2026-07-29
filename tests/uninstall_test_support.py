"""Shared exact package and config fixtures for uninstall security tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
from pathlib import Path

from scripts.cache_security import secure_path
from scripts.cache_types import identity
from scripts.hook_commands import trusted_hook_commands_for_plugin
from scripts.hook_trust import trusted_hook_states_for_plugin
from scripts.installer_cache_validation import snapshot_retained_cache
from scripts.installer_mcp_runtime import current_runtime_spec
from scripts.marketplace_identity import LEGACY_MARKETPLACE_NAME, MARKETPLACE_NAME
from scripts.private_root import ensure_private_root


def secure_tree(root: Path) -> None:
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        assert secure_path(current_path, directory=True, apply=True)
        for name in directories:
            assert secure_path(current_path / name, directory=True, apply=True)
        for name in files:
            assert secure_path(current_path / name, directory=False, apply=True)


def cache_generation(home: Path, marketplace: str, version: str = "1.2.3") -> Path:
    root = home / "plugins" / "cache" / marketplace / "codex-must-work" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    paths = (
        ".codex-plugin/plugin.json",
        "hooks/hooks.json",
        "runtime/package-files.json",
    )
    (root / "runtime").mkdir()
    _ = (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-must-work", "version": version}),
        encoding="utf-8",
    )
    _ = shutil.copy2(Path(__file__).parents[1] / "hooks" / "hooks.json", root / "hooks")
    _ = (root / "runtime" / "package-files.json").write_text(
        json.dumps(paths),
        encoding="utf-8",
    )
    secure_tree(root)
    return root


def config_bytes(source_root: Path, *, include_legacy: bool = True) -> bytes:
    canonical = trusted_hook_states_for_plugin(source_root, MARKETPLACE_NAME)[0]
    legacy = trusted_hook_states_for_plugin(source_root, LEGACY_MARKETPLACE_NAME)[0]
    legacy_text = (
        "".join(
            (
                "[marketplaces.codex-must-work-local]\n",
                'source_type = "local"\n',
                f"source = {json.dumps(str(source_root.resolve()))}\n",
                '[plugins."codex-must-work@codex-must-work-local"]\n',
                "enabled = false\n",
                f'[hooks.state."{legacy.key}"]\n',
                "enabled = true\n",
                f"trusted_hash = {json.dumps(legacy.trusted_hash)}\n",
            )
        )
        if include_legacy
        else ""
    )
    text = (
        'title = "keep"\n'
        "[marketplaces.simdorei]\n"
        'source_type = "git"\n'
        'source = "https://github.com/simdorei/codex-must-work.git"\n'
        'ref = "main"\n'
        '[plugins."codex-must-work@simdorei"]\n'
        "enabled = true\n"
        f'[hooks.state."{canonical.key}"]\n'
        "enabled = true\n"
        f"trusted_hash = {json.dumps(canonical.trusted_hash)}\n"
        f"{legacy_text}"
        "[marketplaces.other]\n"
        'source = "keep-byte-for-byte"\n'
        '[plugins."other@other"]\n'
        "enabled = true # keep\n"
    )
    return text.encode()


def authorize_install(
    home: Path,
    source_root: Path,
    cache: Path,
    *,
    runtime: Path | None = None,
    runtime_generation: str | None = None,
) -> Path:
    """Write one independently encoded protected receipt fixture."""
    state = home / ".cmw-installer-state"
    ensure_private_root(state)
    key = b"k" * 32
    key_path = state / "install-receipt-v1.key"
    _ = key_path.write_bytes(key)
    key_path.chmod(0o600)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    generation = (
        current_runtime_spec().version if runtime_generation is None else runtime_generation
    )
    if runtime is None:
        data.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_root(data)
        runtime = data / f"portable-python-{generation}"
        runtime.mkdir(exist_ok=True)
        secure_tree(runtime)
    cache_identity, digest = snapshot_retained_cache(cache)
    runtime_identity = identity(runtime.lstat())
    states = trusted_hook_states_for_plugin(cache, MARKETPLACE_NAME)
    commands = trusted_hook_commands_for_plugin(cache, MARKETPLACE_NAME)
    payload = {
        "schema": 1,
        "plugin_id": "codex-must-work@simdorei",
        "marketplace": "simdorei",
        "marketplace_source": "https://github.com/simdorei/codex-must-work.git",
        "marketplace_ref": "main",
        "cache_path": str(cache.resolve()),
        "cache_version": cache.name,
        "cache_device": cache_identity.device,
        "cache_inode": cache_identity.inode,
        "package_digest": digest,
        "source_root": str(source_root.resolve()),
        "hooks": [
            {
                "key": state_item.key,
                "command": command.command,
                "trusted_hash": state_item.trusted_hash,
            }
            for state_item, command in zip(states, commands, strict=True)
        ],
        "runtime_path": str(runtime.resolve()),
        "runtime_device": runtime_identity.device,
        "runtime_inode": runtime_identity.inode,
        "runtime_generation": generation,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "payload": payload,
        "hmac_sha256": hmac.new(key, encoded, hashlib.sha256).hexdigest(),
    }
    receipt = state / "install-receipt-v1.json"
    _ = receipt.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    return receipt
