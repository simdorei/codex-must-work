# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Audit one userless Discord continuation without writing in check-only mode."""
# ruff: noqa: EM101, TC001, TC003, TRY300

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.state_io import JsonValue
from tests.live_discord_e2e_audit_authority import (
    DiscordAuthorityError,
    resolve_discord_bot_author_id,
)
from tests.live_discord_e2e_audit_candidate import (
    CandidateBindingError,
    load_candidate_binding,
    require_candidate_matches,
)
from tests.live_discord_e2e_audit_discord import DiscordAuditError
from tests.live_discord_e2e_audit_models import AuditResult, AuditTarget
from tests.live_discord_e2e_audit_preflight import (
    PreflightError,
    evaluate_preflight,
    load_mapping,
    load_preflight_locator,
)
from tests.live_discord_e2e_audit_records import (
    AuditRecord,
    RecordError,
    deduplicate,
    load_discord_records,
    parse_record,
)
from tests.live_discord_e2e_audit_records import (
    load_records as _load_records,
)
from tests.live_discord_e2e_audit_runtime import collect_preflight
from tests.live_discord_e2e_audit_semantics import SemanticAuditError, audit_semantics

AuditError = (
    RecordError,
    SemanticAuditError,
    DiscordAuditError,
    DiscordAuthorityError,
    CandidateBindingError,
)


class AuditArgs(argparse.Namespace):
    """Typed command-line values."""

    rollout: Path
    discord_log: Path
    thread_id: str
    marker: str | None
    discord_bot_author_id: str | None
    expected_sha: str | None
    expected_package_digest_sha256: str | None
    candidate_binding: Path | None
    timeout_seconds: int
    output: Path | None
    check_only: bool
    preflight_only: bool
    require_cmw_inactive: bool
    require_permission_mode: list[str]
    require_goal_absent_or_complete: bool
    require_candidate_binding: bool

    def __init__(self) -> None:
        super().__init__()
        self.rollout = Path()
        self.discord_log = Path()
        self.thread_id = ""
        self.marker = None
        self.discord_bot_author_id = None
        self.expected_sha = None
        self.expected_package_digest_sha256 = None
        self.candidate_binding = None
        self.timeout_seconds = 1
        self.output = None
        self.check_only = False
        self.preflight_only = False
        self.require_cmw_inactive = False
        self.require_permission_mode = []
        self.require_goal_absent_or_complete = False
        self.require_candidate_binding = False


def load_records(path: Path, expected_surface: str) -> tuple[AuditRecord, ...]:
    """Expose the strict JSONL record boundary for tests and reviewers."""
    return _load_records(path, expected_surface)


def audit_records(
    rollout: Sequence[dict[str, str]] | Sequence[AuditRecord],
    discord: Sequence[dict[str, str]] | Sequence[AuditRecord],
    *,
    thread_id: str,
    marker: str,
    discord_bot_author_id: str,
) -> AuditResult:
    """Normalize in-memory fixtures and run the semantic proof."""
    normalized_rollout = _normalize(rollout, "rollout")
    normalized_discord = _normalize(discord, "discord")
    return audit_semantics(
        normalized_rollout,
        normalized_discord,
        AuditTarget(thread_id, marker, discord_bot_author_id),
    )


def parse_args(argv: list[str] | None = None) -> AuditArgs:
    """Parse the deterministic audit and preflight surfaces."""
    parser = argparse.ArgumentParser(prog="live-discord-e2e-audit")
    _ = parser.add_argument("--rollout", type=Path, required=True)
    _ = parser.add_argument("--discord-log", type=Path, required=True)
    _ = parser.add_argument("--thread-id", required=True)
    _ = parser.add_argument("--marker")
    _ = parser.add_argument("--discord-bot-author-id")
    _ = parser.add_argument("--expected-sha")
    _ = parser.add_argument("--expected-package-digest-sha256")
    _ = parser.add_argument("--candidate-binding", type=Path)
    _ = parser.add_argument("--timeout-seconds", type=int, default=1)
    _ = parser.add_argument("--output", type=Path)
    _ = parser.add_argument("--check-only", action="store_true")
    _ = parser.add_argument("--preflight-only", action="store_true")
    _ = parser.add_argument("--require-cmw-inactive", action="store_true")
    _ = parser.add_argument("--require-permission-mode", action="append", default=[])
    _ = parser.add_argument("--require-goal-absent-or-complete", action="store_true")
    _ = parser.add_argument("--require-candidate-binding", action="store_true")
    return parser.parse_args(argv, namespace=AuditArgs())


def main(argv: list[str] | None = None) -> int:
    """Run one public-safe audit and never hide a failed gate."""
    arguments = parse_args(argv)
    try:
        if arguments.preflight_only:
            values = _preflight(arguments)
        else:
            values = _audit(arguments).public_values()
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True)
        _ = sys.stdout.write(encoded + "\n")
        if (
            arguments.output is not None
            and not arguments.check_only
            and not arguments.preflight_only
        ):
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            _ = arguments.output.write_text(encoded + "\n", encoding="utf-8")
        return 0
    except (
        RecordError,
        SemanticAuditError,
        DiscordAuditError,
        DiscordAuthorityError,
        CandidateBindingError,
        PreflightError,
    ) as error:
        _ = sys.stderr.write(json.dumps({"passed": False, "reason": str(error)}) + "\n")
        return 1


def _audit(arguments: AuditArgs) -> AuditResult:
    if not isinstance(arguments.marker, str) or not arguments.marker:
        raise SemanticAuditError("marker_required")
    deadline = time.monotonic() + max(0, arguments.timeout_seconds)
    while True:
        rollout = _load_records(arguments.rollout, "rollout")
        discord = _load_discord_records(arguments.discord_log)
        bot_author_id = resolve_discord_bot_author_id(
            discord,
            arguments.thread_id,
            arguments.discord_bot_author_id,
        )
        target = AuditTarget(arguments.thread_id, arguments.marker, bot_author_id)
        try:
            return audit_semantics(rollout, discord, target)
        except SemanticAuditError:
            if arguments.check_only or time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _preflight(arguments: AuditArgs) -> dict[str, str | bool]:
    expected_package_digest = arguments.expected_package_digest_sha256
    if expected_package_digest is not None and not _is_hex(expected_package_digest, 64):
        raise PreflightError("expected_package_digest_sha256_invalid")
    source_git_sha = arguments.expected_sha
    if source_git_sha is not None and not _is_hex(source_git_sha, 40):
        raise PreflightError("expected_git_sha_invalid")
    locator = load_preflight_locator(arguments.rollout)
    discord = _load_discord_records(arguments.discord_log)
    bot_author_id = resolve_discord_bot_author_id(
        discord,
        arguments.thread_id,
        arguments.discord_bot_author_id,
    )
    mapping = load_mapping(
        arguments.discord_log.parent / "discord_mirror.sqlite", arguments.thread_id
    )
    snapshot = collect_preflight(
        locator,
        arguments.thread_id,
        mapping,
        expected_package_digest,
    )
    required_modes = frozenset(arguments.require_permission_mode)
    if required_modes and snapshot.permission_mode not in required_modes:
        raise PreflightError("approval_required")
    result = evaluate_preflight(snapshot)
    values = result.public_values()
    candidate_verified = False
    if arguments.candidate_binding is not None:
        binding = load_candidate_binding(arguments.candidate_binding, locator.plugin_root)
        require_candidate_matches(
            binding,
            actual_package_digest_sha256=snapshot.actual_package_digest_sha256,
            expected_git_sha=source_git_sha,
            expected_package_digest_sha256=expected_package_digest,
        )
        candidate_verified = True
    elif arguments.require_candidate_binding:
        raise CandidateBindingError("candidate_binding_required")
    values["discord_bot_author_id"] = bot_author_id
    values["candidate_binding_matches"] = candidate_verified
    return values


def _normalize(
    records: Sequence[dict[str, str]] | Sequence[AuditRecord],
    surface: str,
) -> tuple[AuditRecord, ...]:
    normalized: list[AuditRecord] = []
    for index, record in enumerate(records, start=1):
        if isinstance(record, AuditRecord):
            normalized.append(record)
        else:
            values: dict[str, JsonValue] = dict(record)
            normalized.append(parse_record(values, surface, index))
    return deduplicate(normalized)


def _load_discord_records(path: Path) -> tuple[AuditRecord, ...]:
    records: list[AuditRecord] = []
    backup = path.with_name(path.name + ".bak")
    for candidate in (backup, path):
        if candidate.is_file():
            records.extend(load_discord_records(candidate))
    if not records:
        raise RecordError("record_read_failed")
    return deduplicate(records)


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


if __name__ == "__main__":
    raise SystemExit(main())
