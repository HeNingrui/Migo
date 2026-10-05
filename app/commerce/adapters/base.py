"""The payment rail boundary: the one place C talks to something outside itself.

Everything else in ``app/commerce`` is deterministic and local. This is the
seam where a real card network, a real FPS transfer or a real wallet would be
called, so the shape matters more than the current implementation does.

Three rules the boundary enforces, each because the alternative is a system that
cannot be reasoned about:

**The result is a fact, not a boolean.** ``SETTLED`` / ``FAILED`` / ``UNKNOWN``
are three states because the real world has three. A timeout is not a failure:
the money may well have moved, and DC12's rule that an unknown payment is never
automatically retried depends on being able to say so. Collapsing ``UNKNOWN``
into ``FAILED`` is how a system charges twice.

**A failure carries a code, a success does not.** ``PaymentResult`` refuses to be
constructed otherwise, so "it failed" always arrives with something a user can
be told.

**Nothing here decides whether a payment is allowed.** The adapter is handed a
capability and executes it. Authorisation happened in the authority service,
against the mandate, before this module was reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.contracts.common import ErrorCode, PaymentRail, SourceType

#: The three outcomes a rail can report. See the module docstring for why
#: ``UNKNOWN`` is not a flavour of ``FAILED``.
PaymentStatus = Literal["SETTLED", "FAILED", "UNKNOWN"]


@dataclass(frozen=True)
class PaymentRequest:
    """One settlement attempt, as the rail sees it.

    ``capability_token`` is the authority: C signed it, it names one reservation
    and one amount, and it is spent by the time this call returns. The adapter
    does not need to interpret it -- a real implementation forwards it to the
    rail, which is where C's authority is actually checked.
    """

    attempt_id: str
    capability_token: str

    reservation_id: str
    proposal_id: str
    order_id: str

    principal_id: str
    merchant_id: str
    rail: PaymentRail

    amount_cents: int
    currency: str
    idempotency_key: str


@dataclass(frozen=True)
class PaymentResult:
    """What the rail reported.

    ``provider_reference`` is the rail's own identifier for the movement, and it
    is what a human would quote when reconciling against a statement. It is not
    a receipt: the receipt is C's record, and it is only written once this
    result has been applied inside a transaction.
    """

    status: PaymentStatus
    provider_reference: str | None = None
    code: ErrorCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.status == "SETTLED" and self.code is not None:
            raise ValueError("a settled payment must not carry an error code")
        if self.status != "SETTLED" and self.code is None:
            raise ValueError(
                f"a {self.status} payment must name why, so the user can be told"
            )


@runtime_checkable
class PaymentAdapter(Protocol):
    """Anything that can move money for a capability C issued.

    ``name`` is reported in the audit payload, so a reviewer can tell a sandbox
    settlement from a real one without reading configuration.

    ``source_type`` is the same idea made structural. Every settlement is
    labelled with it, so a receipt can never be read as observed market
    behaviour: the current rail moves no money, and the contract already has the
    vocabulary to say so.
    """

    name: str
    source_type: SourceType

    def charge(self, request: PaymentRequest) -> PaymentResult:
        ...


__all__ = ["PaymentAdapter", "PaymentRequest", "PaymentResult", "PaymentStatus"]
