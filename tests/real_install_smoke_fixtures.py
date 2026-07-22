from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from tests import real_install_smoke_ledger as ledger

if TYPE_CHECKING:
    from pathlib import Path

HEAD: Final = "1" * 40
TREE: Final = "2" * 40
_CMW: Final = "codex-must-work@codex-must-work-local"
_LAZY: Final = "lazy-eng-study-codex@lazy-local"
_PREFIX: Final = f"{_CMW}:hooks/hooks.json:"


def _completed(
    event: str,
    source: Path,
    turn: str,
    entries: list[dict[str, str]],
    scope: str = "turn",
) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "hook_completed",
            "turn_id": turn,
            "run": {
                "id": f"{event}-handler",
                "event_name": event,
                "scope": scope,
                "source": "plugin",
                "source_path": str(source),
                "status": "completed",
                "entries": entries,
            },
        },
    }


def rollout_records(tmp_path: Path) -> tuple[Path, list[dict[str, object]], list[str]]:
    rollout = (tmp_path / "rollout.jsonl").resolve()
    cmw = (tmp_path / "cmw" / "hooks" / "hooks.json").resolve()
    lazy = (tmp_path / "lazy" / "hooks" / "hooks.json").resolve()
    session = "019ba8f0-7b5a-7000-8000-000000000001"
    locator = json.dumps(
        {"codex_must_work_locator": {"session_id": session, "transcript_path": str(rollout)}}
    )
    context = (
        "Prompt translation/correction hook is active.\n"
        "Rewrite engine: fixture-engine\n"
        "Start only the first visible assistant message in this turn with this exact line: "
        "번역: hello\n"
        "Do not repeat that exact line in later assistant messages for this turn.\n"
        "Treat the rewritten English prompt as the primary user request.\n"
        "Assistant-understood request: translated body"
    )
    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": session}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-first"}},
        _completed(
            "session_start",
            cmw,
            "turn-session",
            [{"kind": "context", "text": locator}],
            "thread",
        ),
        _completed(
            "user_prompt_submit",
            lazy,
            "turn-first",
            [
                {"kind": "warning", "text": "번역: hello (fixture-engine)"},
                {"kind": "context", "text": context},
            ],
        ),
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": context}],
            },
        },
    ]
    return rollout, records, ["번역: hello\nAnswer body", "SMOKE_OK"]


def config(cache: Path, *, plugins: bool, cmw: bool) -> bytes:
    text = (
        "# keep 한글\r\n[notice]\r\nhide_full_access_warning = false # keep\r\n"
        f"[features]\r\nplugins = {str(plugins).lower()} # editable value only\r\n"
        f'[plugins."{_LAZY}"]\r\nenabled = true\r\n'
        '[hooks.state."lazy-eng-study-codex@lazy-local:hooks/hooks.json:user_prompt_submit:0:0"]\r\n'
        'enabled = true\r\ntrusted_hash = "sha256:lazy"\r\n'
    )
    if not cmw:
        return text.encode()
    text += (
        '\r\n[marketplaces.codex-must-work-local]\r\nsource_type = "local"\r\n'
        f"source = {json.dumps(str(cache))}\r\n"
        f'\r\n[plugins."{_CMW}"]\r\nenabled = true\r\n'
    )
    text += "".join(
        f'\r\n[hooks.state."{_PREFIX}{event}:0:0"]\r\nenabled = true\r\n'
        f'trusted_hash = "sha256:{index:064x}"\r\n'
        for index, event in enumerate(ledger.CMW_EVENTS)
    )
    return text.encode()


def trust() -> tuple[ledger.TrustEntry, ...]:
    return tuple(
        ledger.TrustEntry(f"{_PREFIX}{event}:0:0", f"sha256:{index:064x}")
        for index, event in enumerate(ledger.CMW_EVENTS)
    )
