from __future__ import annotations

import pytest

from scripts.work_on_activation import contains_explicit_work_on


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("$work-on", True),
        ("please $work-on now", True),
        ("($work-on)", True),
        ("$work-onward", False),
        ("prefix$work-on", False),
        ("$work-on_suffix", False),
        ("work-on", False),
        ("please monitor this", False),
    ],
)
def test_exact_work_on_token_is_required(prompt: str, expected: bool) -> None:
    assert contains_explicit_work_on(prompt) is expected


@pytest.mark.parametrize(
    "prompt",
    [
        "한글$work-on",
        "$work-on한글",
        "é$work-on",
        "$work-oné",
        "e\u0301$work-on",
        "$work-on\u0301",
        "四$work-on",
        "$work-on四",
        "\u0661$work-on",
        "$work-on\u0661",
        "\uff3f$work-on",
        "$work-on\uff3f",
        "‿$work-on",
        "$work-on‿",
        "\u200c$work-on",
        "$work-on\u200d",
    ],
    ids=(
        "hangul-before",
        "hangul-after",
        "latin-before",
        "latin-after",
        "combining-before",
        "combining-after",
        "cjk-before",
        "cjk-after",
        "number-before",
        "number-after",
        "fullwidth-connector-before",
        "fullwidth-connector-after",
        "connector-before",
        "connector-after",
        "joiner-before",
        "joiner-after",
    ),
)
def test_unicode_identifier_attachment_is_rejected(prompt: str) -> None:
    assert not contains_explicit_work_on(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "한글 문장: $work-on 시작",
        "English: ($work-on), now.",
        "é: $work-on!",
        "e\u0301: $work-on.",
    ],
    ids=("hangul", "english", "composed-latin", "decomposed-latin"),
)
def test_standalone_token_in_normal_sentences_is_accepted(prompt: str) -> None:
    assert contains_explicit_work_on(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["\uff04work-on", "$work\u2010on", "$work\u2011on", "$\uff57ork-on"],
    ids=("fullwidth-dollar", "hyphen", "nonbreaking-hyphen", "fullwidth-letter"),
)
def test_confusable_token_characters_are_rejected(prompt: str) -> None:
    assert not contains_explicit_work_on(prompt)
