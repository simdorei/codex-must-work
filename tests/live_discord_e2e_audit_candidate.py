"""Parse an external Todo12 candidate-to-package binding receipt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, override

from tests.live_discord_e2e_audit_records import decode_json

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA: Final = "cmw_candidate_binding_v1"


@dataclass(frozen=True, slots=True)
class CandidateBindingError(RuntimeError):
    """Reject a malformed, self-embedded, or mismatched candidate receipt."""

    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    """Exact source commit and install-package digest produced outside the package."""

    git_sha: str
    package_digest_sha256: str


def load_candidate_binding(path: Path, installed_plugin_root: Path) -> CandidateBinding:
    """Load one external receipt and prevent circular self-embedding."""
    resolved = path.resolve()
    if resolved.is_relative_to(installed_plugin_root.resolve()):
        _fail("candidate_binding_self_embedded")
    try:
        decoded = decode_json(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        reason = "candidate_binding_read_failed"
        raise CandidateBindingError(reason) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        _fail("candidate_binding_invalid")
    git_sha = decoded.get("git_sha")
    package_digest = decoded.get("package_digest_sha256")
    if (
        not isinstance(git_sha, str)
        or not _is_hex(git_sha, 40)
        or not isinstance(package_digest, str)
        or not _is_hex(package_digest, 64)
    ):
        _fail("candidate_binding_invalid")
    return CandidateBinding(git_sha.lower(), package_digest.lower())


def require_candidate_matches(
    binding: CandidateBinding,
    *,
    actual_package_digest_sha256: str,
    expected_git_sha: str | None,
    expected_package_digest_sha256: str | None,
) -> None:
    """Cross-check the receipt against actual installed bytes and optional claims."""
    if binding.package_digest_sha256 != actual_package_digest_sha256.lower():
        _fail("candidate_binding_package_mismatch")
    if expected_git_sha is not None and binding.git_sha != expected_git_sha.lower():
        _fail("candidate_binding_git_mismatch")
    if (
        expected_package_digest_sha256 is not None
        and binding.package_digest_sha256 != expected_package_digest_sha256.lower()
    ):
        _fail("candidate_binding_package_mismatch")


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _fail(reason: str) -> Never:
    raise CandidateBindingError(reason)
