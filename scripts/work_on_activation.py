"""Issue and consume private one-time authorizations for explicit work-on prompts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Final, Never, Protocol, final, override

if TYPE_CHECKING:
    from pathlib import Path

from scripts.control_capability import derive_control_capability
from scripts.private_root import ensure_private_root
from scripts.state_io import (
    ExclusiveWriteLock,
    StateError,
    ensure_direct_regular_file,
    prepare_parent_directories,
    safe_absolute_path,
)
from scripts.work_on_activation_record import (
    ActivationTicketRecord,
    read_activation_record,
    write_activation_record,
)
from scripts.work_on_identity import (
    ActivationIdentity,
    activation_identity_is_valid,
    same_activation_identity,
)
from scripts.work_on_token import contains_explicit_work_on

__all__ = ["ActivationIdentity", "contains_explicit_work_on"]

_TICKET_DIRECTORY: Final = "work-on-tickets"
_TTL_SECONDS: Final = 120
_CAPABILITY_DOMAIN: Final = b"cmw-work-on-capability-v1\0"
_REQUIRED: Final = "work_on_authorization_required"
_EXPIRED: Final = "work_on_authorization_expired"
_MISMATCH: Final = "work_on_authorization_mismatch"
_INVALID: Final = "work_on_authorization_state_invalid"


class Clock(Protocol):
    """Return wall-clock seconds for ticket expiry."""

    def __call__(self) -> float:
        """Return current epoch seconds."""
        ...


class NonceFactory(Protocol):
    """Create private random ticket material."""

    def __call__(self, byte_count: int, /) -> str:
        """Return an unpredictable URL-safe value."""
        ...


class ActivationAuthorizer(Protocol):
    """Consume one activation before a monitor can start."""

    def consume(self, identity: ActivationIdentity, capability: str) -> None:
        """Consume a matching live authorization."""
        ...


@final
class ActivationTicketError(StateError):
    """Expose one stable public reason without ticket contents or paths."""

    def __init__(self, reason_code: str) -> None:
        """Retain only a public-safe reason code."""
        super().__init__(reason_code)
        self.reason_code = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code


@final
class ActivationTicketStore:
    """Persist and atomically consume per-session activation tickets."""

    def __init__(
        self,
        plugin_data: Path,
        control_key: bytes,
        *,
        clock: Clock = time.time,
        nonce_factory: NonceFactory = secrets.token_urlsafe,
    ) -> None:
        """Bind ticket persistence to one private root and control key."""
        self._root = plugin_data
        self._key = control_key
        self._clock = clock
        self._nonce_factory = nonce_factory

    def issue(self, identity: ActivationIdentity) -> bool:
        """Issue once, preserving a bounded replay fence for the consumed turn."""
        if not activation_identity_is_valid(identity):
            raise ActivationTicketError(_MISMATCH)
        path = self._path(identity.session_id)
        try:
            ensure_private_root(self._root)
            prepare_parent_directories(self._root, path)
            with ExclusiveWriteLock(path):
                ensure_direct_regular_file(self._root, path)
                try:
                    prior, _, _ = read_activation_record(path, self._key)
                except FileNotFoundError:
                    prior = None
                if prior is not None and same_activation_identity(prior.identity, identity):
                    return not prior.consumed and not self._expired(prior)
                capability = derive_control_capability(self._key, identity.session_id)
                self._write_ticket(
                    path,
                    ActivationTicketRecord(
                        identity,
                        _capability_binding(self._key, capability),
                        int(self._clock()),
                        self._nonce_factory(32),
                        consumed=False,
                    ),
                )
                return True
        except ActivationTicketError:
            raise
        except (OSError, StateError, json.JSONDecodeError, UnicodeError):
            raise ActivationTicketError(_INVALID) from None

    def consume(self, identity: ActivationIdentity, capability: str) -> None:
        """Consume a matching live ticket exactly once."""
        if not activation_identity_is_valid(identity):
            raise ActivationTicketError(_MISMATCH)
        path = self._path(identity.session_id)
        try:
            ensure_private_root(self._root)
            _ = safe_absolute_path(self._root, path)
            if not path.parent.is_dir():
                _fail(_REQUIRED)
            with ExclusiveWriteLock(path):
                ticket, _, _ = read_activation_record(path, self._key)
                if ticket.consumed:
                    _fail(_REQUIRED)
                reason = self._rejection_reason(ticket, identity, capability)
                if reason is not None:
                    _fail(reason)
                self._write_ticket(
                    path,
                    ActivationTicketRecord(
                        ticket.identity,
                        ticket.capability_binding,
                        ticket.issued_at,
                        ticket.nonce,
                        consumed=True,
                    ),
                )
        except ActivationTicketError:
            raise
        except FileNotFoundError:
            raise ActivationTicketError(_REQUIRED) from None
        except (OSError, StateError, json.JSONDecodeError, UnicodeError):
            raise ActivationTicketError(_INVALID) from None

    def _path(self, session_id: str) -> Path:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._root / _TICKET_DIRECTORY / f"{name}.json"

    def _write_ticket(self, path: Path, ticket: ActivationTicketRecord) -> None:
        write_activation_record(path, self._key, ticket)

    def _expired(self, ticket: ActivationTicketRecord) -> bool:
        now = int(self._clock())
        return ticket.issued_at > now or now - ticket.issued_at > _TTL_SECONDS

    def _rejection_reason(
        self,
        ticket: ActivationTicketRecord,
        identity: ActivationIdentity,
        capability: str,
    ) -> str | None:
        if self._expired(ticket):
            return _EXPIRED
        expected_binding = _capability_binding(self._key, capability)
        matches = (
            hmac.compare_digest(ticket.identity.session_id, identity.session_id)
            and hmac.compare_digest(ticket.identity.turn_id, identity.turn_id)
            and hmac.compare_digest(ticket.identity.transcript_path, identity.transcript_path)
            and hmac.compare_digest(ticket.capability_binding, expected_binding)
        )
        return None if matches else _MISMATCH


def _capability_binding(key: bytes, capability: str) -> str:
    return hmac.new(
        key,
        _CAPABILITY_DOMAIN + capability.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _fail(reason_code: str) -> Never:
    raise ActivationTicketError(reason_code)
