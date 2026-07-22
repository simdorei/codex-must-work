from __future__ import annotations

import re
import tempfile
import tomllib
from pathlib import Path

import pytest

from scripts.codex_config import ConfigMutation, update_codex_config
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError

PREFIX = "codex-must-work@codex-must-work-local:hooks/hooks.json"
EVENTS = (
    "session_start",
    "user_prompt_submit",
    "stop",
)


def _hooks() -> tuple[TrustedHookState, ...]:
    return tuple(
        TrustedHookState(f"{PREFIX}:{event}:0:0", f"sha256:{index:064x}")
        for index, event in enumerate(EVENTS)
    )


@pytest.fixture
def mutation(tmp_path: Path) -> ConfigMutation:
    source = tmp_path / "source"
    source.mkdir()
    return ConfigMutation(source.resolve(), _hooks(), plugin_enabled=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock_temp = tmp_path / "temp"
    lock_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(lock_temp))
    value = tmp_path / "home"
    value.mkdir()
    return value


def _apply(home: Path, mutation: ConfigMutation, raw: bytes | None) -> bytes:
    if raw is not None:
        _ = (home / "config.toml").write_bytes(raw)
    return update_codex_config(home, mutation)


def _reject(home: Path, mutation: ConfigMutation, raw: bytes, reason: str) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(raw)
    with pytest.raises(InstallPluginError) as caught:
        _ = update_codex_config(home, mutation)
    assert caught.value.reason_code == reason
    assert path.read_bytes() == raw


PRESERVE_CASES = [
    (None, b"hide_full_access_warning = true", True),
    (b"", b"", False),
    (b"# before\n[arbitrary]\nvalue = 1", b"# before\n", False),
    (b"[notice]\nhide_world_writable_warning = true\n", b"[notice]\n", True),
    (b"[notice]\nhide_world_writable_warning = false # choice\n", b"false # choice", True),
    (b"[notice]\n# kept\nhide_full_access_warning = false\n\n", b"# kept\n", True),
    (b'[marketplaces.simdorei]\nsource = "keep"\n', b'source = "keep"', False),
    (b"[features]\nplugin_hooks = false # inert\n", b"plugin_hooks = false # inert", False),
    (b"[arbitrary]\r\nvalue = 1\r\n\r\n\r\n", b"[arbitrary]\r\n", False),
]


@pytest.mark.parametrize(("raw", "kept", "has_notice"), PRESERVE_CASES)
def test_preserves_unowned_bytes_and_terminal_suffix(
    home: Path, mutation: ConfigMutation, raw: bytes | None, kept: bytes, has_notice: bool
) -> None:
    updated = _apply(home, mutation, raw)
    expected_suffix = b"\n" if raw is None else raw[len(raw.rstrip(b"\r\n")) :]
    assert kept in updated
    assert updated[len(updated.rstrip(b"\r\n")) :] == expected_suffix
    assert (b"[notice]" in updated) is has_notice
    assert b"last_updated" not in updated


def test_preserves_semantic_allowlist(home: Path, mutation: ConfigMutation) -> None:
    raw = (
        b'title = "user"\n[arbitrary]\nlist = [1, 2]\n'
        b"[notice]\nhide_full_access_warning = false\n"
        b'[hooks]\ninline = [{ event = "Stop", command = ["keep"] }]\n'
        b"[features]\nplugin_hooks = false\nplugins = false\n"
    )
    before = tomllib.loads(raw.decode())
    after = tomllib.loads(_apply(home, mutation, raw).decode())
    assert after["title"] == before["title"]
    assert after["arbitrary"] == before["arbitrary"]
    assert after["notice"] == before["notice"]
    assert after["hooks"]["inline"] == before["hooks"]["inline"]
    assert after["features"]["plugin_hooks"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_crlf_edit_removes_only_stale_owned_hook(
    home: Path, mutation: ConfigMutation, enabled: bool
) -> None:
    stale = f"{PREFIX}:obsolete:9:9"
    similar = "codex-must-work@codex-must-work-locality:hooks/hooks.json:stop:0:0"
    raw = (
        "[features]\r\nplugins   = false   # keep\r\nplugin_hooks = false # inert\r\n"
        f'[hooks.state."{stale}"]\r\nenabled = true\r\ntrusted_hash = "sha256:stale"\r\n'
        f'[hooks.state."{similar}"]\r\nenabled = true\r\ntrusted_hash = "sha256:similar"\r\n'
        '[plugins."codex-must-work@simdorei"]\r\nenabled = true # legacy\r\n'
    ).encode()
    changed = ConfigMutation(mutation.source_root, mutation.trusted_hooks, enabled)
    updated = _apply(home, changed, raw)
    pattern = re.compile(rb'^\[hooks\.state\."([^"]+)"\]\r$', re.MULTILINE)
    owned = {
        match.group(1)
        for match in pattern.finditer(updated)
        if match.group(1).startswith(f"{PREFIX}:".encode())
    }
    assert b"\n" not in updated.replace(b"\r\n", b"")
    assert b"plugins   = true   # keep" in updated
    assert b"enabled = false # legacy" in updated
    assert stale.encode() not in updated
    assert similar.encode() in updated
    assert owned == {hook.key.encode() for hook in _hooks()}


@pytest.mark.parametrize(
    ("ending", "count"),
    [(ending, count) for ending in (b"\n", b"\r\n") for count in range(12)],
)
def test_complete_config_preserves_exact_terminal_bytes(
    home: Path, mutation: ConfigMutation, ending: bytes, count: int
) -> None:
    complete = _apply(home, mutation, None).rstrip(b"\n").replace(b"\n", ending) + ending * count
    _ = (home / "config.toml").write_bytes(complete)
    assert update_codex_config(home, mutation) == complete


UNSUPPORTED = [
    b"[features]\r\nplugins = false\n",
    b"features.plugins = false\n",
    b"features = { plugins = false }\n",
    b'["features"]\nplugins = false\n',
    b"[[features]]\nplugins = false\n",
    b'[features]\nplugins = """false"""\n',
    b'marketplaces.codex-must-work-local.source_type = "local"\n',
    b'marketplaces = { codex-must-work-local = { source_type = "local" } }\n',
    b'[marketplaces."codex-must-work-local"]\nsource_type = "local"\n',
    b'[plugins."codex-must-work@codex-must-work-local"]\nenabled = [true]\n',
    f'[hooks.state."{PREFIX}:event_0:0:0"]\ntrusted_hash = """x"""\n'.encode(),
]
MALFORMED = [
    b"\xff",
    b"[features\nplugins = false\n",
    b"[features]\nplugins = false\n[features]\nplugins = true\n",
]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [(b"\xef\xbb\xbf", "codex_config_bom")]
    + [(raw, "codex_config_malformed") for raw in MALFORMED]
    + [(raw, "codex_config_unsupported_syntax") for raw in UNSUPPORTED],
)
def test_rejects_ambiguous_or_malformed_toml(
    home: Path, mutation: ConfigMutation, raw: bytes, reason: str
) -> None:
    _reject(home, mutation, raw, reason)


INVALID_HOOKS = [
    _hooks()[:-1],
    (*_hooks(), _hooks()[0]),
    (*_hooks()[:-1], _hooks()[0]),
    tuple(TrustedHookState(f"other:{index}", f"sha256:{index:064x}") for index in range(3)),
    tuple(TrustedHookState(f"{PREFIX}:event_{index}:0:0", "bad") for index in range(3)),
]


@pytest.mark.parametrize("trusted_hooks", INVALID_HOOKS)
def test_rejects_non_exact_trust(
    home: Path, mutation: ConfigMutation, trusted_hooks: tuple[TrustedHookState, ...]
) -> None:
    invalid = ConfigMutation(mutation.source_root, trusted_hooks, plugin_enabled=True)
    _reject(home, invalid, b"[x]\ny = 1\n", "invalid_trusted_hook_state")


@pytest.mark.parametrize("kind", ["relative", "source_link"])
def test_rejects_unsafe_source(
    home: Path, mutation: ConfigMutation, tmp_path: Path, kind: str
) -> None:
    source = Path("relative")
    if kind == "source_link":
        source = tmp_path / "source-link"
        source.symlink_to(mutation.source_root, target_is_directory=True)
    invalid = ConfigMutation(source, mutation.trusted_hooks, plugin_enabled=True)
    _reject(home, invalid, b"[x]\ny = 1\n", "unsafe_source_root")


def test_public_pipeline_is_idempotent(home: Path, mutation: ConfigMutation) -> None:
    first = update_codex_config(home, mutation)
    assert update_codex_config(home, mutation) == first
    assert (home / "config.toml").read_bytes() == first
