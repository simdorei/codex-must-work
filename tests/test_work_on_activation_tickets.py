from __future__ import annotations

import hashlib
import json
import threading
from typing import TYPE_CHECKING, cast

import pytest

from scripts.control_capability import derive_control_capability
from scripts.work_on_activation import (
    ActivationIdentity,
    ActivationTicketError,
    ActivationTicketStore,
)
from tests.work_on_activation_test_support import TEST_KEY, ActivationFixture

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


@pytest.fixture
def activation(tmp_path: Path) -> ActivationFixture:
    return ActivationFixture(tmp_path)


def test_ticket_is_bound_and_consumed_exactly_once(
    activation: ActivationFixture,
) -> None:
    store = activation.store()
    _ = store.issue(activation.identity)

    store.consume(activation.identity, activation.capability)

    with pytest.raises(ActivationTicketError, match="work_on_authorization_required"):
        store.consume(activation.identity, activation.capability)


def test_consumed_ticket_tombstone_tamper_cannot_reauthorize_start(
    activation: ActivationFixture,
) -> None:
    # Given: a consumed one-use ticket persisted under its session hash.
    store = activation.store()
    _ = store.issue(activation.identity)
    store.consume(activation.identity, activation.capability)
    ticket = (
        activation.root
        / "plugin-data"
        / "work-on-tickets"
        / f"{hashlib.sha256(b'session-a').hexdigest()}.json"
    )
    payload = cast("dict[str, JsonValue]", json.loads(ticket.read_text(encoding="utf-8")))
    payload["consumed"] = False
    _ = ticket.write_text(json.dumps(payload), encoding="utf-8")

    # When / Then: changing only the consumed bit fails closed.
    with pytest.raises(ActivationTicketError, match="work_on_authorization_state_invalid"):
        store.consume(activation.identity, activation.capability)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("session_id", "session-b"),
        ("turn_id", "turn-b"),
        ("transcript_path", "C:/rollouts/b.jsonl"),
        ("issued_at", 1),
        ("nonce", "attacker-controlled"),
    ],
)
def test_ticket_identity_expiry_or_nonce_tamper_fails_as_invalid_authenticated_state(
    activation: ActivationFixture,
    field: str,
    replacement: str | int,
) -> None:
    # Given: one issued ticket whose authenticated body is changed on disk.
    store = activation.store()
    _ = store.issue(activation.identity)
    ticket = (
        activation.root
        / "plugin-data"
        / "work-on-tickets"
        / f"{hashlib.sha256(b'session-a').hexdigest()}.json"
    )
    payload = cast("dict[str, JsonValue]", json.loads(ticket.read_text(encoding="utf-8")))
    payload[field] = replacement
    _ = ticket.write_text(json.dumps(payload), encoding="utf-8")

    # When / Then: authenticated record tampering is not treated as an ordinary mismatch.
    with pytest.raises(ActivationTicketError, match="work_on_authorization_state_invalid"):
        store.consume(activation.identity, activation.capability)


def test_ticket_persists_record_mac_and_rejects_mac_tamper(
    activation: ActivationFixture,
) -> None:
    # Given: one issued ticket with an authenticated record envelope.
    store = activation.store()
    _ = store.issue(activation.identity)
    ticket = (
        activation.root
        / "plugin-data"
        / "work-on-tickets"
        / f"{hashlib.sha256(b'session-a').hexdigest()}.json"
    )
    payload = cast("dict[str, JsonValue]", json.loads(ticket.read_text(encoding="utf-8")))
    signature = payload["record_mac"]
    assert isinstance(signature, str)
    payload["record_mac"] = ("0" if signature[0] != "0" else "1") + signature[1:]
    _ = ticket.write_text(json.dumps(payload), encoding="utf-8")

    # When / Then: the modified MAC fails closed.
    with pytest.raises(ActivationTicketError, match="work_on_authorization_state_invalid"):
        store.consume(activation.identity, activation.capability)


@pytest.mark.parametrize(
    ("session_id", "turn_id", "transcript_path", "capability"),
    [
        (
            "session-b",
            "turn-a",
            "C:/rollouts/a.jsonl",
            derive_control_capability(TEST_KEY, "session-b"),
        ),
        (
            "session-a",
            "turn-b",
            "C:/rollouts/a.jsonl",
            derive_control_capability(TEST_KEY, "session-a"),
        ),
        (
            "session-a",
            "turn-a",
            "C:/rollouts/b.jsonl",
            derive_control_capability(TEST_KEY, "session-a"),
        ),
        (
            "session-a",
            "turn-a",
            "C:/rollouts/a.jsonl",
            derive_control_capability(b"b" * 32, "session-a"),
        ),
    ],
    ids=("session", "turn", "transcript", "capability"),
)
def test_ticket_mismatch_fails_closed(
    activation: ActivationFixture,
    session_id: str,
    turn_id: str,
    transcript_path: str,
    capability: str,
) -> None:
    store = activation.store()
    _ = store.issue(activation.identity)
    identity = ActivationIdentity(session_id, turn_id, transcript_path)
    expected = (
        "work_on_authorization_required"
        if session_id == "session-b"
        else "work_on_authorization_mismatch"
    )

    with pytest.raises(ActivationTicketError, match=expected):
        store.consume(identity, capability)


def test_expired_ticket_fails_closed(tmp_path: Path) -> None:
    now = [1_000.0]
    store = ActivationTicketStore(
        tmp_path / "plugin-data",
        TEST_KEY,
        clock=lambda: now[0],
    )
    identity = ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl")
    _ = store.issue(identity)
    now[0] += 121.0

    with pytest.raises(ActivationTicketError, match="work_on_authorization_expired"):
        store.consume(identity, derive_control_capability(TEST_KEY, "session-a"))


def test_racing_consumers_allow_exactly_one_start(
    activation: ActivationFixture,
) -> None:
    store = activation.store()
    _ = store.issue(activation.identity)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def consume() -> None:
        _ = barrier.wait()
        try:
            store.consume(activation.identity, activation.capability)
        except ActivationTicketError as error:
            outcomes.append(error.reason_code)
        else:
            outcomes.append("consumed")

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    _ = barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["consumed", "work_on_authorization_required"]


def test_ticket_secret_never_appears_in_public_error(tmp_path: Path) -> None:
    nonce_value = "sensitive-ticket-bytes"
    store = ActivationTicketStore(
        tmp_path / "plugin-data",
        TEST_KEY,
        clock=lambda: 1_000.0,
        nonce_factory=lambda _size: nonce_value,
    )
    _ = store.issue(ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"))

    with pytest.raises(ActivationTicketError) as raised:
        store.consume(
            ActivationIdentity("session-a", "wrong-turn", "C:/rollouts/a.jsonl"),
            derive_control_capability(TEST_KEY, "session-a"),
        )

    assert nonce_value not in str(raised.value)
    assert nonce_value not in json.dumps({"error": raised.value.reason_code})
