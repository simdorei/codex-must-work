import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Protocol
from unittest.mock import patch

from scripts.calibration import CalibrationRecommendation, CalibrationStatus
from scripts.durations import Milliseconds
from scripts.hook_event import process_hook
from scripts.hook_payload import SessionLocator, serialize_locator
from scripts.state import JsonValue, StateDocument, load_state, save_state
from tests.hook_fixture import enabled_runtime, hook_event


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


def _locator_context(locator: SessionLocator) -> dict[str, JsonValue]:
    envelope = _LOAD_JSON(serialize_locator(locator))
    assert isinstance(envelope, dict)
    specific = envelope.get("hookSpecificOutput")
    assert isinstance(specific, dict)
    raw_context = specific.get("additionalContext")
    assert isinstance(raw_context, str)
    context = _LOAD_JSON(raw_context)
    assert isinstance(context, dict)
    return context


def test_process_hook_when_session_starts_emits_locator_with_calibration() -> None:
    with TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        root = temporary / "codex-must-work"
        transcript = temporary / "rollout.jsonl"
        plugin_root = temporary / "plugin"
        plugin_data = temporary / "plugin-data"
        manifest = plugin_root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        _ = manifest.write_text('{"version":"1.2.3"}', encoding="utf-8")
        raw = hook_event(
            "SessionStart",
            transcript_path=str(transcript),
            prompt="PRIVATE-PROMPT",
        )

        with patch(
            "scripts.hook_event._scan_history",
            return_value=CalibrationRecommendation(
                42,
                Milliseconds(180_000),
                Milliseconds(480_000),
            ),
        ):
            output = process_hook(
                raw,
                root=root,
                plugin_root=plugin_root,
                plugin_data=plugin_data,
            )

        assert isinstance(output, SessionLocator)
        assert output.session_id == "session-1"
        assert output.transcript_path == str(transcript.resolve())
        assert output.plugin_root == str(plugin_root.resolve())
        assert output.plugin_data == str(plugin_data.resolve())
        assert output.calibration.status is CalibrationStatus.PENDING
        assert output.calibration.sample_count == 42
        assert root.joinpath("calibration.json").is_file()

        serialized = serialize_locator(output)
        context = _locator_context(output)
        calibration = context["codex_must_work_calibration"]
        assert isinstance(calibration, dict)
        assert calibration["action"] == "ask_apply"
        assert calibration["required_skill"] == "work-calibration"
        assert calibration["warning_after_ms"] == 180_000
        assert calibration["restart_after_ms"] == 480_000
        assert "PRIVATE-PROMPT" not in serialized


def test_process_hook_when_session_is_opted_out_creates_zero_artifacts() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        raw = hook_event(
            "SubagentStart",
            agent_id="child-1",
            prompt="PRIVATE-PROMPT",
            tool_input="PRIVATE-INPUT",
            tool_output="PRIVATE-OUTPUT",
        )

        with patch("scripts.hook_event._launch_watcher") as launch:
            _ = process_hook(raw, root=root)

        assert not root.exists()
        launch.assert_not_called()


def test_process_hook_asks_for_calibration_only_in_first_thread(tmp_path: Path) -> None:
    root = tmp_path / "codex-must-work"
    plugin_root = tmp_path / "plugin"
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    _ = manifest.write_text('{"version":"1.2.3"}', encoding="utf-8")
    raw = hook_event("SessionStart", transcript_path=str(tmp_path / "rollout.jsonl"))

    with patch(
        "scripts.hook_event._scan_history",
        return_value=CalibrationRecommendation(
            20,
            Milliseconds(60_000),
            Milliseconds(120_000),
        ),
    ):
        first = process_hook(
            raw,
            root=root,
            plugin_root=plugin_root,
            plugin_data=tmp_path / "data",
        )
        second = process_hook(
            raw,
            root=root,
            plugin_root=plugin_root,
            plugin_data=tmp_path / "data",
        )

    assert isinstance(first, SessionLocator)
    assert isinstance(second, SessionLocator)
    first_calibration = _locator_context(first)["codex_must_work_calibration"]
    second_calibration = _locator_context(second)["codex_must_work_calibration"]
    assert isinstance(first_calibration, dict)
    assert isinstance(second_calibration, dict)
    assert first_calibration["action"] == "ask_apply"
    assert second_calibration["action"] == "awaiting_answer"


def test_process_hook_when_enabled_verifies_private_root() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"
        _ = enabled_runtime(root)

        with patch("scripts.hook_event.ensure_private_root") as secure:
            _ = process_hook(hook_event("SubagentStop", agent_id="missing-child"), root=root)

        secure.assert_called_once_with(root)


def test_process_hook_rejects_mismatched_transcript_before_state_mutation() -> None:
    with TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        root = temporary / "codex-must-work"
        path = enabled_runtime(root)
        values = dict(load_state(root, path).values)
        values["transcript_path"] = "sessions/expected.jsonl"
        values["revision"] = 4
        save_state(root, path, StateDocument(values=values))
        unexpected = temporary / "sessions" / "unexpected.jsonl"
        unexpected.parent.mkdir(parents=True)
        unexpected.touch()
        before = load_state(root, path).values

        with patch("scripts.hook_event._launch_watcher") as launch:
            result = process_hook(
                hook_event("UserPromptSubmit", transcript_path=str(unexpected)),
                root=root,
            )

        assert result is None
        assert load_state(root, path).values == before
        launch.assert_not_called()
