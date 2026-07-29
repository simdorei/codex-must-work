from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.config_publication import ConfigSnapshot
from scripts.marketplace_identity import MARKETPLACE_REF, MARKETPLACE_SOURCE, PLUGIN_ID
from scripts.toml_lexical_headers import TomlLexicalError, scan_table_headers
from scripts.uninstall_config import render_config_removal
from scripts.uninstall_evidence import ValidatedInstallEvidence

_CANONICAL_EVIDENCE = (
    ValidatedInstallEvidence(
        "simdorei",
        PLUGIN_ID,
        "git",
        MARKETPLACE_SOURCE,
        MARKETPLACE_REF,
        (),
    ),
)


def _snapshot(data: bytes) -> ConfigSnapshot:
    return ConfigSnapshot(data, None, None, Path("config.toml"), (1, 1))


@pytest.mark.parametrize(
    "unrelated",
    [
        (
            b'title = """opening \\" quoted\n'
            b"[marketplaces.simdorei]\n"
            b"[still part of the basic multiline string]\n"
            b'"""\n'
        ),
        (
            b"literal = '''\n"
            b'[plugins."codex-must-work@simdorei"]\n'
            b"[still part of the literal multiline string]\n"
            b"'''\n"
        ),
    ],
    ids=("basic-multiline-with-escaped-quote", "literal-multiline"),
)
def test_removal_preserves_header_lookalikes_inside_multiline_strings(
    unrelated: bytes,
) -> None:
    owned = (
        b"[marketplaces.simdorei]\n"
        b'source_type = "git"\n'
        b'source = "https://github.com/simdorei/codex-must-work.git"\n'
        b'ref = "main"\n'
    )
    raw = unrelated + owned

    rendered = render_config_removal(_snapshot(raw), _CANONICAL_EVIDENCE)

    assert rendered == unrelated
    assert tomllib.loads(rendered.decode("utf-8"))


def test_removal_preserves_comments_and_inline_table_lookalikes_exactly() -> None:
    unrelated = (
        b"# [marketplaces.simdorei]\n"
        b'quoted = "[plugins.\\"codex-must-work@simdorei\\"]"\n'
        b'inline = { text = "[marketplaces.simdorei]", enabled = true }\n'
    )
    owned = (
        b"[marketplaces.simdorei]\n"
        b'source_type = "git"\n'
        b'source = "https://github.com/simdorei/codex-must-work.git"\n'
        b'ref = "main"\n'
    )

    rendered = render_config_removal(
        _snapshot(unrelated + owned),
        _CANONICAL_EVIDENCE,
    )

    assert rendered == unrelated
    assert tomllib.loads(rendered.decode("utf-8"))


@pytest.mark.parametrize("source", ['value = """unterminated', 'value = "trailing\\'])
def test_header_scanner_rejects_unterminated_or_ambiguous_strings(source: str) -> None:
    with pytest.raises(TomlLexicalError):
        _ = scan_table_headers(source)
