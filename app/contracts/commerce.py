"""Commerce contracts owned by C.

The central idea: A proposes, C decides. Nothing in this module can be supplied
by A as an authoritative value. A gives product ids, quantities and an
idempotency key; C derives every amount, every hash and every state.

Money fields are integer minor units. ``cash_total_cents`` has exactly one
meaning everywhere in the system:

    cash_total_cents = merchant_total_cents + fee_cents + fx_cost_cents

It is the amount that leaves the principal's account, and it is what every
authorisation cap is compared against. Rewards never reduce it -- rewards are
reported separately as ``effective_cost_cents``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .common import PaymentRail, SourceType
from .product import Category, Connection, Device, FormFactor

ReservationStatus = Literal[
    "ACTIVE",
    "PAYMENT_SUBMITTING",
    "CAPTURED",
    "SETTLED",
    "RELEASED",
    "EXPIRED",
    "FAILED",
    "UNKNOWN",
]

#: Reservation states that still hold budget. Concurrency safety depends on
#: every one of these counting against the caps.
HOLDING_STATUSES: frozenset[str] = frozenset({"ACTIVE", "PAYMENT_SUBMITTING", "UNKNOWN"})

HASH_PREFIX = "sha256:"


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return HASH_PREFIX + hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Quote -- C's authoritative priced offer
# ---------------------------------------------------------------------------

class Quote(BaseModel):
    """A priced offer for one product, produced by C from D's catalog.

    C computes the total itself. A never submits a total, and a submitted total
    would be ignored if it did.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_id: str = Field(min_length=1)

    merchant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    category: Category
    connection: Connection
    form_factor: FormFactor
    anc: bool | None = None
    #: Copied from the catalog when C prices the product, and checked against
    #: the mandate's ``required_device``. It lives on the quote rather than being
    #: read again at decision time because the evaluator is a pure function over
    #: recorded facts: it must be able to replay a decision without a database.
    #: ``None`` means the catalog does not record it, and a requirement naming a
    #: device is refused rather than assumed satisfied (CC-10).
    supported_devices: list[Device] | None = None
    quantity: StrictInt = Field(ge=1)

    unit_price_cents: StrictInt = Field(ge=0)
    subtotal_cents: StrictInt = Field(ge=0)
    shipping_cents: StrictInt = Field(ge=0)
    tax_cents: StrictInt = Field(ge=0)
    discount_cents: StrictInt = Field(ge=0)
    merchant_total_cents: StrictInt = Field(ge=0)

    currency: Literal["HKD"]
    source_type: SourceType
    source_ref: str | None = None

    issued_at: datetime
    expires_at: datetime
    quote_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def _amounts_reconcile(self) -> "Quote":
        if self.subtotal_cents != self.unit_price_cents * self.quantity:
            raise ValueError("subtotal_cents must equal unit_price_cents * quantity")
        expected = self.subtotal_cents + self.shipping_cents + self.tax_cents - self.discount_cents
        if self.merchant_total_cents != expected:
            raise ValueError(
                "merchant_total_cents must equal subtotal + shipping + tax - discount"
            )
        if self.merchant_total_cents < 0:
            raise ValueError("merchant_total_cents must not be negative")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if not self.quote_hash.startswith(HASH_PREFIX):
            raise ValueError(f"quote_hash must start with {HASH_PREFIX!r}")
        return self

    def hashable(self) -> dict[str, Any]:
        """The priced facts a quote hash commits to.

        Deliberately excludes timestamps, so that re-pricing the same basket at
        the same amount yields the same hash and a genuine price change does
        not. ``supported_devices`` is excluded for the same reason ``product_id``
        is *included* but the name is not: this commits to what was priced, and a
        device list is not priced. The device check reads the value the quote
        carries; the hash stays a statement about money.
        """
        return {
            "category": self.category,
            "currency": self.currency,
            "discount_cents": self.discount_cents,
            "merchant_id": self.merchant_id,
            "merchant_total_cents": self.merchant_total_cents,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "shipping_cents": self.shipping_cents,
            "subtotal_cents": self.subtotal_cents,
            "tax_cents": self.tax_cents,
            "unit_price_cents": self.unit_price_cents,
        }

    def compute_hash(self) -> str:
        return _digest(self.hashable())

    def hash_matches(self) -> bool:
        return self.compute_hash() == self.quote_hash

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


# ---------------------------------------------------------------------------
# Payment route evaluation
# ---------------------------------------------------------------------------

class PaymentRouteEvaluation(BaseModel):
    """How one payment rail would settle this quote.

    ``cash_total_cents`` is what leaves the account and what the caps are
    checked against. ``effective_cost_cents`` subtracts a conservatively valued
    reward and is used only for ranking between eligible routes -- never for
    authorisation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = Field(min_length=1)
    rail: PaymentRail

    accepted_by_merchant: bool
    allowed_by_mandate: bool
    owned_by_principal: bool
    eligible: bool

    merchant_total_cents: StrictInt = Field(ge=0)
    fee_cents: StrictInt = Field(ge=0)
    fx_cost_cents: StrictInt = Field(ge=0)
    cash_total_cents: StrictInt = Field(ge=0)

    reward_value_cents: StrictInt = Field(default=0, ge=0)
    effective_cost_cents: StrictInt = Field(ge=0)

    evidence_id: str | None = None
    evidence_type: SourceType

    rejection_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _arithmetic_and_eligibility_agree(self) -> "PaymentRouteEvaluation":
        if self.cash_total_cents != (
            self.merchant_total_cents + self.fee_cents + self.fx_cost_cents
        ):
            raise ValueError(
                "cash_total_cents must equal merchant_total + fee_cents + fx_cost_cents"
            )
        if self.effective_cost_cents != max(
            0, self.cash_total_cents - self.reward_value_cents
        ):
            raise ValueError(
                "effective_cost_cents must equal cash_total_cents - reward_value_cents, floored at 0"
            )
        if self.eligible and self.rejection_reasons:
            raise ValueError("an eligible route must not carry rejection reasons")
        if not self.eligible and not self.rejection_reasons:
            raise ValueError("an ineligible route must explain why")
        return self

    @property
    def is_usable(self) -> bool:
        return self.eligible


# ---------------------------------------------------------------------------
# Purchase proposal -- A's ask, never an authorisation
# ---------------------------------------------------------------------------

class PurchaseProposal(BaseModel):
    """What A submits. Carries no authority and no derived amounts.

    Deliberately absent: remaining budget, current spend, approval decision,
    policy hash, payment status, wallet balance, reward earned. C derives all
    of those. A value supplied for any of them is a contract violation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(min_length=1)

    mandate_id: str = Field(min_length=1)
    expected_mandate_version: StrictInt = Field(ge=1)

    principal_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)

    product_id: str = Field(min_length=1)
    quantity: StrictInt = Field(ge=1)
    merchant_id: str = Field(min_length=1)

    quote_id: str = Field(min_length=1)
    preferred_payment_route_ids: list[str] = Field(default_factory=list)

    shipping_address_id: str = Field(min_length=1)

    request_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    created_at: datetime

    def request_hash(self) -> str:
        """Identity of the *content* of this proposal.

        Two submissions with the same idempotency key but a different hash are a
        conflict, not a replay.
        """
        return _digest({
            "agent_id": self.agent_id,
            "expected_mandate_version": self.expected_mandate_version,
            "mandate_id": self.mandate_id,
            "merchant_id": self.merchant_id,
            "preferred_payment_route_ids": list(self.preferred_payment_route_ids),
            "principal_id": self.principal_id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "quote_id": self.quote_id,
            "shipping_address_id": self.shipping_address_id,
        })


# ---------------------------------------------------------------------------
# Reservation -- C's hold on budget
# ---------------------------------------------------------------------------

class Reservation(BaseModel):
    """A hold placed on budget after a policy approval.

    A reservation is created only on APPROVE. A DENY never creates one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)

    principal_id: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    mandate_version: StrictInt = Field(ge=1)

    quote_id: str = Field(min_length=1)
    quote_hash: str = Field(min_length=1)
    payment_route_id: str = Field(min_length=1)

    amount_cents: StrictInt = Field(ge=0)
    currency: Literal["HKD"]
    quantity: StrictInt = Field(ge=1)

    status: ReservationStatus

    created_at: datetime
    expires_at: datetime

    @property
    def holds_budget(self) -> bool:
        """Whether this reservation still counts against the caps."""
        return self.status in HOLDING_STATUSES


# ---------------------------------------------------------------------------
# Spend state -- the only correct input to a cap check
# ---------------------------------------------------------------------------

class SpendState(BaseModel):
    """Cumulative exposure for one principal and mandate window.

    Held amounts are counted, not just settled ones. Otherwise two concurrent
    proposals each observe the full remaining budget and the pair overspends.

    **Window semantics (fixed here, because two implementations both passed the
    old contract and disagreed on the answer).**

    The window is ``[now - window_seconds, now]``, a *sliding* window measured
    from the evaluation instant. It is not ``[rolling_window_start, now]``, and
    ``rolling_window_start`` is not the start of the window being judged -- it is
    the caller's record of when the counts it supplies begin. The evaluator uses
    it only to decide whether the counts are still inside the window at all::

        if now - rolling_window_start < mandate.rolling_window_seconds:
            compare exposure against the cap     # counts are current
        else:
            treat exposure as zero               # every count has aged out

    So the counts must already describe exactly the sliding window. The caller
    recomputes them at query time over ``now - window_seconds``; it does not
    accumulate since ``rolling_window_start``.

    A worked example, with ``rolling_window_seconds = 86400``:

    * at 10:00 a purchase settles for 20000
    * at 11:00 the caller supplies ``settled_cents = 20000`` and
      ``rolling_window_start = 10:00``; the window reaches back to 10:00 the
      previous day, so the purchase counts and exposure is 20000
    * at 10:00 the next day the purchase has aged out: the caller recomputes
      ``settled_cents = 0`` over the new window, and exposure is 0 even though
      ``rolling_window_start`` may still read 10:00

    The contract cannot enforce this from the numbers alone -- a caller that
    accumulates instead of recomputing produces plausible, wrong values. It is
    stated here so the repository layer has one documented obligation, and it is
    covered by a test at that layer rather than in the evaluator.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    settled_cents: StrictInt = Field(default=0, ge=0)
    captured_cents: StrictInt = Field(default=0, ge=0)
    reserved_cents: StrictInt = Field(default=0, ge=0)

    settled_count: StrictInt = Field(default=0, ge=0)
    captured_count: StrictInt = Field(default=0, ge=0)
    reserved_count: StrictInt = Field(default=0, ge=0)

    purchased_quantity: StrictInt = Field(default=0, ge=0)
    reserved_quantity: StrictInt = Field(default=0, ge=0)

    #: When the supplied counts begin. See the window semantics above.
    rolling_window_start: datetime
    #: Same idea for the velocity window, which uses ``exposure_count``.
    velocity_window_start: datetime

    @property
    def exposure_cents(self) -> int:
        """Settled, captured, or still held by an open reservation."""
        return self.settled_cents + self.captured_cents + self.reserved_cents

    @property
    def exposure_count(self) -> int:
        return self.settled_count + self.captured_count + self.reserved_count

    @property
    def exposure_quantity(self) -> int:
        return self.purchased_quantity + self.reserved_quantity

    def with_reservation(self, amount_cents: int, quantity: int) -> "SpendState":
        """Return the state as it would be once a reservation is taken."""
        return self.model_copy(update={
            "reserved_cents": self.reserved_cents + amount_cents,
            "reserved_count": self.reserved_count + 1,
            "reserved_quantity": self.reserved_quantity + quantity,
        })


__all__ = [
    "HASH_PREFIX",
    "HOLDING_STATUSES",
    "PaymentRouteEvaluation",
    "PurchaseProposal",
    "Quote",
    "Reservation",
    "ReservationStatus",
    "SpendState",
]
