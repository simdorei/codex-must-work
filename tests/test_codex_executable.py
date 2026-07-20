from pathlib import Path

import pytest

from scripts.codex_executable import CodexExecutableError, resolve_codex_executable


def test_resolver_uses_only_codex_home_sandbox_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    executable = codex_home / ".sandbox-bin" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    workspace_fake = tmp_path / "codex.exe"
    workspace_fake.touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_MUST_WORK_CODEX_EXE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert resolve_codex_executable() == executable.resolve()


def test_resolver_prefers_complete_plugin_app_server_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the legacy sandbox copy lacks the code-mode host, while Codex's plugin bundle has it.
    codex_home = tmp_path / "codex-home"
    sandbox_executable = codex_home / ".sandbox-bin" / "codex.exe"
    sandbox_executable.parent.mkdir(parents=True)
    sandbox_executable.touch()
    plugin_bundle = codex_home / "plugins" / ".plugin-appserver"
    plugin_bundle.mkdir(parents=True)
    plugin_executable = plugin_bundle / "codex.exe"
    plugin_executable.touch()
    (plugin_bundle / "codex-code-mode-host.exe").touch()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # When: the resident manager resolves the trusted Codex executable.
    resolved = resolve_codex_executable()

    # Then: it selects the complete bundle so managed turns can execute tools.
    assert resolved == plugin_executable.resolve()


def test_resolver_fails_when_trusted_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CODEX_MUST_WORK_CODEX_EXE", raising=False)

    with pytest.raises(CodexExecutableError, match="trusted_codex_executable_missing"):
        _ = resolve_codex_executable()


def test_resolver_ignores_workspace_override_and_checks_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    executable = codex_home / ".sandbox-bin" / "codex.exe"
    executable.parent.mkdir(parents=True)
    _ = executable.write_bytes(b"trusted")
    poisoned = tmp_path / "poisoned.exe"
    _ = poisoned.write_bytes(b"poisoned")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_MUST_WORK_CODEX_EXE", str(poisoned))

    assert resolve_codex_executable() == executable
    with pytest.raises(CodexExecutableError, match="trusted_codex_executable_changed"):
        _ = resolve_codex_executable("0" * 64)


def test_resolver_rejects_relative_codex_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", "relative-codex-home")

    with pytest.raises(CodexExecutableError, match="trusted_codex_home_invalid"):
        _ = resolve_codex_executable()
