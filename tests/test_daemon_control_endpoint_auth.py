from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.daemon_control_endpoint_auth import (
    encode_client_hello,
    encode_server_proof,
    parse_client_hello,
    verify_server_proof,
)
from scripts.daemon_control_endpoint_models import EndpointLocator

_CHALLENGE = "a" * 43


def _locator() -> EndpointLocator:
    return EndpointLocator(101, 202, 303, "private-endpoint-secret")


def test_client_hello_contains_no_endpoint_secret() -> None:
    # Given
    locator = _locator()

    # When
    encoded = encode_client_hello(locator, _CHALLENGE)

    # Then
    assert locator.endpoint_nonce.encode() not in encoded


@pytest.mark.parametrize("challenge", ["é" * 32, "/" * 32, "a" * 31, "a" * 129])
def test_server_rejects_non_url_safe_or_unbounded_challenge(challenge: str) -> None:
    # Given
    locator = _locator()
    encoded = encode_client_hello(locator, challenge).decode("utf-8")

    # When / Then
    assert parse_client_hello(encoded, locator) is None


@pytest.mark.parametrize(
    ("locator", "challenge"),
    [
        (replace(_locator(), pid=102), _CHALLENGE),
        (replace(_locator(), process_created_ns=203), _CHALLENGE),
        (replace(_locator(), port=304), _CHALLENGE),
        (_locator(), "b" * 43),
    ],
)
def test_server_proof_rejects_replay_for_another_generation_or_challenge(
    locator: EndpointLocator,
    challenge: str,
) -> None:
    # Given
    original = _locator()
    proof = encode_server_proof(original, _CHALLENGE).decode("utf-8")

    # When / Then
    assert not verify_server_proof(proof, locator, challenge)
