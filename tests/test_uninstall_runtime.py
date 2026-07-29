from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import install_receipt, uninstall_paths
from scripts.installer_mcp_runtime import RuntimePlatform, RuntimeSpec
from scripts.private_root import ensure_private_root
from scripts.runtime_tree import (
    RuntimeTreeManifest,
    load_runtime_manifest,
    validate_runtime_tree,
)
from scripts.uninstall_plugin import uninstall
from tests.uninstall_test_support import (
    authorize_install,
    cache_generation,
    config_bytes,
    secure_tree,
)

if TYPE_CHECKING:
    import pytest


def test_default_uninstall_removes_authenticated_runtime_and_preserves_user_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    runtime_source = tmp_path / "runtime-source"
    manifests = runtime_source / "runtime" / "manifests"
    exclusions = runtime_source / "runtime" / "exclusions"
    manifests.mkdir(parents=True)
    exclusions.mkdir(parents=True)
    payload = b"runtime"
    empty_digest = hashlib.sha256(b"").hexdigest()
    entries = [
        {
            "path": "bin",
            "size": 0,
            "sha256": empty_digest,
            "type": "directory",
            "executable": False,
        },
        {
            "path": "bin/python",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "type": "file",
            "executable": False,
        },
    ]
    manifest_data = json.dumps(entries).encode()
    exclusion_data = b"[]"
    _ = (manifests / "test.json").write_bytes(manifest_data)
    _ = (exclusions / "test.json").write_bytes(exclusion_data)
    spec = RuntimeSpec(
        "test",
        "unused",
        "0" * 64,
        RuntimePlatform.WINDOWS,
        "test.json",
        hashlib.sha256(manifest_data).hexdigest(),
        "test.json",
        hashlib.sha256(exclusion_data).hexdigest(),
        0,
    )
    monkeypatch.setattr(install_receipt, "current_runtime_spec", lambda: spec)
    monkeypatch.setattr(uninstall_paths, "current_runtime_spec", lambda: spec)
    runtime_manifest = load_runtime_manifest(
        manifests / "test.json",
        spec.manifest_sha256,
        exclusions / "test.json",
        spec.exclusion_sha256,
        spec.exclusion_count,
    )

    def load_synthetic_manifest(
        _path: Path,
        _digest: str,
        _exclusion_path: Path,
        _exclusion_digest: str,
        _count: int,
    ) -> RuntimeTreeManifest:
        return runtime_manifest

    monkeypatch.setattr(
        uninstall_paths,
        "load_runtime_manifest",
        load_synthetic_manifest,
    )
    monkeypatch.setattr(uninstall_paths, "validate_runtime_tree", validate_runtime_tree)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    data.parent.mkdir(parents=True)
    ensure_private_root(data)
    runtime = data / "portable-python-test"
    (runtime / "bin").mkdir(parents=True)
    _ = (runtime / "bin" / "python").write_bytes(payload)
    preserved_files = {
        data / "notification.json": b"preserved-notification",
        data / "webhook.json": b"preserved-webhook",
        data / "notification.log": b"preserved-log",
    }
    for path, payload_bytes in preserved_files.items():
        _ = path.write_bytes(payload_bytes)
    unrelated_runtime = data / "portable-python-unrelated"
    unrelated_runtime.mkdir()
    _ = (unrelated_runtime / "sentinel").write_bytes(b"unrelated")
    unrelated_plugin = home / "plugins" / "cache" / "other" / "unrelated" / "1.0.0"
    unrelated_plugin.mkdir(parents=True)
    unrelated_plugin_bytes = b"unrelated-plugin-bytes"
    _ = (unrelated_plugin / "sentinel").write_bytes(unrelated_plugin_bytes)
    secure_tree(runtime)
    cache = cache_generation(home, "simdorei")
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(
        home,
        source,
        cache,
        runtime=runtime,
        runtime_generation=spec.version,
    )

    # When
    receipt = uninstall(home, source, purge_data=False)

    # Then
    assert not runtime.exists()
    assert (unrelated_runtime / "sentinel").read_bytes() == b"unrelated"
    for path, payload_bytes in preserved_files.items():
        assert path.read_bytes() == payload_bytes
    assert (unrelated_plugin / "sentinel").read_bytes() == unrelated_plugin_bytes
    assert receipt.removed_runtime_roots == 1
