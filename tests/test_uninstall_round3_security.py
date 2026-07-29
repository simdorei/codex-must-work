from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.config_publication import ConfigSnapshot
from scripts.hook_trust import trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import LEGACY_MARKETPLACE_NAME, MARKETPLACE_NAME
from scripts.private_root import ensure_private_root
from scripts.toml_lexical_headers import scan_table_headers
from scripts.uninstall_config import render_config_removal
from scripts.uninstall_plugin import uninstall
from tests.uninstall_test_support import authorize_install, cache_generation


def _snapshot(data: bytes) -> ConfigSnapshot:
    return ConfigSnapshot(data, None, None, Path("config.toml"), (1, 1))


@pytest.mark.parametrize("delimiter", ['"""', "'''"])
def test_assignment_lookalikes_in_multiline_strings_and_nested_tables_survive(
    delimiter: str,
) -> None:
    raw = (
        f"note = {delimiter}\n"
        'marketplaces.simdorei = { source_type = "git" }\n'
        f"{delimiter}\n"
        "[other]\n"
        'marketplaces.simdorei = "nested-and-unrelated"\n'
    ).encode()

    rendered = render_config_removal(_snapshot(raw), ())

    assert rendered == raw


def test_multiline_array_items_are_not_table_headers() -> None:
    source = "values = [\n  [1, 2],\n  [3, 4]\n]\n[other]\nvalue = true\n"

    headers = scan_table_headers(source)

    assert tuple(header.text for header in headers) == ("[other]",)


@pytest.mark.parametrize(
    "target",
    [
        (
            'marketplaces.simdorei.source_type = "git"\n'
            "marketplaces.simdorei.source = "
            '"https://github.com/simdorei/codex-must-work.git"\n'
            'marketplaces.simdorei.ref = "main"\n'
        ),
        (
            'marketplaces = { simdorei = { source_type = "git", '
            'source = "https://github.com/simdorei/codex-must-work.git", ref = "main" } }\n'
        ),
        (
            '[marketplaces."simdorei"]\n'
            'source_type = "git"\n'
            'source = "https://github.com/simdorei/codex-must-work.git"\n'
            'ref = "main"\n'
        ),
        (
            "[marketplaces.simdorei] # noncanonical header\n"
            'source_type = "git"\n'
            'source = "https://github.com/simdorei/codex-must-work.git"\n'
            'ref = "main"\n'
        ),
        (
            "[[marketplaces.simdorei]]\n"
            'source_type = "git"\n'
            'source = "https://github.com/simdorei/codex-must-work.git"\n'
            'ref = "main"\n'
        ),
    ],
    ids=("dotted", "inline", "quoted-equivalent", "commented-header", "array-of-tables"),
)
def test_noncanonical_target_forms_fail_before_config_cache_or_data_mutation(
    tmp_path: Path,
    target: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    raw = target.encode()
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    data.parent.mkdir(parents=True)
    ensure_private_root(data)
    sentinel = data / "keep.txt"
    _ = sentinel.write_bytes(b"keep")
    _ = authorize_install(home, source, cache)

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=True)

    assert caught.value.reason_code == "codex_config_unsupported_syntax"
    assert config.read_bytes() == raw
    assert cache.is_dir()
    assert sentinel.read_bytes() == b"keep"


def test_unrelated_legacy_source_is_rejected_without_any_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cache = cache_generation(home, LEGACY_MARKETPLACE_NAME)
    source = cache
    hook = trusted_hook_states_for_plugin(cache, LEGACY_MARKETPLACE_NAME)[0]
    raw = _legacy_config("C:/unrelated-user-tree", hook.key, hook.trusted_hash)
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
    data.parent.mkdir(parents=True)
    ensure_private_root(data)
    sentinel = data / "keep.txt"
    _ = sentinel.write_bytes(b"keep")

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=True)

    assert caught.value.reason_code == "uninstall_receipt_reinstall_required"
    assert config.read_bytes() == raw
    assert cache.is_dir()
    assert sentinel.read_bytes() == b"keep"


def test_target_config_without_validated_cache_evidence_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    raw = (
        b"[marketplaces.simdorei]\n"
        b'source_type = "git"\n'
        b'source = "https://github.com/simdorei/codex-must-work.git"\n'
        b'ref = "main"\n'
    )
    config = home / "config.toml"
    _ = config.write_bytes(raw)

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)

    assert caught.value.reason_code == "uninstall_receipt_reinstall_required"
    assert config.read_bytes() == raw


def test_legacy_source_requires_same_validated_package_digest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, LEGACY_MARKETPLACE_NAME)
    hook = trusted_hook_states_for_plugin(cache, LEGACY_MARKETPLACE_NAME)[0]
    raw = _legacy_config(str(source.resolve()), hook.key, hook.trusted_hash)
    config = home / "config.toml"
    _ = config.write_bytes(raw)

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)

    assert caught.value.reason_code == "uninstall_receipt_reinstall_required"
    assert config.read_bytes() == raw
    assert cache.is_dir()


@pytest.mark.parametrize("marketplace", [MARKETPLACE_NAME, LEGACY_MARKETPLACE_NAME])
def test_changed_trusted_hash_is_rejected_without_any_mutation(
    tmp_path: Path,
    marketplace: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cache = cache_generation(home, marketplace)
    source = cache if marketplace == LEGACY_MARKETPLACE_NAME else Path(__file__).parents[1]
    hook = trusted_hook_states_for_plugin(cache, marketplace)[0]
    changed = hook.trusted_hash[:-1] + ("0" if hook.trusted_hash[-1] != "0" else "1")
    raw = (
        _canonical_config(hook.key, changed)
        if marketplace == MARKETPLACE_NAME
        else _legacy_config(str(source.resolve()), hook.key, changed)
    )
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    if marketplace == MARKETPLACE_NAME:
        _ = authorize_install(home, source, cache)

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)

    expected = (
        "uninstall_config_ownership_unknown"
        if marketplace == MARKETPLACE_NAME
        else "uninstall_receipt_reinstall_required"
    )
    assert caught.value.reason_code == expected
    assert config.read_bytes() == raw
    assert cache.is_dir()


def test_arbitrary_trusted_hash_is_rejected_without_any_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    hook = trusted_hook_states_for_plugin(cache, MARKETPLACE_NAME)[0]
    arbitrary = "sha256:" + ("0" * 64)
    assert arbitrary != hook.trusted_hash
    raw = _canonical_config(hook.key, arbitrary)
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    _ = authorize_install(home, source, cache)

    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)

    assert caught.value.reason_code == "uninstall_config_ownership_unknown"
    assert config.read_bytes() == raw
    assert cache.is_dir()


def _canonical_config(key: str, trusted_hash: str) -> bytes:
    return (
        "[marketplaces.simdorei]\n"
        'source_type = "git"\n'
        'source = "https://github.com/simdorei/codex-must-work.git"\n'
        'ref = "main"\n'
        f'[hooks.state."{key}"]\n'
        "enabled = true\n"
        f"trusted_hash = {json.dumps(trusted_hash)}\n"
    ).encode()


def _legacy_config(source: str, key: str, trusted_hash: str) -> bytes:
    return (
        "[marketplaces.codex-must-work-local]\n"
        'source_type = "local"\n'
        f"source = {json.dumps(source)}\n"
        f'[hooks.state."{key}"]\n'
        "enabled = true\n"
        f"trusted_hash = {json.dumps(trusted_hash)}\n"
    ).encode()
