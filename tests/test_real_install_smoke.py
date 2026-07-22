from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import real_install_smoke as smoke
from tests import real_install_smoke_ledger as ledger
from tests import real_install_smoke_support as support
from tests.real_install_smoke_fixtures import HEAD as _HEAD
from tests.real_install_smoke_fixtures import TREE as _TREE
from tests.real_install_smoke_fixtures import config as _config
from tests.real_install_smoke_fixtures import rollout_records as _rollout_records
from tests.real_install_smoke_fixtures import trust as _trust


def test_source_gate_requires_exact_detached_or_original_clean_checkout(tmp_path: Path) -> None:
    # Given: an absolute original checkout and exact Git answers.
    source = (tmp_path / "source").resolve()
    source.mkdir()
    answers = {
        ("rev-parse", "HEAD"): (0, _HEAD),
        ("rev-parse", "HEAD^{tree}"): (0, _TREE),
        ("symbolic-ref", "-q", "HEAD"): (0, "refs/heads/main"),
        ("rev-parse", "--git-dir"): (0, ".git"),
        ("rev-parse", "--git-common-dir"): (0, ".git"),
        ("diff", "--quiet"): (0, ""),
        ("diff", "--cached", "--quiet"): (0, ""),
    }

    # When / Then: exact expected identities pass without accepting PATH output.
    support.verify_source(source, _HEAD, _TREE, lambda args: answers[args])


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        (("rev-parse", "HEAD"), "source_head_mismatch"),
        (("rev-parse", "HEAD^{tree}"), "source_tree_mismatch"),
        (("diff", "--quiet"), "source_tracked_worktree_dirty"),
        (("diff", "--cached", "--quiet"), "source_index_dirty"),
        (("rev-parse", "--git-common-dir"), "source_worktree_not_candidate_or_original"),
    ],
)
def test_source_gate_rejects_every_false_positive(
    tmp_path: Path, changed: tuple[str, ...], reason: str
) -> None:
    # Given: one wrong Git record among otherwise valid original-worktree records.
    source = (tmp_path / "source").resolve()
    source.mkdir()
    valid = {
        ("rev-parse", "HEAD"): (0, _HEAD),
        ("rev-parse", "HEAD^{tree}"): (0, _TREE),
        ("symbolic-ref", "-q", "HEAD"): (0, "refs/heads/main"),
        ("rev-parse", "--git-dir"): (0, ".git"),
        ("rev-parse", "--git-common-dir"): (0, ".git"),
        ("diff", "--quiet"): (0, ""),
        ("diff", "--cached", "--quiet"): (0, ""),
    }
    valid[changed] = (1, "") if changed[0] == "diff" else (0, "wrong")

    # When / Then: the gate names the exact violated proof.
    with pytest.raises(support.SmokeError, match=reason):
        support.verify_source(source, _HEAD, _TREE, lambda args: valid[args])


def test_home_and_auth_preconditions_do_not_read_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a normal-looking home and a present opaque auth file.
    home = (tmp_path.parent / "profile" / ".codex").resolve()
    home.mkdir(parents=True)
    auth = home / "auth.json"
    auth.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("auth bytes were read"))

    # When / Then: presence is enough and no secret content is opened.
    support.require_auth_presence(home)


@pytest.mark.parametrize("name", ["cmw-plan-codex-home.fixture", "reviewer-home"])
def test_real_home_gate_rejects_temporary_or_reviewer_home(tmp_path: Path, name: str) -> None:
    # Given / When / Then: fixture/reviewer homes cannot be mistaken for the normal home gate.
    home = (tmp_path / name).resolve()
    home.mkdir()
    with pytest.raises(support.SmokeError, match="normal_codex_home_required"):
        support.require_normal_home(home, tmp_path.resolve())


def test_byte_span_ledger_accepts_only_owned_delta(tmp_path: Path) -> None:
    # Given: pre/post config bytes differing only in the allowed feature/CMW spans.
    cache = (tmp_path / "cache").resolve()
    before = _config(cache, plugins=False, cmw=False)
    after = _config(cache, plugins=True, cmw=True)

    # When / Then: semantic and complementary-byte ledgers both accept the transition.
    result = ledger.verify_config_transition(before, after, cache, _trust())
    assert result == ledger.ConfigCheck(
        allowed_delta_exact=True,
        non_cmw_bytes_unchanged=True,
        trust_count=3,
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: data.replace(b"# keep", b"# CHANGED", 1),
        lambda data: data.replace(b"sha256:lazy", b"sha256:other", 1),
        lambda data: data.replace(b"\r\n", b"\n", 1),
    ],
)
def test_byte_span_ledger_rejects_complementary_byte_changes(
    tmp_path: Path, mutator: Callable[[bytes], bytes]
) -> None:
    # Given: an otherwise valid install plus one non-CMW byte-span mutation.
    cache = (tmp_path / "cache").resolve()
    before = _config(cache, plugins=False, cmw=False)
    after = mutator(_config(cache, plugins=True, cmw=True))

    # When / Then: semantic equality cannot hide byte/comment/line-ending damage.
    with pytest.raises(ledger.SmokeError, match="non_cmw_config_bytes_changed"):
        ledger.verify_config_transition(before, after, cache, _trust())


@pytest.mark.parametrize("mode", ["wrong_count", "wrong_record", "wrong_source"])
def test_config_ledger_rejects_wrong_owned_transition(tmp_path: Path, mode: str) -> None:
    # Given: a valid baseline and one installer-owned false positive.
    cache = (tmp_path / "cache").resolve()
    before = _config(cache, plugins=False, cmw=False)
    after = _config(cache, plugins=True, cmw=True)
    trust = _trust()
    if mode == "wrong_count":
        trust = trust[:-1]
    elif mode == "wrong_record":
        trust = (*trust[:-1], ledger.TrustEntry(trust[-1].key, "sha256:" + "f" * 64))
    else:
        cache = (tmp_path / "swapped-cache").resolve()

    # When / Then: exact lifecycle-hook/source/hash semantics are mandatory.
    with pytest.raises(ledger.SmokeError, match="allowed_config_delta_mismatch"):
        ledger.verify_config_transition(before, after, cache, trust)


@pytest.mark.parametrize("change", ["bytes", "metadata", "identity", "membership"])
def test_second_install_no_write_rejects_every_mutation(tmp_path: Path, change: str) -> None:
    # Given: a complete in-memory before/after filesystem ledger.
    root = tmp_path / "home"
    root.mkdir()
    item = root / "state"
    item.write_bytes(b"before")
    before = ledger.snapshot_tree(root)
    if change == "bytes":
        item.write_bytes(b"after!")
    elif change == "metadata":
        item.chmod(0o600 if os.name != "nt" else 0o444)
    elif change == "identity":
        replacement = root / "replacement"
        replacement.write_bytes(b"before")
        replacement.replace(item)
    else:
        (root / "extra").write_bytes(b"new")

    # When / Then: second-install byte, metadata, identity, and membership writes fail closed.
    with pytest.raises(ledger.SmokeError, match="second_install_wrote_state"):
        ledger.require_same_tree(before, ledger.snapshot_tree(root))


def test_effective_source_ledger_rejects_source_swap(tmp_path: Path) -> None:
    # Given: one effective hook source replaced with byte-identical content.
    source = tmp_path / "hooks.json"
    source.write_bytes(b'{"hooks":{"UserPromptSubmit":[]}}')
    before = ledger.snapshot_sources((source,))
    replacement = tmp_path / "replacement"
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)

    # When / Then: identity binding catches a byte-identical source swap.
    with pytest.raises(ledger.SmokeError, match="effective_hook_sources_changed"):
        ledger.require_same_sources(before, ledger.snapshot_sources((source,)))


def test_rollout_accepts_distinct_session_and_first_prompt_turns(tmp_path: Path) -> None:
    # Given: exact CMW thread context and Lazy first-prompt records on distinct turns.
    rollout, records, visible = _rollout_records(tmp_path)

    # When / Then: no false same-turn requirement is imposed.
    result = support.verify_rollout(
        rollout,
        records,
        visible,
        tmp_path / "cmw/hooks/hooks.json",
        tmp_path / "lazy/hooks/hooks.json",
    )
    assert result == support.RolloutCheck(
        locator_matches=True,
        lazy_context_matches=True,
        visible_output_matches=True,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("combined", "hook_record_identity_mismatch"),
        ("wrong_header", "lazy_context_header_invalid"),
        ("different_turn", "lazy_first_prompt_binding_invalid"),
        ("ambiguous_engine", "lazy_engine_invalid"),
        ("warning", "lazy_warning_invalid"),
        ("locator", "cmw_locator_identity_invalid"),
        ("prefix", "visible_prefix_invalid"),
        ("tail", "visible_tail_invalid"),
        ("prompt_marker_only", "visible_prefix_invalid"),
    ],
)
def test_rollout_rejects_false_positive_records(tmp_path: Path, mutation: str, reason: str) -> None:
    # Given: a valid synthetic rollout with one false-positive condition.
    rollout, records, visible = _rollout_records(tmp_path)
    cmw_run = records[2]["payload"]["run"]
    lazy_run = records[3]["payload"]["run"]
    if mutation == "combined":
        cmw_run["entries"].extend(lazy_run["entries"])
    elif mutation == "wrong_header":
        original = lazy_run["entries"][1]["text"]
        lazy_run["entries"][1]["text"] = original.replace("Prompt", "Wrong", 1)
    elif mutation == "different_turn":
        records[3]["payload"]["turn_id"] = "turn-later"
    elif mutation == "ambiguous_engine":
        lazy_run["entries"][1]["text"] += "\nRewrite engine: second"
    elif mutation == "warning":
        lazy_run["entries"][0]["text"] += " extra"
    elif mutation == "locator":
        cmw_run["entries"][0]["text"] = cmw_run["entries"][0]["text"].replace("019b", "019c")
    elif mutation == "prefix":
        visible[0] = "wrong"
    elif mutation == "tail":
        visible[-1] = "not done"
    else:
        visible[:] = ["user prompt contains SMOKE_OK"]
    with pytest.raises(support.SmokeError, match=reason):
        support.verify_rollout(
            rollout,
            records,
            visible,
            tmp_path / "cmw/hooks/hooks.json",
            tmp_path / "lazy/hooks/hooks.json",
        )


def test_privacy_safe_output_rejects_paths_digests_bodies_or_credentials() -> None:
    # Given: the complete allowed persisted key/value surface.
    check = ledger.ConfigCheck(
        allowed_delta_exact=True,
        non_cmw_bytes_unchanged=True,
        trust_count=3,
    )
    safe = support.safe_output(_HEAD, check, 0, 0)

    # When / Then: only booleans, counts, exit codes, prefix/tail status, and candidate SHA persist.
    assert "second_install_no_write=true" in safe
    assert "allowed_delta_exact=true" in safe
    assert _HEAD in safe
    assert not any(token in safe.lower() for token in ("path=", "digest=", "auth", "translation="))


def test_cli_requires_all_absolute_arguments_and_idempotent_flag(tmp_path: Path) -> None:
    # Given: the complete explicit command surface.
    source = (tmp_path / "source").resolve()
    home = (tmp_path / "home").resolve()

    # When / Then: parsing succeeds only with the binding flag and absolute paths.
    parsed = smoke.parse_args(
        [
            "--codex-home",
            str(home),
            "--source-root",
            str(source),
            "--expected-source-head",
            _HEAD,
            "--expected-source-tree",
            _TREE,
            "--verify-idempotent-reinstall",
        ]
    )
    assert parsed.codex_home == home
    with pytest.raises(SystemExit):
        smoke.parse_args(["--codex-home", "relative"])


def test_child_commands_use_absolute_runtime_and_child_only_codex_home(tmp_path: Path) -> None:
    # Given: a selected absolute runtime, hostile parent CODEX_HOME, and recording runner.
    home = (tmp_path / "normal" / ".codex").resolve()
    runtime = (home / ".sandbox-bin" / ("codex.exe" if os.name == "nt" else "codex")).resolve()
    environment = {"CODEX_HOME": "must-not-be-parent-mutated", "PATH": "host-path"}
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(argv: tuple[str, ...], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "", "")

    # When: only the final selected runtime is launched for the neutral request.
    smoke.run_codex(runtime, home, runner, environment)

    # Then: no bare Codex/PATH lookup occurs and parent environment is unchanged.
    assert Path(calls[0][0][0]).is_absolute()
    assert calls[0][0][1:3] == ("exec", "--json")
    assert calls[0][1]["CODEX_HOME"] == str(home)
    assert environment["CODEX_HOME"] == "must-not-be-parent-mutated"
