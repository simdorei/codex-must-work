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
