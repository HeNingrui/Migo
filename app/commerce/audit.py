"""The audit chain C writes alongside every commerce transaction.

The claim the demo makes is that a third party can check what the agent did
without trusting the operator. That claim only holds if the record is produced
by the same transaction that moved the money: an event written afterwards can be
missing exactly when it matters, and an event written in a separate transaction
can describe a payment that was then rolled back.

So this module has one obligation -- ``append`` takes the caller's connection
and writes inside the caller's transaction -- and three rules:

**Append-only.** No update, no delete, and two database triggers that refuse
both. The chain is the evidence; a writer that can edit it is not evidence of
anything.

**Dense and chained.** ``sequence`` counts from zero with no gaps, and each
record commits to its predecessor's hash, so removing or reordering a record
breaks every record after it. :func:`app.contracts.audit.verify_chain` recomputes
all of it, and :meth:`AuditLog.verify` runs that over C's own rows.

**Nothing secret.** The contract says a payload must never contain a full
payment capability, a signing key, or a payment credential, because the chain is
shown to people. That rule is enforced here by refusing a payload whose keys
name one, rather than by hoping every caller remembers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.commerce.database import new_id
from app.commerce.repositories import AuditRepository
from app.contracts.audit import (
    AuditEvent,
    AuditEventType,
    VerifyReport,
    compute_event_hash,
    verify_chain,
)
from app.contracts.common import ActorType

#: Payload key names that would put a credential into a log meant to be shown to
#: people. Matched case-insensitively as a substring, so ``capability_token`` and
#: ``signing_key_id`` are both caught. The list is deliberately short and blunt:
#: a clever allowlist would be a second specification of the contract's rule.
FORBIDDEN_PAYLOAD_KEYS: tuple[str, ...] = (
    "capability",
    "credential",
    "secret",
    "signing_key",
    "private_key",
    "token",
    "password",
    "card_number",
    "cvv",
)


class AuditLog:
    """C's writer for the append-only chain."""

    def __init__(self, repository: AuditRepository | None = None) -> None:
        self._repository = repository or AuditRepository()

    def append(
        self,
        conn,
        *,
        event_type: AuditEventType,
        actor_type: ActorType,
        actor_id: str,
        occurred_at: datetime,
        payload: dict[str, Any] | None = None,
        mandate_id: str | None = None,
        proposal_id: str | None = None,
        reservation_id: str | None = None,
        transaction_id: str | None = None,
    ) -> AuditEvent:
        """Write one record, inside the caller's transaction.

        The sequence and the previous hash are read in the same transaction as
        the insert. That is what makes the chain dense under concurrency:
        ``BEGIN IMMEDIATE`` has already taken the write lock, so no other writer
        can claim the same sequence between the read and the write.
        """
        body = dict(payload or {})
        self._reject_credentials(body)

        head = self._repository.head(conn)
        sequence = 0 if head is None else head.sequence + 1
        previous_hash = None if head is None else head.event_hash
        event_id = new_id("evt")

        event = AuditEvent(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            mandate_id=mandate_id,
            proposal_id=proposal_id,
            reservation_id=reservation_id,
            transaction_id=transaction_id,
            payload=body,
            previous_hash=previous_hash,
            event_hash=compute_event_hash(
                event_id=event_id,
                sequence=sequence,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=body,
                previous_hash=previous_hash,
                occurred_at=occurred_at,
                mandate_id=mandate_id,
                proposal_id=proposal_id,
                reservation_id=reservation_id,
                transaction_id=transaction_id,
            ),
            occurred_at=occurred_at,
        )
        self._repository.add(conn, event)
        return event

    # -- reading ------------------------------------------------------------

    def verify(self, conn) -> VerifyReport:
        """Recompute the whole chain and locate the first break, if any."""
        return verify_chain(self._repository.all(conn))

    def events_for_proposal(self, conn, proposal_id: str) -> list[AuditEvent]:
        return self._repository.for_proposal(conn, proposal_id)

    def events_for_mandate(self, conn, mandate_id: str) -> list[AuditEvent]:
        return self._repository.for_mandate(conn, mandate_id)

    def all_events(self, conn) -> list[AuditEvent]:
        return self._repository.all(conn)

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _reject_credentials(payload: dict[str, Any]) -> None:
        offenders = sorted(
            key for key in payload
            if any(banned in key.lower() for banned in FORBIDDEN_PAYLOAD_KEYS)
        )
        if offenders:
            raise ValueError(
                "the audit payload must not carry a credential: "
                f"{offenders}. Record the nonce or the id instead -- the chain is "
                "meant to be shown to people."
            )


__all__ = ["FORBIDDEN_PAYLOAD_KEYS", "AuditLog"]
