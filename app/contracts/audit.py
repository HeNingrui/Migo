"""Append-only, hash-chained audit events.

The audit log is the evidence. Everything else in the system can be re-derived
or argued about, but this is the record a third party is asked to trust, so it
is built to be checked rather than believed.

Three properties make that possible:

**Append-only.** Events are never updated or deleted. A correction is a new
event that refers to the earlier one.

**Hash-chained.** Each record commits to its predecessor's hash, so removing or
reordering a record breaks every record after it. A single altered payload
changes its own hash and every hash that follows.

**Self-describing.** The genesis record carries ``previous_hash = None``, so a
verifier needs no out-of-band initial value.

What this is *not*: it is not a blockchain, not a consensus system, and not
proof of authorship. A single writer can rewrite the whole chain. What it does
prove is that a chain presented as complete is internally consistent, which is
what catches the interesting failure -- a record quietly removed or edited
after the fact.

Payloads must never contain a full payment capability, a signing key, or a
payment credential. The chain is meant to be shown to people.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .common import ActorType

HASH_PREFIX = "sha256:"

#: The only permitted value for the first record's ``previous_hash``.
GENESIS_PREVIOUS_HASH: None = None


class AuditEventType(str, Enum):
    """Every recorded occurrence.

    The list is deliberately explicit rather than free text: a verifier and a UI
    both need to switch on it, and free text guarantees they will disagree.
    """

    # -- consent and authorisation -----------------------------------------
    MANDATE_DRAFTED = "MANDATE_DRAFTED"
    MANDATE_ACTIVATED = "MANDATE_ACTIVATED"
    MANDATE_AMENDED = "MANDATE_AMENDED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    CONSENT_RECORDED = "CONSENT_RECORDED"

    # -- pricing ------------------------------------------------------------
    #: Added for C's quote service. The original list covered mandates, proposals
    #: and money movement but had no value for a priced offer, so a quote could
    #: only be recorded as untyped free text -- which the enum's own docstring
    #: rules out. Additive: existing values are unchanged.
    QUOTE_ISSUED = "QUOTE_ISSUED"

    # -- proposals and decisions -------------------------------------------
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    DECISION_APPROVED = "DECISION_APPROVED"
    DECISION_DENIED = "DECISION_DENIED"
    DECISION_ESCALATED = "DECISION_ESCALATED"

    # -- reservations -------------------------------------------------------
    RESERVATION_CREATED = "RESERVATION_CREATED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"

    # -- escalation ---------------------------------------------------------
    ESCALATION_RAISED = "ESCALATION_RAISED"
    ESCALATION_APPROVED = "ESCALATION_APPROVED"
    ESCALATION_REJECTED = "ESCALATION_REJECTED"
    ESCALATION_TIMED_OUT = "ESCALATION_TIMED_OUT"

    # -- capability ---------------------------------------------------------
    CAPABILITY_ISSUED = "CAPABILITY_ISSUED"
    CAPABILITY_CONSUMED = "CAPABILITY_CONSUMED"
    CAPABILITY_REJECTED = "CAPABILITY_REJECTED"

    # -- payment ------------------------------------------------------------
    PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
    PAYMENT_SETTLED = "PAYMENT_SETTLED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"
    PAYMENT_RECONCILED = "PAYMENT_RECONCILED"

    # -- the honest gap -----------------------------------------------------
    #: A revocation that arrived too late to stop a payment already in flight.
    #: Recorded rather than hidden; see docs on revocation semantics.
    REVOCATION_MISSED = "REVOCATION_MISSED"


class AuditEvent(BaseModel):
    """One recorded occurrence.

    ``sequence`` is a dense, gap-free ordering. A missing number is itself
    evidence, which is why it is recorded rather than implied by insertion
    order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    sequence: StrictInt = Field(ge=0)

    event_type: AuditEventType
    actor_type: ActorType
    actor_id: str = Field(min_length=1)

    mandate_id: str | None = None
    proposal_id: str | None = None
    reservation_id: str | None = None
    transaction_id: str | None = None

    #: Structured detail. Must not contain a full capability, a signing key or a
    #: payment credential.
    payload: dict[str, Any] = Field(default_factory=dict)

    previous_hash: str | None = None
    event_hash: str = Field(min_length=1)

    occurred_at: datetime

    def commit_payload(self) -> dict[str, Any]:
        """The exact object the event hash commits to.

        Excludes ``event_hash`` itself (it cannot contain its own digest) and
        fixes the serialisation, so the same record always hashes the same way
        regardless of key order or platform.
        """
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type.value,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "mandate_id": self.mandate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
            "previous_hash": self.previous_hash,
            "proposal_id": self.proposal_id,
            "reservation_id": self.reservation_id,
            "sequence": self.sequence,
            "transaction_id": self.transaction_id,
        }

    def compute_hash(self) -> str:
        encoded = json.dumps(
            self.commit_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return HASH_PREFIX + hashlib.sha256(encoded).hexdigest()

    def hash_matches(self) -> bool:
        return self.compute_hash() == self.event_hash

    @property
    def is_genesis(self) -> bool:
        return self.sequence == 0

    @model_validator(mode="after")
    def _genesis_rules(self) -> "AuditEvent":
        if self.is_genesis and self.previous_hash is not None:
            raise ValueError("the first record must have previous_hash = null")
        if not self.is_genesis and not self.previous_hash:
            raise ValueError("every record after the first must carry previous_hash")
        return self


class ChainBreak(BaseModel):
    """One reason a chain failed verification, located precisely enough to act on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: StrictInt = Field(ge=0)
    event_id: str | None = None
    problem: Literal[
        "sequence_gap",
        "sequence_out_of_order",
        "hash_mismatch",
        "previous_hash_mismatch",
        "genesis_missing",
        "payload_edited",
    ]
    detail: str


class VerifyReport(BaseModel):
    """The result of checking a chain end to end.

    ``ok`` is true only when every record is present, in order, and hashes to
    what it claims. ``breaks`` locates each failure so a verifier can point at
    the first one rather than saying "invalid".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    checked: StrictInt = Field(ge=0)
    first_sequence: StrictInt | None = None
    last_sequence: StrictInt | None = None
    head_hash: str | None = None
    breaks: list[ChainBreak] = Field(default_factory=list)

    @property
    def is_empty_chain(self) -> bool:
        return self.checked == 0


def compute_event_hash(
    *,
    event_id: str,
    sequence: int,
    event_type: AuditEventType,
    actor_type: ActorType,
    actor_id: str,
    payload: dict[str, Any],
    previous_hash: str | None,
    occurred_at: datetime,
    mandate_id: str | None = None,
    proposal_id: str | None = None,
    reservation_id: str | None = None,
    transaction_id: str | None = None,
) -> str:
    """Hash an event that has not been constructed yet.

    A thin wrapper over :meth:`AuditEvent.compute_hash` so the writer and the
    verifier cannot drift: both go through the model's own serialisation. The
    ``event_hash`` passed in is a placeholder and is never part of the digest.
    """
    probe = AuditEvent.model_construct(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        mandate_id=mandate_id,
        proposal_id=proposal_id,
        reservation_id=reservation_id,
        transaction_id=transaction_id,
        payload=payload,
        previous_hash=previous_hash,
        event_hash=HASH_PREFIX + "0" * 64,
        occurred_at=occurred_at,
    )
    return probe.compute_hash()


def verify_chain(events: list[AuditEvent]) -> VerifyReport:
    """Check a chain presented as complete.

    Pure function: no I/O, no clock. Ordering is by ``sequence``, and a gap or a
    duplicate is reported rather than tolerated.
    """
    if not events:
        return VerifyReport(ok=True, checked=0)

    ordered = sorted(events, key=lambda e: e.sequence)
    breaks: list[ChainBreak] = []

    if ordered[0].sequence != 0:
        breaks.append(ChainBreak(
            sequence=ordered[0].sequence,
            event_id=ordered[0].event_id,
            problem="genesis_missing",
            detail="the chain does not start at sequence 0",
        ))

    for index, event in enumerate(ordered):
        if index > 0 and event.sequence != ordered[index - 1].sequence + 1:
            breaks.append(ChainBreak(
                sequence=event.sequence,
                event_id=event.event_id,
                problem="sequence_gap",
                detail=(
                    f"expected sequence {ordered[index - 1].sequence + 1}, "
                    f"found {event.sequence}"
                ),
            ))

        if not event.hash_matches():
            breaks.append(ChainBreak(
                sequence=event.sequence,
                event_id=event.event_id,
                problem="hash_mismatch",
                detail="the recorded hash does not match the record's contents",
            ))

        if index > 0:
            expected_previous = ordered[index - 1].event_hash
            if event.previous_hash != expected_previous:
                breaks.append(ChainBreak(
                    sequence=event.sequence,
                    event_id=event.event_id,
                    problem="previous_hash_mismatch",
                    detail="this record does not link to the record before it",
                ))

    deduped: list[AuditEvent] = []
    seen: set[int] = set()
    for event in ordered:
        if event.sequence in seen:
            breaks.append(ChainBreak(
                sequence=event.sequence,
                event_id=event.event_id,
                problem="sequence_out_of_order",
                detail=f"sequence {event.sequence} appears more than once",
            ))
            continue
        seen.add(event.sequence)
        deduped.append(event)

    return VerifyReport(
        ok=not breaks,
        checked=len(deduped) or len(ordered),
        first_sequence=ordered[0].sequence,
        last_sequence=ordered[-1].sequence,
        head_hash=ordered[-1].event_hash,
        breaks=breaks,
    )


__all__ = [
    "AuditEvent",
    "AuditEventType",
    "ChainBreak",
    "GENESIS_PREVIOUS_HASH",
    "HASH_PREFIX",
    "VerifyReport",
    "compute_event_hash",
    "verify_chain",
]
