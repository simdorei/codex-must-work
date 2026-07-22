from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Never, Protocol

from tests.real_install_smoke_ledger import ConfigCheck, SmokeError

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RolloutCheck:
    locator_matches: bool
    lazy_context_matches: bool
    visible_output_matches: bool


class GitProbe(Protocol):
    def __call__(self, arguments: tuple[str, ...]) -> tuple[int, str]: ...


def verify_source(root: Path, expected_head: str, expected_tree: str, probe: GitProbe) -> None:
    if not root.is_absolute() or not root.is_dir() or root.resolve(strict=True) != root:
        _fail("source_root_invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected_head):
        _fail("expected_source_head_invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected_tree):
        _fail("expected_source_tree_invalid")
    if probe(("rev-parse", "HEAD")) != (0, expected_head):
        _fail("source_head_mismatch")
    if probe(("rev-parse", "HEAD^{tree}")) != (0, expected_tree):
        _fail("source_tree_mismatch")
    _verify_checkout_kind(root, probe)
    if probe(("diff", "--quiet"))[0] != 0:
        _fail("source_tracked_worktree_dirty")
    if probe(("diff", "--cached", "--quiet"))[0] != 0:
        _fail("source_index_dirty")


def _verify_checkout_kind(root: Path, probe: GitProbe) -> None:
    symbolic_status, _ = probe(("symbolic-ref", "-q", "HEAD"))
    if symbolic_status == 0:
        git_dir = _git_path(root, probe(("rev-parse", "--git-dir")))
        common_dir = _git_path(root, probe(("rev-parse", "--git-common-dir")))
        if git_dir != common_dir:
            _fail("source_worktree_not_candidate_or_original")
    elif symbolic_status != 1:
        _fail("source_git_state_unverifiable")


def require_normal_home(home: Path, temporary_root: Path) -> None:
    try:
        direct = home.is_absolute() and home.resolve(strict=True) == home and home.is_dir()
    except (OSError, RuntimeError):
        direct = False
    fixture_named = any(
        part.lower().startswith("cmw-plan-codex-home") or "reviewer" in part.lower()
        for part in home.parts
    )
    if not direct or home.is_relative_to(temporary_root) or fixture_named:
        _fail("normal_codex_home_required")


def require_auth_presence(home: Path) -> None:
    try:
        metadata = (home / "auth.json").lstat()
    except OSError as error:
        reason = "codex_auth_missing"
        raise SmokeError(reason) from error
    invalid = (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size == 0
    )
    if invalid:
        _fail("codex_auth_missing")


def verify_rollout(
    rollout: Path,
    records: list[dict[str, object]],
    visible: list[str],
    cmw_source: Path,
    lazy_source: Path,
) -> RolloutCheck:
    session_id, first_turn = _rollout_identity(records)
    cmw = _matching_runs(records, cmw_source, "session_start")
    lazy = _matching_runs(records, lazy_source, "user_prompt_submit")
    if len(cmw) != 1 or len(lazy) != 1:
        _fail("hook_record_identity_mismatch")
    _, cmw_run = cmw[0]
    lazy_payload, lazy_run = lazy[0]
    locator_text = _cmw_locator_text(cmw_run)
    warning, context = _lazy_entries(lazy_payload, lazy_run, first_turn)
    if not _locator_matches(locator_text, session_id, rollout):
        _fail("cmw_locator_identity_invalid")
    notice, engine = _lazy_context(context)
    if warning != f"{notice} ({engine})":
        _fail("lazy_warning_invalid")
    if _model_contexts(records).count(context) != 1:
        _fail("lazy_model_context_missing")
    if not visible or visible[0].splitlines()[0] != notice:
        _fail("visible_prefix_invalid")
    if not visible[-1].splitlines() or visible[-1].splitlines()[-1] != "SMOKE_OK":
        _fail("visible_tail_invalid")
    return RolloutCheck(
        locator_matches=True,
        lazy_context_matches=True,
        visible_output_matches=True,
    )


def _rollout_identity(records: list[dict[str, object]]) -> tuple[str, str]:
    sessions = [record for record in records if record.get("type") == "session_meta"]
    turns = [
        payload.get("turn_id")
        for record in records
        if isinstance(payload := record.get("payload"), dict)
        and payload.get("type") == "task_started"
    ]
    if len(sessions) != 1 or len(turns) != 1 or not isinstance(turns[0], str):
        _fail("rollout_identity_invalid")
    session_payload = sessions[0].get("payload")
    session_id = session_payload.get("id") if isinstance(session_payload, dict) else None
    if not isinstance(session_id, str):
        _fail("rollout_identity_invalid")
    return session_id, turns[0]


def _cmw_locator_text(cmw_run: dict[str, object]) -> str:
    cmw_entries = cmw_run.get("entries")
    if cmw_run.get("scope") != "thread" or not _entry_shape(cmw_entries, ("context",)):
        _fail("hook_record_identity_mismatch")
    return cmw_entries[0]["text"]


def _lazy_entries(
    lazy_payload: dict[str, object], lazy_run: dict[str, object], first_turn: str
) -> tuple[str, str]:
    lazy_entries = lazy_run.get("entries")
    if lazy_payload.get("turn_id") != first_turn or lazy_run.get("scope") != "turn":
        _fail("lazy_first_prompt_binding_invalid")
    if not _entry_shape(lazy_entries, ("warning", "context")):
        _fail("hook_record_identity_mismatch")
    return lazy_entries[0]["text"], lazy_entries[1]["text"]


def safe_output(candidate_sha: str, config: ConfigCheck, first_exit: int, second_exit: int) -> str:
    values = (
        ("candidate_sha", candidate_sha),
        ("first_install_exit", str(first_exit)),
        ("second_install_exit", str(second_exit)),
        ("allowed_delta_exact", str(config.allowed_delta_exact).lower()),
        ("non_cmw_bytes_unchanged", str(config.non_cmw_bytes_unchanged).lower()),
        ("new_local_trust_count", str(config.trust_count)),
        ("non_cmw_user_prompt_hooks_unchanged", "true"),
        ("non_cmw_user_prompt_sources_unchanged", "true"),
        ("lazy_settings_unchanged", "true"),
        ("second_install_no_write", "true"),
        ("new_rollout_count", "1"),
        ("visible_prefix_exact", "true"),
        ("final_tail_smoke_ok", "true"),
    )
    return "".join(f"{key}={value}\n" for key, value in values)


def _git_path(root: Path, result: tuple[int, str]) -> Path:
    status, value = result
    if status != 0 or not value:
        _fail("source_git_state_unverifiable")
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve(strict=False)


def _matching_runs(
    records: list[dict[str, object]], source: Path, event: str
) -> list[tuple[dict[str, object], dict[str, object]]]:
    matched: list[tuple[dict[str, object], dict[str, object]]] = []
    expected = source.resolve(strict=False)
    for record in records:
        payload = record.get("payload")
        run = payload.get("run") if isinstance(payload, dict) else None
        path = run.get("source_path") if isinstance(run, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("type") == "hook_completed"
            and isinstance(run, dict)
            and run.get("event_name") == event
            and run.get("source") == "plugin"
            and run.get("status") == "completed"
            and isinstance(path, str)
            and Path(path).resolve(strict=False) == expected
        ):
            matched.append((payload, run))
    return matched


def _entry_shape(value: object, kinds: tuple[str, ...]) -> bool:
    return (
        isinstance(value, list)
        and [entry.get("kind") for entry in value if isinstance(entry, dict)] == list(kinds)
        and all(
            isinstance(entry.get("text"), str)
            for entry in value
            if isinstance(entry, dict)
        )
    )


def _locator_matches(text: str, session_id: str, rollout: Path) -> bool:
    try:
        root = json.loads(text)
    except json.JSONDecodeError:
        return False
    locator = root.get("codex_must_work_locator") if isinstance(root, dict) else None
    return (
        isinstance(locator, dict)
        and locator.get("session_id") == session_id
        and isinstance(locator.get("transcript_path"), str)
        and Path(locator["transcript_path"]).resolve(strict=False) == rollout.resolve(strict=False)
    )


def _lazy_context(context: str) -> tuple[str, str]:
    lines = context.splitlines()
    if not lines or lines[0] != "Prompt translation/correction hook is active.":
        _fail("lazy_context_header_invalid")
    engines = [
        line.removeprefix("Rewrite engine: ")
        for line in lines
        if line.startswith("Rewrite engine: ")
    ]
    prefix = "Start only the first visible assistant message in this turn with this exact line: "
    notices = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(engines) != 1 or not engines[0] or len(notices) != 1 or not notices[0]:
        _fail("lazy_engine_invalid")
    return notices[0], engines[0]


def _model_contexts(records: list[dict[str, object]]) -> list[str]:
    contexts: list[str] = []
    for record in records:
        payload = record.get("payload")
        is_developer = isinstance(payload, dict) and payload.get("role") == "developer"
        content = payload.get("content") if is_developer else None
        if isinstance(content, list):
            contexts.extend(
                text
                for item in content
                if isinstance(item, dict) and isinstance(text := item.get("text"), str)
            )
    return contexts


def _fail(reason: str) -> Never:
    raise SmokeError(reason)
