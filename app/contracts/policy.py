"""Policy decision contracts, and the settlement record that follows one.

These types are produced by C. A only renders them; A never constructs a
decision, a receipt or a failure.

The first group comes out of C's deterministic policy evaluator. The second --
:class:`PaymentFailure` and :class:`ProposalOutcome` -- is the read model for
"what happened to this proposal", and it exists because the evaluator's output
cannot answer that on its own: a decision says whether the purchase may proceed,
and the outcome says what then happened to it.

The two boolean fields on every decision and receipt exist so that the safety
claim is auditable rather than asserted:

* ``reservation_created`` must be False for DENY
* ``payment_adapter_called`` must be False for DENY and ESCALATE

A reviewer, judge or test can check those without trusting the prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .common import ErrorCode, PaymentRail, SourceType
from .commerce import Reservation, ReservationStatus
from .mandate import HASH_PREFIX

Outcome = Literal["APPROVE", "DENY", "ESCALATE"]

#: A violation may report a scalar, a list (a set of allowed values) or nothing.
#: Kept as ``Any`` on purpose: restricting this to scalars forced the evaluator
#: to drop the context that makes a denial explainable.
ObservedValue = Any


class PolicyViolation(BaseModel):
    """One reason a proposal failed a rule.

    ``observed`` and ``limit`` are what make a denial explainable: the user is
    told which measured value met which recorded limit, not a paraphrase
    written after the fact.
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    field: str
    message: str
    observed: ObservedValue = None
    limit: ObservedValue = None

    def describe(self) -> str:
        if self.observed is None and self.limit is None:
            return f"{self.field}: {self.message}"
        return (
            f"{self.field}: {self.message} "
            f"(observed={self.observed!r}, limit={self.limit!r})"
        )


class PolicyDecision(BaseModel):
    """The result of evaluating one proposal against one mandate version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Outcome

    primary_reason: ErrorCode | None = None
    violations: list[PolicyViolation] = Field(default_factory=list)

    mandate_id: str
    mandate_version: StrictInt = Field(ge=1)
    policy_hash: str

    proposal_id: str
    quote_id: str
    quote_hash: str
    cash_total_cents: StrictInt = Field(ge=0)
    currency: str
    payment_route_id: str | None = None

    observed_values: dict[str, Any] = Field(default_factory=dict)
    applicable_limits: dict[str, Any] = Field(default_factory=dict)

    reservation_created: bool = False
    payment_adapter_called: bool = False

    evaluated_at: datetime

    @model_validator(mode="after")
    def _outcome_is_self_consistent(self) -> "PolicyDecision":
        if self.outcome == "APPROVE":
            if self.violations:
                raise ValueError("APPROVE must not carry violations")
            if self.primary_reason is not None:
                raise ValueError("APPROVE must not carry a primary reason")
        else:
            if not self.primary_reason:
                raise ValueError(f"{self.outcome} must carry a primary reason")
        if self.outcome in ("DENY", "ESCALATE"):
            if self.reservation_created:
                raise ValueError(f"{self.outcome} must never create a reservation")
            if self.payment_adapter_called:
                raise ValueError(f"{self.outcome} must never call the payment adapter")
        return self

    @property
    def is_approved(self) -> bool:
        return self.outcome == "APPROVE"

    def ordered_violations(self) -> list[PolicyViolation]:
        """Violations sorted by check order for stable rendering and testing."""
        return sorted(self.violations, key=lambda v: (v.field, v.code.value))


class DenialReceipt(BaseModel):
    """Durable record of a refusal. Evidence for the HKT demo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    denial_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)

    mandate_id: str
    mandate_version: StrictInt = Field(ge=1)
    policy_hash: str

    primary_reason: ErrorCode
    violations: list[PolicyViolation] = Field(default_factory=list)

    observed_values: dict[str, Any] = Field(default_factory=dict)
    applicable_limits: dict[str, Any] = Field(default_factory=dict)

    quote_id: str
    cash_total_cents: StrictInt = Field(ge=0)
    currency: str

    reservation_created: bool = False
    payment_adapter_called: bool = False

    created_at: datetime
    audit_event_id: str | None = None

    @model_validator(mode="after")
    def _must_not_have_moved_money(self) -> "DenialReceipt":
        if self.reservation_created:
            raise ValueError("a denial must never have created a reservation")
        if self.payment_adapter_called:
            raise ValueError("a denial must never have called the payment adapter")
        return self


class EscalationRequest(BaseModel):
    """A question the agent could not answer on its own.

    Expiry is fail-closed: once ``expires_at`` passes the purchase is denied. An
    unanswered escalation never becomes an approval.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    escalation_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)

    mandate_id: str
    mandate_version: StrictInt = Field(ge=1)

    quote_id: str
    quote_hash: str
    cash_total_cents: StrictInt = Field(ge=0)
    escalate_above_cents: StrictInt = Field(ge=0)
    currency: str

    question: str = Field(min_length=1)

    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    resolution: Literal["APPROVED", "REJECTED", "TIMEOUT"] | None = None

    def is_open(self, now: datetime) -> bool:
        return self.resolved_at is None and now < self.expires_at

    def has_expired(self, now: datetime) -> bool:
        return self.resolved_at is None and now >= self.expires_at


class PaymentReceipt(BaseModel):
    """C's authoritative record of a settled payment.

    A renders this; it never produces one.

    Money naming is deliberately the same as everywhere else in the system:
    ``cash_total_cents`` is what left the account, and it is what the caps were
    compared against. An earlier version called it ``amount_cents`` while
    carrying a separate ``fee_cents`` and never said whether the fee was inside
    the amount -- two readers could disagree about the same receipt, which is
    exactly the confusion the shared name is meant to remove.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payment_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)

    principal_id: str
    mandate_id: str
    mandate_version: StrictInt = Field(ge=1)

    quote_id: str
    quote_hash: str
    payment_route_id: str
    rail: PaymentRail | None = None

    #: Amount that left the account: merchant total + fee + FX cost.
    cash_total_cents: StrictInt = Field(ge=0)
    #: The part of ``cash_total_cents`` that was fee rather than goods. Present
    #: for reconciliation, never additional to the total.
    fee_cents: StrictInt = Field(default=0, ge=0)
    fx_cost_cents: StrictInt = Field(default=0, ge=0)
    merchant_total_cents: StrictInt | None = Field(default=None, ge=0)

    currency: str
    balance_after_cents: StrictInt = Field(ge=0)

    reservation_status: ReservationStatus
    order_status: str

    reward_earned_cents: StrictInt = Field(default=0, ge=0)
    reward_evidence_type: str | None = None

    settled_at: datetime
    audit_event_id: str | None = None

    @model_validator(mode="after")
    def _fee_is_inside_the_total(self) -> "PaymentReceipt":
        if self.fee_cents > self.cash_total_cents:
            raise ValueError("fee_cents must not exceed cash_total_cents")
        if self.fx_cost_cents > self.cash_total_cents:
            raise ValueError("fx_cost_cents must not exceed cash_total_cents")
        if self.merchant_total_cents is not None:
            expected = self.merchant_total_cents + self.fee_cents + self.fx_cost_cents
            if self.cash_total_cents != expected:
                raise ValueError(
                    "cash_total_cents must equal merchant_total_cents + fee_cents "
                    "+ fx_cost_cents when merchant_total_cents is given"
                )
        return self


class PaymentFailure(BaseModel):
    """A settlement attempt that did not settle.

    Deliberately not an exception and deliberately not a ``PolicyDecision``.
    Three things are true at once when a payment is refused -- the purchase was
    *authorised*, an attempt was *made*, and the money did *not* move -- and
    collapsing any of the three loses the part a user needs. A raised error
    cannot carry it either: the HTTP envelope would report a failed request for
    a purchase the mandate permitted, which reads as the agent being broken
    rather than as the rail saying no.

    ``ErrorCode`` already names the four ways this happens --
    ``INSUFFICIENT_BALANCE``, ``OUT_OF_STOCK``, ``PAYMENT_FAILED`` and
    ``PAYMENT_STATUS_UNKNOWN`` -- and until now there was no type to carry one
    to A. ``retryable`` is reported rather than inferred, because the honest
    answer for ``PAYMENT_STATUS_UNKNOWN`` is that retrying may charge twice.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)

    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool = False

    #: The rail's own identifier, when it gave one. Never a credential.
    provider_reference: str | None = None

    failed_at: datetime
    audit_event_id: str | None = None


class ProposalOutcome(BaseModel):
    """Everything C recorded about one proposal, in one object.

    This is the second half of the A -> C boundary. A submits a
    ``PurchaseProposal``; what comes back is not one value but a small history
    -- a decision, and then either a denial, an open question, or a reservation
    that was paid for or refused. Before this type existed, ``DenialReceipt``,
    ``EscalationRequest``, ``Reservation`` and ``PaymentReceipt`` were all
    defined with no path from C to A, so four of the five things a user is owed
    an explanation for could not be delivered at all.

    A read model rather than a fifth write path: it is assembled from C's own
    rows, it changes nothing, and asking for it twice returns the same answer.
    The fields below are each C's own contract type, passed through unmodified,
    so A renders from structured data instead of parsing prose back into it.

    ``settlement_source_type`` is the honesty field. The current rail is a
    sandbox that moves no money, and a receipt that does not say so invites
    exactly the misreading the problem statement calls fabrication. It is
    ``None`` until a settlement has been attempted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(min_length=1)

    #: The latest decision for this proposal. Never ``None``: a proposal that
    #: was submitted has been evaluated.
    decision: PolicyDecision

    denial: DenialReceipt | None = None
    escalation: EscalationRequest | None = None
    reservation: Reservation | None = None
    receipt: PaymentReceipt | None = None
    payment_failure: PaymentFailure | None = None

    settlement_source_type: SourceType | None = None

    @model_validator(mode="after")
    def _the_pieces_agree_with_the_decision(self) -> "ProposalOutcome":
        if self.denial is not None and self.decision.outcome != "DENY":
            raise ValueError("only a denied proposal carries a denial receipt")
        if self.denial is not None and self.denial.proposal_id != self.proposal_id:
            raise ValueError("the denial receipt is for a different proposal")
        if self.escalation is not None and self.escalation.proposal_id != self.proposal_id:
            raise ValueError("the escalation is for a different proposal")
        if self.reservation is not None and self.reservation.proposal_id != self.proposal_id:
            raise ValueError("the reservation is for a different proposal")
        if self.receipt is not None and self.reservation is None:
            raise ValueError("a settled payment implies a reservation")
        if self.payment_failure is not None and self.receipt is not None:
            raise ValueError("a proposal cannot be both settled and failed")
        if self.payment_failure is not None and self.payment_failure.proposal_id != self.proposal_id:
            raise ValueError("the payment failure is for a different proposal")
        return self

    @property
    def is_settled(self) -> bool:
        return self.receipt is not None


__all__ = [
    "DenialReceipt",
    "EscalationRequest",
    "HASH_PREFIX",
    "ObservedValue",
    "Outcome",
    "PaymentFailure",
    "PaymentReceipt",
    "PolicyDecision",
    "PolicyViolation",
    "ProposalOutcome",
]
