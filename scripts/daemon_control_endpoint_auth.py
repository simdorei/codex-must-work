"""Authenticate one endpoint generation before a client sends capabilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Final, Protocol

from scripts.mcp_limits import (
    DuplicateMemberError,
    IntegerLimitError,
    MemberLimitError,
    MemberNameLimitError,
    NonFiniteNumberError,
    object_pairs,
    parse_int,
    reject_non_finite,
    validate_structure,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.daemon_control_endpoint_models import EndpointLocator
    from scripts.mcp_protocol import JsonObject
    from scripts.state_io import JsonValue

_SCHEMA_VERSION: Final = 1
_DOMAIN: Final = b"cmw-endpoint-server-v1\0"
_MIN_CHALLENGE_CHARS: Final = 32
_MAX_CHALLENGE_CHARS: Final = 128
_PROOF_CHARS: Final = 43
_CHALLENGE_ALPHABET: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class _JsonLoader(Protocol):
    def __call__(
        self,
        s: str,
        *,
        object_pairs_hook: Callable[[list[tuple[str, JsonValue]]], JsonObject],
        parse_constant: Callable[[str], JsonValue],
        parse_int: Callable[[str], int],
    ) -> JsonValue: ...


def _stdlib_json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _stdlib_json_loader()


def encode_client_hello(locator: EndpointLocator, challenge: str) -> bytes:
    """Encode a public-only generation challenge."""
    value = {
        "schema_version": _SCHEMA_VERSION,
        "challenge": challenge,
        "pid": locator.pid,
        "process_created_ns": locator.process_created_ns,
        "port": locator.port,
    }
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def parse_client_hello(raw_line: str, locator: EndpointLocator) -> str | None:
    """Parse an exact public generation challenge without accepting extras."""
    try:
        decoded: JsonValue = _LOAD_JSON(
            raw_line,
            object_pairs_hook=object_pairs,
            parse_constant=reject_non_finite,
            parse_int=parse_int,
        )
        validate_structure(decoded)
    except (
        DuplicateMemberError,
        IntegerLimitError,
        MemberLimitError,
        MemberNameLimitError,
        NonFiniteNumberError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None
    if type(decoded) is not dict or set(decoded) != {
        "schema_version",
        "challenge",
        "pid",
        "process_created_ns",
        "port",
    }:
        return None
    challenge = decoded.get("challenge")
    exact_generation = (
        decoded.get("schema_version") == _SCHEMA_VERSION
        and decoded.get("pid") == locator.pid
        and decoded.get("process_created_ns") == locator.process_created_ns
        and decoded.get("port") == locator.port
    )
    if not (
        exact_generation
        and type(challenge) is str
        and _MIN_CHALLENGE_CHARS <= len(challenge) <= _MAX_CHALLENGE_CHARS
        and all(character in _CHALLENGE_ALPHABET for character in challenge)
    ):
        return None
    return challenge


def encode_server_proof(locator: EndpointLocator, challenge: str) -> bytes:
    """Prove possession of the locator secret without transmitting it."""
    value = {
        "schema_version": _SCHEMA_VERSION,
        "server_proof": server_proof(locator, challenge),
    }
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def verify_server_proof(
    raw_line: str,
    locator: EndpointLocator,
    challenge: str,
) -> bool:
    """Verify one exact generation proof in constant time."""
    try:
        decoded: JsonValue = _LOAD_JSON(
            raw_line,
            object_pairs_hook=object_pairs,
            parse_constant=reject_non_finite,
            parse_int=parse_int,
        )
        validate_structure(decoded)
    except (
        DuplicateMemberError,
        IntegerLimitError,
        MemberLimitError,
        MemberNameLimitError,
        NonFiniteNumberError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
    if (
        type(decoded) is not dict
        or set(decoded) != {"schema_version", "server_proof"}
        or decoded.get("schema_version") != _SCHEMA_VERSION
    ):
        return False
    proof = decoded.get("server_proof")
    return (
        type(proof) is str
        and len(proof) == _PROOF_CHARS
        and hmac.compare_digest(proof, server_proof(locator, challenge))
    )


def server_proof(locator: EndpointLocator, challenge: str) -> str:
    """Return the HMAC bound to challenge and exact endpoint generation."""
    message = _DOMAIN + "\0".join(
        (
            challenge,
            str(locator.pid),
            str(locator.process_created_ns),
            str(locator.port),
        )
    ).encode("ascii")
    digest = hmac.new(locator.endpoint_nonce.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
