from __future__ import annotations

import hashlib
import re
from typing import Final

MAX_BLOB_BYTES: Final[int] = 1_048_576
ALLOWED_EXACT: Final[frozenset[str]] = frozenset(
    {
        ".codex-plugin/plugin.json",
        ".gitignore",
        ".mcp.json",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        ".github/workflows/installer-posix.yml",
        "install.ps1",
        "install.sh",
        "pyproject.toml",
        "uninstall.ps1",
        "uninstall.sh",
        "uv.lock",
    }
)
ALLOWED_PREFIXES: Final[tuple[str, ...]] = ("hooks/", "runtime/", "scripts/", "skills/", "tests/")
SCANNABLE_BINARY_EXACT: Final[frozenset[str]] = frozenset({"runtime/launch-python.exe"})
FORBIDDEN_BASENAMES: Final[frozenset[str]] = frozenset(
    {".env", "secrets", "secret", "credentials", "credential", "id_rsa", "id_ed25519"}
)
FORBIDDEN_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".omo", "handoffs", "debug", "cache", "temp", "tmp", ".pytest_cache", ".ruff_cache"}
)

PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9][A-Z0-9 -]{0,80} PRIVATE KEY-----")
TOKEN_RULES: Final[tuple[tuple[str, re.Pattern[bytes]], ...]] = (
    ("SECRET_OPENAI_TOKEN", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("SECRET_STRIPE_TOKEN", re.compile(rb"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("SECRET_GITHUB_TOKEN", re.compile(rb"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("SECRET_GITHUB_TOKEN", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("SECRET_GITLAB_TOKEN", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("SECRET_SLACK_TOKEN", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("SECRET_AWS_ACCESS_KEY", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("SECRET_GOOGLE_API_KEY", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("SECRET_BEARER_TOKEN", re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "SECRET_JWT",
        re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\." + rb"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
)
ASSIGNMENT = re.compile(
    b"".join(
        (
            rb"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|secret[_-]?key|access[_-]?token|",
            rb"auth(?:entication)?[_-]?token|client[_-]?secret|aws_secret_access_key|password|passwd)",
            rb"\b\s*[:=]\s*[\"']?([A-Za-z0-9_+/=-]{20,})",
        )
    )
)
PLACEHOLDERS: Final[frozenset[bytes]] = frozenset(
    {b"placeholder", b"changeme", b"not-a-real-secret", b"example-token"}
)


def finding(blob: bytes) -> str | None:
    """Return a rule for a high-confidence credential, without returning its bytes."""
    if PRIVATE_KEY.search(blob):
        return "SECRET_PRIVATE_KEY"
    for rule, pattern in TOKEN_RULES:
        if pattern.search(blob):
            return rule
    match = ASSIGNMENT.search(blob)
    if match is None:
        return None
    value = match.group(1)
    if value.lower() in PLACEHOLDERS:
        return None
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (rb"[a-z]", rb"[A-Z]", rb"[0-9]", rb"[^A-Za-z0-9]")
    )
    return "SECRET_CREDENTIAL_ASSIGNMENT" if classes >= 3 else None


def is_allowed(path: str) -> bool:
    """Return whether a path belongs to the explicit release allowlist."""
    return path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)


def is_scannable_binary(path: str) -> bool:
    """Return whether a required generated binary may receive pattern scanning."""
    return path in SCANNABLE_BINARY_EXACT


def is_forbidden_name(path: str) -> bool:
    """Return whether a path names a forbidden artifact or diagnostic directory."""
    parts = path.lower().split("/")
    basename = parts[-1]
    return (
        any(part in FORBIDDEN_DIRECTORIES for part in parts)
        or basename in FORBIDDEN_BASENAMES
        or basename.startswith(".env.")
        or basename.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def display_path(path: str) -> str:
    """Display normal paths plainly and hash paths carrying secret/control-like text."""
    raw = path.encode("utf-8", errors="strict")
    unsafe = (
        has_control_path(path)
        or any(pattern.search(raw) for _rule, pattern in TOKEN_RULES)
        or PRIVATE_KEY.search(raw) is not None
        or ASSIGNMENT.search(raw) is not None
    )
    if unsafe:
        digest = hashlib.sha256(raw).hexdigest()
        return f"path_sha256={digest}"
    return path


def has_control_path(path: str) -> bool:
    """Return whether a path contains control or non-printable characters."""
    return any(not char.isprintable() for char in path)
