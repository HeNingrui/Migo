"""The deterministic policy evaluator.

This is the single place where a purchase is permitted or refused, and it is a
pure function. It performs no I/O, opens no transaction, calls no LLM, reads no
clock, issues no capability and writes nothing. The same inputs always produce
the same decision.

Why that matters here: an approval or denial can be replayed and checked by a
third party from the recorded inputs, so "why did it do that?" is answered from
the rule rather than from an explanation written afterwards.

Transaction placement: this function must be called *inside* the caller's write
transaction, after every input has been read and before any reservation row is
written. Approval and reservation creation must not be separable, otherwise two
concurrent proposals can each observe the same remaining budget.

    BEGIN IMMEDIATE
        read proposal, mandate, quote, route, spend_state
        decision = evaluate_policy(...)      <-- here
        DENY/ESCALATE: write receipt + audit, no reservation
        APPROVE:       write reservation + audit
    COMMIT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.contracts.commerce import (
    PaymentRouteEvaluation,
    PurchaseProposal,
    Quote,
    SpendState,
)
from app.contracts.common import ErrorCode
from app.contracts.mandate import ApprovalGrant, Mandate
from app.contracts.policy import PolicyDecision, PolicyViolation

#: Grace applied when the evaluator's clock reads marginally earlier than the
#: clock that stamped a row. Anything beyond this is a real time inconsistency.
CLOCK_SKEW = timedelta(seconds=5)


@dataclass(frozen=True)
class PolicyInputs:
    """Everything the decision depends on.

    Bundled so the evaluator can be called with one argument and so a recorded
    decision can be replayed byte-for-byte.
    """

    proposal: PurchaseProposal
    mandate: Mandate
    quote: Quote
    route: PaymentRouteEvaluation
    spend_state: SpendState
    now: datetime
    approval_grant: ApprovalGrant | None = None


@dataclass
class _Check:
    """Accumulates violations and the values behind them."""

    observed: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    violations: list[PolicyViolation] = field(default_factory=list)
    order: list[ErrorCode] = field(default_factory=list)

    def fail(
        self,
        code: ErrorCode,
        *,
        field_name: str,
        message: str,
        observed: Any = None,
        limit: Any = None,
    ) -> None:
        if observed is not None:
            self.observed[field_name] = observed
        if limit is not None:
            self.limits[field_name] = limit
        self.violations.append(
            PolicyViolation(
                code=code,
                field=field_name,
                message=message,
                observed=observed,
                limit=limit,
            )
        )
        self.order.append(code)


def as_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so comparisons never raise.

    Public because C's other services must normalise the same way: a spend-state
    window computed under a different convention than the evaluator compares
    with is a cap check measuring something other than what it reports. One
    definition, imported -- rather than a second one that agrees until it does
    not.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def evaluate_policy(inputs: PolicyInputs) -> PolicyDecision:
    """Decide whether one proposal may proceed.

    Checks run in a fixed order (see the module docstring of the commerce spec).
    Every violated rule is recorded; one of them is named as the primary reason
    so the user gets an actionable message rather than a list.
    """
    proposal = inputs.proposal
    mandate = inputs.mandate
    quote = inputs.quote
    route = inputs.route
    spend_state = inputs.spend_state
    now = as_utc(inputs.now)

    check = _Check()
    check.observed["cash_total_cents"] = route.cash_total_cents
    check.limits["cap_per_transaction_cents"] = mandate.cap_per_transaction_cents
    check.limits["rolling_cap_cents"] = mandate.rolling_cap_cents

    # -- 1-2 identity: whose money, which agent ---------------------------
    if proposal.principal_id != mandate.principal_id:
        check.fail(
            ErrorCode.PRINCIPAL_MISMATCH,
            field_name="principal_id",
            message="the proposal was submitted for a different principal",
            observed=proposal.principal_id,
            limit=mandate.principal_id,
        )
    if proposal.agent_id != mandate.agent_id:
        check.fail(
            ErrorCode.AGENT_MISMATCH,
            field_name="agent_id",
            message="the proposal was submitted by a different agent",
            observed=proposal.agent_id,
            limit=mandate.agent_id,
        )

    # -- 3-4 mandate state and version ------------------------------------
    if mandate.status == "REVOKED":
        check.fail(
            ErrorCode.MANDATE_REVOKED,
            field_name="mandate.status",
            message="the mandate has been revoked",
            observed="REVOKED",
            limit="ACTIVE",
        )
    elif mandate.status == "SUPERSEDED":
        check.fail(
            ErrorCode.MANDATE_VERSION_STALE,
            field_name="mandate.status",
            message="this mandate version has been superseded",
            observed="SUPERSEDED",
            limit="ACTIVE",
        )
    elif mandate.status == "EXPIRED":
        check.fail(
            ErrorCode.MANDATE_EXPIRED,
            field_name="mandate.status",
            message="the mandate has expired",
            observed="EXPIRED",
            limit="ACTIVE",
        )

    if proposal.expected_mandate_version != mandate.version:
        # The agent acted on a stale copy: this is how a revocation lands on a
        # purchase that was already in flight.
        check.fail(
            ErrorCode.MANDATE_VERSION_STALE,
            field_name="expected_mandate_version",
            message="the agent acted on a stale mandate version",
            observed=proposal.expected_mandate_version,
            limit=mandate.version,
        )

    # -- 5-6 validity window ----------------------------------------------
    valid_from = as_utc(mandate.valid_from)
    expires_at = as_utc(mandate.expires_at)
    if now + CLOCK_SKEW < valid_from:
        check.fail(
            ErrorCode.MANDATE_NOT_ACTIVE,
            field_name="valid_from",
            message="the mandate is not yet in effect",
            observed=now.isoformat(),
            limit=valid_from.isoformat(),
        )
    if now >= expires_at:
        check.fail(
            ErrorCode.MANDATE_EXPIRED,
            field_name="expires_at",
            message="the mandate has expired",
            observed=now.isoformat(),
            limit=expires_at.isoformat(),
        )

    # -- 7-9 scope: merchant, category, connection ------------------------
    if proposal.merchant_id not in mandate.allowed_merchants:
        check.fail(
            ErrorCode.MERCHANT_NOT_ALLOWED,
            field_name="merchant_id",
            message="the merchant is outside the mandate",
            observed=proposal.merchant_id,
            limit=list(mandate.allowed_merchants),
        )
    if quote.product_id != proposal.product_id:
        check.fail(
            ErrorCode.QUOTE_HASH_MISMATCH,
            field_name="quote.product_id",
            message="the quote is for a different product than the proposal",
            observed=quote.product_id,
            limit=proposal.product_id,
        )
    if quote.merchant_id != proposal.merchant_id:
        check.fail(
            ErrorCode.QUOTE_CHANGED,
            field_name="quote.merchant_id",
            message="the quote is from a different merchant than the proposal",
            observed=quote.merchant_id,
            limit=proposal.merchant_id,
        )
    if quote.category not in mandate.allowed_categories:
        check.fail(
            ErrorCode.CATEGORY_NOT_ALLOWED,
            field_name="category",
            message="the category is outside the mandate",
            observed=quote.category,
            limit=list(mandate.allowed_categories),
        )
    if (
        mandate.required_connection is not None
        and quote.connection != mandate.required_connection
    ):
        check.fail(
            ErrorCode.CONNECTION_NOT_ALLOWED,
            field_name="connection",
            message="the connection type does not match the mandate",
            observed=quote.connection,
            limit=mandate.required_connection,
        )
    # Unknown ANC never satisfies a mandate that requires ANC.
    if mandate.anc_required and quote.anc is not True:
        check.fail(
            ErrorCode.ANC_REQUIREMENT_NOT_MET,
            field_name="anc",
            message="the mandate requires active noise cancelling and this product does not confirm it",
            observed=quote.anc,
            limit=True,
        )
    # The same fail-closed rule for the device the purchase must work with.
    # ``supported_devices`` is read from the quote, which copied it from the
    # catalog under C's own read -- a recommender's claim about compatibility is
    # not an input here.
    if mandate.required_device is not None:
        declared = quote.supported_devices
        if declared is None or mandate.required_device not in declared:
            check.fail(
                ErrorCode.DEVICE_REQUIREMENT_NOT_MET,
                field_name="required_device",
                message=(
                    "the mandate requires this product to work with a device the "
                    "trusted product data does not confirm"
                ),
                observed=(None if declared is None else list(declared)),
                limit=mandate.required_device,
            )

    # -- 10 quantity ------------------------------------------------------
    if quote.quantity != proposal.quantity:
        check.fail(
            ErrorCode.QUOTE_CHANGED,
            field_name="quote.quantity",
            message="the quote is for a different quantity than the proposal",
            observed=quote.quantity,
            limit=proposal.quantity,
        )
    if proposal.quantity > mandate.max_quantity_total:
        check.fail(
            ErrorCode.QUANTITY_LIMIT_EXCEEDED,
            field_name="quantity",
            message="the quantity exceeds the mandate limit",
            observed=proposal.quantity,
            limit=mandate.max_quantity_total,
        )

    # -- 11 address -------------------------------------------------------
    if proposal.shipping_address_id != mandate.shipping_address_id:
        allowed = mandate.address_change_allowed
        check.fail(
            ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED if not allowed else ErrorCode.PRINCIPAL_MISMATCH,
            field_name="shipping_address_id",
            message=(
                "the mandate does not allow changing the shipping address"
                if not allowed
                else "the shipping address does not match the mandate"
            ),
            observed=proposal.shipping_address_id,
            limit=mandate.shipping_address_id,
        )

    # -- 12 payment route -------------------------------------------------
    check.observed["payment_route_id"] = route.route_id
    if route.route_id not in mandate.allowed_payment_routes:
        check.fail(
            ErrorCode.PAYMENT_ROUTE_NOT_ALLOWED,
            field_name="payment_route_id",
            message="the payment route is not permitted by the mandate",
            observed=route.route_id,
            limit=list(mandate.allowed_payment_routes),
        )
    elif not route.eligible:
        check.fail(
            ErrorCode.PAYMENT_ROUTE_NOT_ALLOWED,
            field_name="payment_route_id",
            message="the payment route is not usable for this purchase",
            observed=route.rejection_reasons,
            limit="eligible",
        )

    # -- 13-16 quote integrity --------------------------------------------
    if quote.quote_id != proposal.quote_id:
        check.fail(
            ErrorCode.QUOTE_CHANGED,
            field_name="quote.quote_id",
            message="the proposal references a different quote",
            observed=quote.quote_id,
            limit=proposal.quote_id,
        )
    if not quote.hash_matches():
        check.fail(
            ErrorCode.QUOTE_HASH_MISMATCH,
            field_name="quote.quote_hash",
            message="the quote contents do not match the recorded quote hash",
            observed=quote.quote_hash,
            limit=quote.compute_hash(),
        )
    issued_at = as_utc(quote.issued_at)
    quote_expires_at = as_utc(quote.expires_at)
    if now + CLOCK_SKEW < issued_at:
        check.fail(
            ErrorCode.QUOTE_CHANGED,
            field_name="quote.issued_at",
            message="the quote was issued in the future",
            observed=now.isoformat(),
            limit=issued_at.isoformat(),
        )
    if now >= quote_expires_at:
        check.fail(
            ErrorCode.QUOTE_EXPIRED,
            field_name="quote.expires_at",
            message="the quote has expired and must be re-priced",
            observed=now.isoformat(),
            limit=quote_expires_at.isoformat(),
        )
    if route.merchant_total_cents != quote.merchant_total_cents:
        check.fail(
            ErrorCode.QUOTE_CHANGED,
            field_name="route.merchant_total_cents",
            message="the route was priced against a different quote total",
            observed=route.merchant_total_cents,
            limit=quote.merchant_total_cents,
        )
    if quote.currency != mandate.currency:
        check.fail(
            ErrorCode.QUOTE_CURRENCY_MISMATCH,
            field_name="quote.currency",
            message="the quote currency does not match the mandate currency",
            observed=quote.currency,
            limit=mandate.currency,
        )

    # -- 17 velocity ------------------------------------------------------
    velocity_window_start = as_utc(spend_state.velocity_window_start)
    if now - velocity_window_start < timedelta(seconds=mandate.velocity_window_seconds):
        if spend_state.exposure_count >= mandate.velocity_max_count:
            check.fail(
                ErrorCode.VELOCITY_LIMIT_EXCEEDED,
                field_name="velocity_max_count",
                message="too many purchases inside the velocity window",
                observed=spend_state.exposure_count,
                limit=mandate.velocity_max_count,
            )

    # -- 18-19 amount caps, measured on the cash total --------------------
    cash_total = route.cash_total_cents
    if cash_total > mandate.cap_per_transaction_cents:
        check.fail(
            ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
            field_name="cap_per_transaction_cents",
            message="the cash total exceeds the per-transaction cap",
            observed=cash_total,
            limit=mandate.cap_per_transaction_cents,
        )

    rolling_window_start = as_utc(spend_state.rolling_window_start)
    if now - rolling_window_start < timedelta(seconds=mandate.rolling_window_seconds):
        projected = spend_state.exposure_cents + cash_total
        check.observed["projected_exposure_cents"] = projected
        check.observed["current_exposure_cents"] = spend_state.exposure_cents
        if projected > mandate.rolling_cap_cents:
            check.fail(
                ErrorCode.ROLLING_CAP_EXCEEDED,
                field_name="rolling_cap_cents",
                message="the rolling window cap would be exceeded",
                observed=projected,
                limit=mandate.rolling_cap_cents,
            )

    # -- 20 lifetime quantity ---------------------------------------------
    projected_quantity = spend_state.exposure_quantity + proposal.quantity
    check.observed["projected_quantity"] = projected_quantity
    if projected_quantity > mandate.max_quantity_total:
        check.fail(
            ErrorCode.QUANTITY_TOTAL_EXCEEDED,
            field_name="max_quantity_total",
            message="the lifetime purchase quantity would be exceeded",
            observed=projected_quantity,
            limit=mandate.max_quantity_total,
        )

    # -- 21 escalation ----------------------------------------------------
    escalate_above = mandate.escalate_above_cents
    needs_escalation = escalate_above is not None and cash_total > escalate_above
    if needs_escalation:
        check.observed["escalate_above_cents"] = escalate_above
        issue = _grant_problem(inputs, now)
        if issue is not None:
            check.fail(
                ErrorCode.ESCALATION_REQUIRED,
                field_name="escalate_above_cents",
                message="this amount needs the principal's approval before it can proceed",
                observed=cash_total,
                limit=escalate_above,
            )

    # -- outcome ----------------------------------------------------------
    if check.violations:
        # Escalation is the only outcome that asks the principal instead of
        # refusing. It is ESCALATE only when escalation is the sole problem:
        # any hard violation still denies.
        outcome = "DENY"
        if all(code == ErrorCode.ESCALATION_REQUIRED for code in check.order):
            outcome = "ESCALATE"
        return _decision(
            inputs,
            outcome=outcome,
            violations=check.violations,
            primary=check.order[0],
            check=check,
            reservation_created=False,
        )

    return _decision(
        inputs,
        outcome="APPROVE",
        violations=[],
        primary=None,
        check=check,
        reservation_created=True,
    )


def _grant_problem(inputs: PolicyInputs, now: datetime) -> str | None:
    """Return why the principal's approval does not cover this purchase.

    ``None`` means a valid, unexpired grant covers the exact priced facts.
    """
    grant = inputs.approval_grant
    if grant is None:
        return "no approval grant on file"
    if not grant.is_usable(now):
        return "the approval grant has expired or was already used"
    if grant.proposal_id != inputs.proposal.proposal_id:
        return "the approval grant is for a different proposal"
    if grant.mandate_id != inputs.mandate.mandate_id:
        return "the approval grant is for a different mandate"
    if grant.mandate_version != inputs.mandate.version:
        return "the approval grant is for a stale mandate version"
    if grant.principal_id != inputs.proposal.principal_id:
        return "the approval grant belongs to a different principal"
    if not grant.covers(
        quote_hash_=inputs.quote.quote_hash,
        payment_route_id=inputs.route.route_id,
        cash_total_cents=inputs.route.cash_total_cents,
    ):
        # The price, the route or the amount moved after the user said yes.
        return "the quote, route or amount changed after approval"
    return None


def _decision(
    inputs: PolicyInputs,
    *,
    outcome: str,
    violations: list[PolicyViolation],
    primary: ErrorCode | None,
    check: _Check,
    reservation_created: bool,
) -> PolicyDecision:
    return PolicyDecision(
        outcome=outcome,
        primary_reason=primary,
        violations=violations,
        mandate_id=inputs.mandate.mandate_id,
        mandate_version=inputs.mandate.version,
        policy_hash=inputs.mandate.policy_hash,
        proposal_id=inputs.proposal.proposal_id,
        quote_id=inputs.quote.quote_id,
        quote_hash=inputs.quote.quote_hash,
        cash_total_cents=inputs.route.cash_total_cents,
        currency=inputs.quote.currency,
        payment_route_id=inputs.route.route_id,
        observed_values=check.observed,
        applicable_limits=check.limits,
        reservation_created=reservation_created,
        payment_adapter_called=False,
        evaluated_at=as_utc(inputs.now),
    )


class PolicyEvaluator:
    """Thin object wrapper so the evaluator can be injected like any other service.

    Holds no state. The logic lives in :func:`evaluate_policy`, which stays
    directly callable and trivially testable.
    """

    def evaluate(self, inputs: PolicyInputs) -> PolicyDecision:
        return evaluate_policy(inputs)

    __call__ = evaluate


__all__ = ["CLOCK_SKEW", "PolicyEvaluator", "PolicyInputs", "as_utc", "evaluate_policy"]
