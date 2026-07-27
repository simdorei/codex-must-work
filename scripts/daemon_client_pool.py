"""Own the daemon's single fingerprint-bound app-server client."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, final

from scripts.app_server_protocol import AppServerActivity, ManagedAppServer
from scripts.daemon_client_close import CloseGate
from scripts.daemon_models import DaemonServiceError
from scripts.daemon_reservation import KeyedReservations

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

_CLIENT_RESERVATION: Final = "shared-client"


class ClosableAppServer(ManagedAppServer, Protocol):
    """Describe the shared client resource owned by the registry."""

    def close(self) -> None:
        """Close the owned app-server child."""
        ...


type ClientFactory = Callable[[str, Callable[[AppServerActivity], None]], ClosableAppServer]


@final
class ClientReference:
    """Identify one borrow across its pool-owned state transitions."""

    __slots__ = ()


class _PoolReferenceState(StrEnum):
    BORROWED = "borrowed"
    INSTALLED = "installed"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class _ReferenceRecord:
    client: ClosableAppServer
    state: _PoolReferenceState
    close_gate: CloseGate


@dataclass(frozen=True, slots=True)
class _PooledClient:
    client: ClosableAppServer
    fingerprint: str


@final
class ClientBorrow:
    """Retain one unpublished client reference until commit or rollback."""

    def __init__(
        self,
        pool: SharedClientPool,
        client: ClosableAppServer,
        reference: ClientReference,
    ) -> None:
        """Bind one unfinished reference to its pool."""
        self._pool = pool
        self.client = client
        self._reference = reference

    def commit(self) -> None:
        """Convert this borrow to an installed reference exactly once."""
        self._pool.commit(self._reference)

    def release(self) -> None:
        """Release this borrow and close a newly idle client."""
        self._pool.release(self._reference)

    def rollback(self) -> None:
        """Release either the borrowed or installed form exactly once."""
        self.release()


@final
class SharedClientPool:
    """Create one shared client and retain every unpublished borrower."""

    def __init__(
        self,
        factory: ClientFactory,
        activity_listener: Callable[[AppServerActivity], None],
        lock: threading.RLock,
    ) -> None:
        """Bind factory I/O and reference state to shared synchronization."""
        self._factory = factory
        self._activity_listener = activity_listener
        self._lock = lock
        self._reservations = KeyedReservations(lock)
        self._pooled: dict[str, _PooledClient] = {}
        self._references: dict[ClientReference, _ReferenceRecord] = {}

    def borrow(self, fingerprint: str) -> ClientBorrow:
        """Retain a client for one task that has not been published."""
        reference = ClientReference()
        created_client: ClosableAppServer | None = None
        try:
            with self._reservations.claim(_CLIENT_RESERVATION):
                with self._lock:
                    pooled = self._pooled.get(_CLIENT_RESERVATION)
                    if pooled is not None:
                        self._require_fingerprint(pooled, fingerprint)
                        self._references[reference] = _ReferenceRecord(
                            client=pooled.client,
                            state=_PoolReferenceState.BORROWED,
                            close_gate=CloseGate(),
                        )
                        return ClientBorrow(self, pooled.client, reference)
                created_client = self._factory(fingerprint, self._activity_listener)
                with self._lock:
                    self._pooled[_CLIENT_RESERVATION] = _PooledClient(
                        created_client,
                        fingerprint,
                    )
                    self._references[reference] = _ReferenceRecord(
                        client=created_client,
                        state=_PoolReferenceState.BORROWED,
                        close_gate=CloseGate(),
                    )
                    return ClientBorrow(self, created_client, reference)
        except (OSError, KeyboardInterrupt, SystemExit):
            with self._lock:
                _ = self._references.pop(reference, None)
                pooled = self._pooled.pop(_CLIENT_RESERVATION, None)
            if pooled is not None:
                pooled.client.close()
            elif created_client is not None:
                created_client.close()
            raise

    def commit(self, reference: ClientReference) -> None:
        """Promote one borrowed token under the shared lock."""
        with self._lock:
            record = self._references.get(reference)
            if record is None:
                reason = "client_borrow_already_released"
                raise RuntimeError(reason)
            if record.state is _PoolReferenceState.INSTALLED:
                reason = "client_borrow_already_committed"
                raise RuntimeError(reason)
            self._references[reference] = _ReferenceRecord(
                client=record.client,
                state=_PoolReferenceState.INSTALLED,
                close_gate=record.close_gate,
            )

    def release(self, reference: ClientReference) -> None:
        """Release one token and finish any resulting close obligation."""
        with self._lock:
            record = self._references.get(reference)
            if record is None:
                return
            obligation = (
                record
                if record.state is _PoolReferenceState.CLOSING
                else self._release_active(reference)
            )
        if obligation is not None:
            self._finish_close(reference, obligation)

    def release_installed(self, count: int) -> None:
        """Release installed references and drain every pending close."""
        with self._lock:
            installed = tuple(
                reference
                for reference, record in self._references.items()
                if record.state is _PoolReferenceState.INSTALLED
            )
            if count < 0 or count > len(installed):
                reason = "client_install_underflow"
                raise RuntimeError(reason)
            selected = installed[:count]
            remaining_active = any(
                reference not in selected and record.state is not _PoolReferenceState.CLOSING
                for reference, record in self._references.items()
            )
            if selected and not remaining_active:
                reference = selected[-1]
                record = self._references[reference]
                for released in selected[:-1]:
                    del self._references[released]
                pooled = self._pooled.get(_CLIENT_RESERVATION)
                if pooled is not None:
                    self._references[reference] = _ReferenceRecord(
                        client=pooled.client,
                        state=_PoolReferenceState.CLOSING,
                        close_gate=record.close_gate,
                    )
                    _ = self._pooled.pop(_CLIENT_RESERVATION, None)
                else:
                    del self._references[reference]
            else:
                for reference in selected:
                    del self._references[reference]
            obligations = tuple(
                (reference, record)
                for reference, record in self._references.items()
                if record.state is _PoolReferenceState.CLOSING
            )
        self._finish_obligations(obligations)

    def reference_counts(self) -> tuple[int, int, int]:
        """Return borrowed, installed, and pending-close counts."""
        with self._lock:
            states = tuple(record.state for record in self._references.values())
            return (
                states.count(_PoolReferenceState.BORROWED),
                states.count(_PoolReferenceState.INSTALLED),
                states.count(_PoolReferenceState.CLOSING),
            )

    def get_existing(self) -> ClosableAppServer | None:
        """Return the current client without acquiring a reference."""
        with self._lock:
            pooled = self._pooled.get(_CLIENT_RESERVATION)
            return None if pooled is None else pooled.client

    def _release_active(self, reference: ClientReference) -> _ReferenceRecord | None:
        record = self._references[reference]
        if any(
            other_reference is not reference and other.state is not _PoolReferenceState.CLOSING
            for other_reference, other in self._references.items()
        ):
            del self._references[reference]
            return None
        pooled = self._pooled.get(_CLIENT_RESERVATION)
        if pooled is None:
            del self._references[reference]
            return None
        obligation = _ReferenceRecord(
            client=pooled.client,
            state=_PoolReferenceState.CLOSING,
            close_gate=record.close_gate,
        )
        self._references[reference] = obligation
        _ = self._pooled.pop(_CLIENT_RESERVATION, None)
        return obligation

    def _finish_obligations(
        self,
        obligations: tuple[tuple[ClientReference, _ReferenceRecord], ...],
    ) -> None:
        first_error: BaseException | None = None
        for reference, obligation in obligations:
            try:
                self._finish_close(reference, obligation)
            except BaseException as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                first_error = error if first_error is None else first_error
        if first_error is not None:
            raise first_error

    def _finish_close(self, reference: ClientReference, obligation: _ReferenceRecord) -> None:
        gate = obligation.close_gate
        with self._lock:
            if self._references.get(reference) is not obligation:
                return
            pooled = self._pooled.get(_CLIENT_RESERVATION)
            if pooled is not None and pooled.client is obligation.client:
                _ = self._pooled.pop(_CLIENT_RESERVATION, None)
        if not gate.finish(obligation.client):
            return
        with self._lock:
            if self._references.get(reference) is obligation:
                del self._references[reference]

    @staticmethod
    def _require_fingerprint(pooled: _PooledClient, fingerprint: str) -> None:
        if pooled.fingerprint != fingerprint:
            reason = "codex_executable_changed"
            raise DaemonServiceError(reason)
