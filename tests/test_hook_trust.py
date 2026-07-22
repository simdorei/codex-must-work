import json
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Protocol, assert_never

import pytest

from scripts.hook_trust import HookPlatform, TrustedHookState, trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()
_SOURCE_ROOT: Final = Path(__file__).parents[1]
_KEYS: Final = (
    "codex-must-work@codex-must-work-local:hooks/hooks.json:session_start:0:0",
    "codex-must-work@codex-must-work-local:hooks/hooks.json:user_prompt_submit:0:0",
    "codex-must-work@codex-must-work-local:hooks/hooks.json:stop:0:0",
)
_WINDOWS_HASHES: Final = (
    "sha256:bc47873f83656027c970cf9b656dbe96f1809f20eb658b5843529e4be0788e1c",
    "sha256:92c01da61756375d56bbdc11d5cad9b8ef927d8a1aa820f875ba32336838f297",
    "sha256:b941d4836119cbe4f147c4fd0d28ccc170089e48f3683eb5130cdc4df28c2324",
)
_POSIX_HASHES: Final = (
    "sha256:002cb7f93a1c9ac91f32267bba2b9d579eb6981536fbd30b7007c46aa1d95621",
    "sha256:01dfbd0d3b4ad7a75224890f11197c1942caa7b22da9ee5fc674a03d93cc9da0",
    "sha256:e06d698ff9789ae863e2a829d7459d7717a977726298dca5691bf65ec6951093",
)


def _copy_plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    manifest = root / ".codex-plugin" / "plugin.json"
    hooks = root / "hooks" / "hooks.json"
    manifest.parent.mkdir(parents=True)
    hooks.parent.mkdir(parents=True)
    _ = shutil.copy2(_SOURCE_ROOT / ".codex-plugin" / "plugin.json", manifest)
    _ = shutil.copy2(_SOURCE_ROOT / "hooks" / "hooks.json", hooks)
    raw = _read_object(manifest)
    _ = raw.pop("hooks", None)
    _write_object(manifest, raw)
    return root


def _read_object(path: Path) -> JsonObject:
    parsed = _LOAD_JSON(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _write_object(path: Path, value: JsonObject) -> None:
    _ = path.write_text(json.dumps(value), encoding="utf-8")


def _events(document: JsonObject) -> JsonObject:
    events = document["hooks"]
    assert isinstance(events, dict)
    return events


def _group(events: JsonObject, event: str) -> JsonObject:
    groups = events[event]
    assert isinstance(groups, list)
    assert len(groups) == 1
    group = groups[0]
    assert isinstance(group, dict)
    return group


def _handler(events: JsonObject, event: str) -> JsonObject:
    handlers = _group(events, event)["hooks"]
    assert isinstance(handlers, list)
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, dict)
    return handler


def _states(root: Path, platform: HookPlatform) -> tuple[TrustedHookState, ...]:
    return trusted_hook_states_for_plugin(root, "codex-must-work-local", platform)


@contextmanager
def _passthrough() -> Generator[None]:
    yield


def test_shared_install_error_survives_generator_context_manager() -> None:
    # Given: a shared installer error crosses a generator context manager.
    # When/Then: Python can preserve its traceback instead of masking the typed error.
    reason = "expected_failure"
    with pytest.raises(InstallPluginError, match=reason), _passthrough():
        raise InstallPluginError(reason)


@pytest.mark.parametrize(
    ("platform", "hashes"),
    [(HookPlatform.WINDOWS, _WINDOWS_HASHES), (HookPlatform.POSIX, _POSIX_HASHES)],
)
def test_golden_hook_keys_and_hashes_are_complete(
    platform: HookPlatform,
    hashes: tuple[str, ...],
) -> None:
    # Given: the checked-in manifest whose commands retain ${PLUGIN_ROOT} placeholders.
    # When: Codex-compatible trust states are calculated for one command platform.
    states = _states(_SOURCE_ROOT, platform)

    # Then: every complete persisted key and SHA-256 matches the pinned Codex contract.
    assert tuple(state.key for state in states) == _KEYS
    assert tuple(state.trusted_hash for state in states) == hashes


def test_default_manifest_discovers_exact_hook_keys(tmp_path: Path) -> None:
    # Given: a manifest without an explicit hooks field.
    root = _copy_plugin(tmp_path)

    # When: Codex's default hooks/hooks.json discovery is mirrored.
    states = _states(root, HookPlatform.POSIX)

    # Then: the same exact three source-relative keys are produced.
    assert tuple(state.key for state in states) == _KEYS


@pytest.mark.parametrize("declaration", ["./hooks/hooks.json", [".\\hooks\\hooks.json"]])
def test_explicit_hook_path_normalizes_to_exact_source(
    tmp_path: Path,
    declaration: JsonValue,
) -> None:
    # Given: the approved hook path expressed with optional dot and native separators.
    root = _copy_plugin(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = _read_object(manifest_path)
    manifest["hooks"] = declaration
    _write_object(manifest_path, manifest)

    # When: trust states are calculated.
    states = _states(root, HookPlatform.WINDOWS)

    # Then: the persisted keys use Codex's normalized POSIX relative path.
    assert tuple(state.key for state in states) == _KEYS


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_matcher_is_discarded_for_matcherless_events(tmp_path: Path, event: str) -> None:
    # Given: a matcher is injected on an event for which Codex ignores matchers.
    root = _copy_plugin(tmp_path)
    hooks_path = root / "hooks" / "hooks.json"
    before, document = _states(root, HookPlatform.POSIX), _read_object(hooks_path)
    _group(_events(document), event)["matcher"] = "ignored-value"
    _write_object(hooks_path, document)

    # When: the modified manifest is fingerprinted.
    after = _states(root, HookPlatform.POSIX)

    # Then: its complete canonical trust map is unchanged.
    assert after == before


def test_matcher_changes_hash_for_matcher_supporting_event(tmp_path: Path) -> None:
    # Given: SessionStart receives a meaningful Codex matcher.
    root = _copy_plugin(tmp_path)
    hooks_path = root / "hooks" / "hooks.json"
    before, document = _states(root, HookPlatform.POSIX), _read_object(hooks_path)
    _group(_events(document), "SessionStart")["matcher"] = "resume"
    _write_object(hooks_path, document)

    # When: the modified manifest is fingerprinted.
    after = _states(root, HookPlatform.POSIX)

    # Then: only the matching event's hash changes.
    assert after[0].trusted_hash != before[0].trusted_hash
    assert after[1:] == before[1:]


def test_windows_command_falls_back_only_when_override_is_absent(tmp_path: Path) -> None:
    # Given: SessionStart omits commandWindows while retaining its POSIX command.
    root = _copy_plugin(tmp_path)
    hooks_path = root / "hooks" / "hooks.json"
    document = _read_object(hooks_path)
    _ = _handler(_events(document), "SessionStart").pop("commandWindows")
    _write_object(hooks_path, document)

    # When: Windows and POSIX identities are calculated.
    windows = _states(root, HookPlatform.WINDOWS)
    posix = _states(root, HookPlatform.POSIX)

    # Then: the missing override uses command without expanding ${PLUGIN_ROOT}.
    assert windows[0].trusted_hash == posix[0].trusted_hash


@pytest.mark.parametrize(("first", "second"), [(None, 600), (0, 1)])
def test_timeout_uses_default_and_minimum(
    tmp_path: Path,
    first: int | None,
    second: int,
) -> None:
    # Given: equivalent timeout forms in otherwise identical hook manifests.
    roots = (_copy_plugin(tmp_path / "first"), _copy_plugin(tmp_path / "second"))
    for root, timeout in zip(roots, (first, second), strict=True):
        hooks_path = root / "hooks" / "hooks.json"
        document = _read_object(hooks_path)
        handler = _handler(_events(document), "Stop")
        if timeout is None:
            _ = handler.pop("timeout")
        else:
            handler["timeout"] = timeout
        _write_object(hooks_path, document)

    # When: both canonical identities are calculated.
    hashes = tuple(_states(root, HookPlatform.POSIX)[-1].trusted_hash for root in roots)

    # Then: missing means 600 and zero clamps to one.
    assert hashes[0] == hashes[1]


def test_status_message_is_preserved_in_hash(tmp_path: Path) -> None:
    # Given: one approved handler gains a statusMessage.
    root = _copy_plugin(tmp_path)
    hooks_path = root / "hooks" / "hooks.json"
    before, document = _states(root, HookPlatform.POSIX), _read_object(hooks_path)
    _handler(_events(document), "Stop")["statusMessage"] = "Waiting"
    _write_object(hooks_path, document)

    # When: the updated identity is calculated.
    after = _states(root, HookPlatform.POSIX)

    # Then: the optional status text participates in the canonical hash.
    assert after[-1].trusted_hash != before[-1].trusted_hash


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("alternate", "hook_manifest_path_invalid"),
        ("multiple", "hook_manifest_count_invalid"),
        ("escape", "hook_manifest_path_invalid"),
        ("malformed_plugin", "invalid_json"),
        ("malformed_hooks", "invalid_json"),
        ("missing_event", "hook_handler_set_invalid"),
        ("extra_event", "hook_handler_set_invalid"),
        ("async", "hook_async_invalid"),
        ("non_command", "hook_handler_invalid"),
        ("empty", "hook_command_invalid"),
        ("multiple_groups", "hook_group_invalid"),
        ("multiple_handlers", "hook_handler_set_invalid"),
        ("timeout", "hook_timeout_invalid"),
        ("matcher", "hook_matcher_invalid"),
        ("status", "hook_status_message_invalid"),
        ("missing_file", "source_file_missing"),
    ],
)
def test_rejects_invalid_manifest_without_partial_trust(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    # Given: one malformed, escaping, incomplete, or unsupported hook declaration.
    root = _copy_plugin(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    hooks_path = root / "hooks" / "hooks.json"
    manifest = _read_object(manifest_path)
    document = _read_object(hooks_path)
    events = _events(document)
    match case:
        case "alternate":
            _ = shutil.copy2(hooks_path, root / "hooks" / "alternate.json")
            manifest["hooks"] = "./hooks/alternate.json"
        case "multiple":
            manifest["hooks"] = ["./hooks/hooks.json", "./hooks/other.json"]
        case "escape":
            manifest["hooks"] = "./hooks/../hooks/hooks.json"
        case "malformed_plugin":
            _ = manifest_path.write_text("{", encoding="utf-8")
        case "malformed_hooks":
            _ = hooks_path.write_text("{", encoding="utf-8")
        case "missing_event":
            _ = events.pop("Stop")
        case "extra_event":
            events["PreCompact"] = events["Stop"]
        case "async":
            _handler(events, "Stop")["async"] = True
        case "non_command":
            _handler(events, "Stop")["type"] = "prompt"
        case "empty":
            _handler(events, "Stop")["command"] = " "
        case "multiple_groups":
            groups = events["Stop"]
            assert isinstance(groups, list)
            groups.append(groups[0])
        case "multiple_handlers":
            handlers = _group(events, "Stop")["hooks"]
            assert isinstance(handlers, list)
            handlers.append(handlers[0])
        case "timeout":
            _handler(events, "Stop")["timeout"] = -1
        case "matcher":
            _group(events, "Stop")["matcher"] = 7
        case "status":
            _handler(events, "Stop")["statusMessage"] = 7
        case "missing_file":
            hooks_path.unlink()
        case _:
            assert_never(pytest.fail(f"unknown rejection fixture: {case}"))
    if case not in {"malformed_plugin", "malformed_hooks", "missing_file"}:
        _write_object(manifest_path, manifest)
        _write_object(hooks_path, document)

    # When/Then: calculation raises one typed error and returns no partial state tuple.
    with pytest.raises(InstallPluginError, match=reason):
        _ = _states(root, HookPlatform.WINDOWS)
