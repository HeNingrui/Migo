"""Settlement: C validates its own authority, then moves the money.

The flow this module implements is the one the plan specifies, and the ordering
inside it is the whole design::

    claim the reservation, re-check the mandate and the goods   (one transaction)
    issue a capability, and spend it before anything outside C is called
    ---- call the rail, holding no database lock ----
    debit, decrement stock, write the receipt                   (one transaction)

Four decisions worth stating, because the obvious alternative is wrong in each
case.

**The rail is called with no lock held.** Holding SQLite's write lock across a
network call turns a slow payment processor into a global outage, and this is a
single-writer database. So the attempt is *claimed* first -- the reservation
moves to ``PAYMENT_SUBMITTING``, which is what stops a second attempt from
picking it up -- and the ledger moves only once the rail has answered.

**The capability is spent before the call, not after.** A bearer credential that
is still valid during the call it authorises is a credential that can be
presented twice if the process dies mid-call. Spending it first means a crash
leaves a reservation that is claimed and a nonce that is burnt: the honest
failure, rather than a second charge.

**``UNKNOWN`` is not ``FAILED``.** A timeout means the money may have moved. The
reservation keeps holding budget and the outcome is reported as unknown, because
releasing it would hand the same budget out again while the first payment might
still settle, and retrying automatically is how a customer is charged twice.

**The debit is conditional.** ``balance_cents >= ?`` lives inside the UPDATE, so
the check and the write are one operation. A read followed by a write would let
two settlements both observe enough money.

**What happens if the rail settles and C then cannot complete.** The ledger debit
and the stock decrement run after the rail has answered, because the reverse
order has no compensation: D's repository can put stock back only by taking it,
while a debit has a credit. So a post-settlement failure is reported as
``PAYMENT_STATUS_UNKNOWN`` rather than as a failure -- the money may be gone and
saying otherwise would be a lie -- and any partial ledger effect is reversed and
named in the message. This is reachable, and ``tests/commerce/test_payment.py``
reaches it with an adapter that empties the wallet or the shelf while the rail is
thinking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

from app.commerce.adapters import PaymentAdapter, PaymentRequest, SandboxPaymentAdapter
from app.commerce.audit import AuditLog
from app.commerce.capability import CapabilityService
from app.commerce.database import new_id, write_transaction
from app.commerce.policy_evaluator import as_utc
from app.commerce.quote_service import SandboxPaymentRouter
from app.commerce.repositories import (
    MandateRepository,
    OrderRepository,
    PaymentAttemptRepository,
    ProposalRepository,
    QuoteRepository,
    ReservationRepository,
    WalletRepository,
)
from app.contracts.audit import AuditEventType
from app.contracts.commerce import (
    PaymentRouteEvaluation,
    PurchaseProposal,
    Quote,
    Reservation,
)
from app.contracts.common import ActorType, ErrorCode, SourceType, is_retryable
from app.contracts.mandate import Mandate
from app.contracts.policy import PaymentFailure, PaymentReceipt

#: Actor recorded on every event this module writes. C is the actor: the agent
#: asked, and the commerce layer is what acted.
_ACTOR = "payment_service"

#: The identity used for events written by the settlement path itself.
_ORDER_ACTOR = "order_service"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SettlementOutcome:
    """What one settlement attempt produced.

    Not a contract type: it is the internal result the authority service folds
    into a :class:`~app.contracts.policy.ProposalOutcome`. At most one of
    ``receipt`` and ``failure`` is set, and ``reservation`` is always the state
    the reservation was left in.
    """

    reservation: Reservation
    receipt: PaymentReceipt | None = None
    failure: PaymentFailure | None = None

    @property
    def is_settled(self) -> bool:
        return self.receipt is not None


class _PayableFacts(NamedTuple):
    """What the final check read and found payable.

    Returned instead of a decision because there is nothing to decide by then:
    the check either refused (and returned a :class:`SettlementOutcome`) or
    established the facts the rest of the payment path needs. Passing them
    onward keeps the re-reads in one place instead of scattered through the
    settlement code.

    The five fields are annotated with the types they are, rather than with
    ``object``: this tuple *is* the contract between the check and the settlement
    that follows it, and a caller that had to guess what ``proposal`` holds would
    be reading the check's internals to find out.
    """

    reservation: Reservation
    proposal: PurchaseProposal
    mandate: Mandate
    quote: Quote
    route: PaymentRouteEvaluation


class PaymentService:
    """Turns an approved reservation into a receipt, or into a recorded refusal."""

    def __init__(self, *, db_path=None, audit: AuditLog | None = None,
                 adapter: PaymentAdapter | None = None,
                 capabilities: CapabilityService | None = None,
                 router: SandboxPaymentRouter | None = None) -> None:
        self._db_path = db_path
        self._audit = audit or AuditLog()
        self._adapter = adapter or SandboxPaymentAdapter()
        self._capabilities = capabilities or CapabilityService()
        self._router = router or SandboxPaymentRouter()

        self._reservations = ReservationRepository()
        self._proposals = ProposalRepository()
        self._mandates = MandateRepository()
        self._quotes = QuoteRepository()
        self._orders = OrderRepository()
        self._attempts = PaymentAttemptRepository()
        self._wallet = WalletRepository()

    @property
    def adapter_name(self) -> str:
        """Which rail settles purchases here. Reported on every outcome."""
        return self._adapter.name

    @property
    def source_type(self) -> SourceType:
        """How this configuration's settlements must be labelled."""
        return self._adapter.source_type

    # -- the flow -----------------------------------------------------------

    def pay_reservation(self, reservation_id: str, *,
                        now: datetime | None = None) -> SettlementOutcome:
        """Settle an approved reservation.

        Raises only when the caller named something that does not exist or is in
        no state to be paid at all. Every *business* refusal -- revoked mandate,
        no money, no stock, the rail saying no -- comes back as a recorded
        failure, because each is a handled outcome that a user is owed an
        explanation for rather than a broken request.

        The first thing that happens inside the transaction is
        :meth:`_final_check`. It is not an optimisation to skip and it is not a
        repeat of what ``submit_proposal`` decided: an approval is a statement
        about the facts as they were read a moment ago, and this is the last
        point at which C can still disagree. Everything it needs is re-read from
        C's own rows; nothing the agent, the recommender or the client claims is
        an input.
        """
        from app.errors import AgentError

        moment = as_utc(now or _utc())
        attempt_id = new_id("pay")

        with write_transaction(self._db_path) as conn:
            checked = self._final_check(
                conn, reservation_id=reservation_id, attempt_id=attempt_id,
                moment=moment,
            )
            if isinstance(checked, SettlementOutcome):
                return checked
            reservation, proposal, mandate, quote, route = checked
            order_id = new_id("ord")
            self._reservations.set_status(
                conn, reservation_id=reservation_id, status="PAYMENT_SUBMITTING")
            self._orders.add(
                conn, order_id=order_id, proposal_id=reservation.proposal_id,
                principal_id=reservation.principal_id, merchant_id=quote.merchant_id,
                product_id=quote.product_id, quantity=reservation.quantity,
                amount_cents=route.cash_total_cents, currency=quote.currency,
                status="CREATED", now=moment,
            )

            capability, token = self._capabilities.issue(
                conn,
                reservation_id=reservation_id,
                proposal_id=reservation.proposal_id,
                quote_hash=reservation.quote_hash,
                payment_route_id=route.route_id,
                cash_total_cents=route.cash_total_cents,
                currency=quote.currency,
                now=moment,
            )
            self._audit.append(
                conn, event_type=AuditEventType.CAPABILITY_ISSUED,
                actor_type=ActorType.COMMERCE, actor_id="capability_service",
                occurred_at=moment, mandate_id=reservation.mandate_id,
                proposal_id=reservation.proposal_id, reservation_id=reservation_id,
                payload={
                    "nonce": capability.nonce,
                    "cash_total_cents": capability.cash_total_cents,
                    "payment_route_id": capability.payment_route_id,
                    "expires_at": capability.expires_at.isoformat(),
                },
            )
            # Spent here, before anything outside C is called: a capability that
            # is still valid during the call it authorises can be presented
            # twice if the process dies mid-call.
            self._capabilities.spend(
                conn, token=token, payment_id=attempt_id, now=moment,
                reservation_id=reservation_id, proposal_id=reservation.proposal_id,
                quote_hash=reservation.quote_hash, payment_route_id=route.route_id,
                cash_total_cents=route.cash_total_cents,
            )
            self._audit.append(
                conn, event_type=AuditEventType.CAPABILITY_CONSUMED,
                actor_type=ActorType.COMMERCE, actor_id="capability_service",
                occurred_at=moment, mandate_id=reservation.mandate_id,
                proposal_id=reservation.proposal_id, reservation_id=reservation_id,
                transaction_id=attempt_id,
                payload={"nonce": capability.nonce, "attempt_id": attempt_id},
            )

            request = PaymentRequest(
                attempt_id=attempt_id,
                capability_token=token,
                reservation_id=reservation_id,
                proposal_id=reservation.proposal_id,
                order_id=order_id,
                principal_id=reservation.principal_id,
                merchant_id=quote.merchant_id,
                rail=route.rail,
                amount_cents=route.cash_total_cents,
                currency=quote.currency,
                idempotency_key=f"{reservation.proposal_id}:{attempt_id}",
            )

        # No lock is held here. A slow rail must not become a stalled database.
        result = self._adapter.charge(request)

        with write_transaction(self._db_path) as conn:
            if result.status == "SETTLED":
                return self._apply_settlement(
                    conn, reservation=reservation, attempt_id=attempt_id,
                    order_id=order_id, quote=quote, route=route,
                    provider_reference=result.provider_reference, moment=moment,
                )
            return self._apply_rail_refusal(
                conn, reservation=reservation, attempt_id=attempt_id, order_id=order_id,
                status=result.status, code=result.code, message=result.message or "",
                provider_reference=result.provider_reference, moment=moment,
                order_status=("PAYMENT_UNKNOWN" if result.status == "UNKNOWN"
                              else "PAYMENT_FAILED"),
            )

    # -- the final check ----------------------------------------------------

    def _final_check(self, conn, *, reservation_id: str, attempt_id: str,
                     moment: datetime) -> "_PayableFacts | SettlementOutcome":
        """Re-read C's own state and decide whether this may still be paid.

        Named, unconditional, and the first statement of the payment
        transaction, because its value is that it cannot be skipped. It answers
        one question -- *does the authorisation still cover this, right now?* --
        and it answers it from rows C owns:

        ===========================  ==========================================
        re-read here                 refusal if it says no
        ===========================  ==========================================
        the reservation              ``RESERVATION_NOT_FOUND`` (raise)
        its status                   ``RESERVATION_NOT_ACTIVE`` (raise)
        its deadline                 ``RESERVATION_NOT_ACTIVE`` (recorded, released)
        the proposal                 ``PROPOSAL_NOT_FOUND`` (raise)
        the **current** mandate      ``MANDATE_NOT_FOUND`` (raise)
        mandate status               ``MANDATE_REVOKED``
        mandate version              ``MANDATE_VERSION_STALE``
        mandate expiry               ``MANDATE_EXPIRED``
        the quote                    ``QUOTE_NOT_FOUND`` (raise)
        what the rail would charge   ``QUOTE_CHANGED`` if it moved
        the wallet balance           ``INSUFFICIENT_BALANCE``
        the product and its stock    ``OUT_OF_STOCK``
        ===========================  ==========================================

        Two of those are worth naming. **Expiry**: ``submit_proposal`` checks the
        window, but a mandate can lapse between approval and this call, and
        paying on a lapsed authorisation is the failure this whole layer exists
        to prevent. **The amount**: the re-priced total is compared with the
        amount the reservation holds, so "the number that was approved" and "the
        number that leaves the account" are the same number by assertion rather
        than by construction.

        No decision, no receipt and no capability is written before this returns
        facts. A refusal releases the hold and records why, so the budget goes
        back rather than being frozen by an attempt that is over.
        """
        from app.errors import AgentError

        reservation = self._reservations.get(conn, reservation_id)
        if reservation is None:
            raise AgentError(
                ErrorCode.RESERVATION_NOT_FOUND,
                f"no reservation {reservation_id!r}",
                details={"reservation_id": reservation_id},
            )
        if reservation.status != "ACTIVE":
            raise AgentError(
                ErrorCode.RESERVATION_NOT_ACTIVE,
                f"reservation {reservation_id} is {reservation.status}, not payable",
                details={"reservation_id": reservation_id, "status": reservation.status},
            )
        if as_utc(reservation.expires_at) <= moment:
            self._reservations.set_status(
                conn, reservation_id=reservation_id, status="EXPIRED")
            self._audit.append(
                conn, event_type=AuditEventType.RESERVATION_EXPIRED,
                actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
                reservation_id=reservation_id, proposal_id=reservation.proposal_id,
                mandate_id=reservation.mandate_id,
                payload={"expires_at": reservation.expires_at.isoformat()},
            )
            # A refusal, not a broken request: the reservation lapsed, which
            # is an ordinary thing for a hold to do. Reported as a failure so
            # A can explain it, and not retryable because the budget it held
            # is already gone -- continuing needs a fresh quote.
            return SettlementOutcome(
                reservation=self._reservations.get(conn, reservation_id) or reservation,
                failure=self._write_failure(
                    conn, reservation=reservation, attempt_id=attempt_id,
                    code=ErrorCode.RESERVATION_NOT_ACTIVE,
                    message=(
                        "the reservation expired before it was paid; the purchase "
                        "must be re-quoted"
                    ),
                    provider_reference=None, moment=moment,
                    attempt_status="FAILED", retryable=False,
                ),
            )

        proposal = self._proposals.get(conn, reservation.proposal_id)
        if proposal is None:  # pragma: no cover - a reservation implies a proposal
            raise AgentError(
                ErrorCode.PROPOSAL_NOT_FOUND,
                f"no proposal {reservation.proposal_id!r}",
                details={"proposal_id": reservation.proposal_id},
            )
        mandate = self._mandates.current(conn, reservation.mandate_id)
        if mandate is None:  # pragma: no cover - a reservation implies a mandate
            raise AgentError(
                ErrorCode.MANDATE_NOT_FOUND,
                f"no mandate {reservation.mandate_id!r}",
                details={"mandate_id": reservation.mandate_id},
            )
        quote = self._quotes.get(conn, reservation.quote_id)
        if quote is None:  # pragma: no cover - a reservation implies a quote
            raise AgentError(
                ErrorCode.QUOTE_NOT_FOUND,
                f"no quote {reservation.quote_id!r}",
                details={"quote_id": reservation.quote_id},
            )

        # The authorisation is re-read here rather than trusted from the
        # decision. This is the check that makes a revocation land on a
        # purchase approved a moment ago: the plan's rule is that a mandate
        # which moved before payment was submitted is withdrawn, releasing
        # the budget rather than spending it.
        if mandate.status == "REVOKED" or mandate.version != reservation.mandate_version:
            return self._refuse_before_the_rail(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=(ErrorCode.MANDATE_REVOKED if mandate.status == "REVOKED"
                      else ErrorCode.MANDATE_VERSION_STALE),
                message=(
                    "the mandate was revoked before payment was submitted"
                    if mandate.status == "REVOKED" else
                    "the mandate changed before payment was submitted"
                ),
                moment=moment,
            )
        # A mandate that lapsed between approval and settlement. The window was
        # checked when the proposal was decided; it is checked again here because
        # "it was valid a moment ago" is not "it is valid now".
        if moment >= as_utc(mandate.expires_at):
            return self._refuse_before_the_rail(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=ErrorCode.MANDATE_EXPIRED,
                message="the mandate expired before payment was submitted",
                moment=moment,
                detail={"expires_at": mandate.expires_at.isoformat(),
                        "now": moment.isoformat()},
            )

        balance = self._wallet.balance(conn, reservation.principal_id)
        if balance is None or balance < reservation.amount_cents:
            return self._refuse_before_the_rail(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=ErrorCode.INSUFFICIENT_BALANCE,
                message="the wallet does not cover this purchase",
                moment=moment,
                detail={"balance_cents": balance,
                        "required_cents": reservation.amount_cents},
            )

        from app.catalog import ProductRepository

        product = ProductRepository().get_product(quote.product_id, conn)
        if product is None or product.stock < reservation.quantity:
            return self._refuse_before_the_rail(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=ErrorCode.OUT_OF_STOCK,
                message="the product is no longer available in this quantity",
                moment=moment,
                detail={"stock": None if product is None else product.stock,
                        "requested": reservation.quantity},
            )

        route = self._router.choose(
            quote, mandate, list(proposal.preferred_payment_route_ids))
        # The amount is re-priced above and was fixed when the reservation was
        # created. They agree by construction today; asserting it turns a
        # coincidence into a check, so a future re-pricing rule that drifts is
        # refused here instead of quietly debiting a different number than the
        # one the user approved.
        if route.cash_total_cents != reservation.amount_cents:
            return self._refuse_before_the_rail(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=ErrorCode.QUOTE_CHANGED,
                message="the amount changed between approval and payment",
                moment=moment,
                detail={"approved_cents": reservation.amount_cents,
                        "repriced_cents": route.cash_total_cents},
            )
        return _PayableFacts(
            reservation=reservation, proposal=proposal, mandate=mandate,
            quote=quote, route=route,
        )


    # -- applying a rail answer ---------------------------------------------

    def _apply_settlement(self, conn, *, reservation: Reservation, attempt_id: str,
                          order_id: str, quote: Quote, route: PaymentRouteEvaluation,
                          provider_reference: str | None,
                          moment: datetime) -> SettlementOutcome:
        """Debit, decrement stock and write the receipt, in one transaction."""
        cash_total = route.cash_total_cents

        if not self._wallet.debit_if_sufficient(
            conn, principal_id=reservation.principal_id, amount_cents=cash_total,
            now=moment,
        ):
            return self._unreconciled(
                conn, reservation=reservation, attempt_id=attempt_id, order_id=order_id,
                message=(
                    "the rail reported settlement but the wallet no longer covered the "
                    "amount, so the ledger was not debited; reconcile before retrying"
                ),
                provider_reference=provider_reference, moment=moment,
            )

        from app.catalog import ProductRepository

        if not ProductRepository().decrease_stock(quote.product_id, reservation.quantity,
                                                  conn):
            self._wallet.credit(
                conn, principal_id=reservation.principal_id, amount_cents=cash_total,
                now=moment,
            )
            return self._unreconciled(
                conn, reservation=reservation, attempt_id=attempt_id, order_id=order_id,
                message=(
                    "the stock ran out while the rail was settling, so the ledger debit "
                    "was reversed; reconcile before retrying"
                ),
                provider_reference=provider_reference, moment=moment,
            )

        balance_after = self._wallet.balance(conn, reservation.principal_id) or 0
        self._orders.set_status(conn, order_id=order_id, status="PAID", now=moment)
        self._reservations.set_status(
            conn, reservation_id=reservation.reservation_id, status="SETTLED")

        event = self._audit.append(
            conn, event_type=AuditEventType.PAYMENT_SETTLED,
            actor_type=ActorType.COMMERCE, actor_id=_ORDER_ACTOR, occurred_at=moment,
            mandate_id=reservation.mandate_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id, transaction_id=attempt_id,
            payload={
                "cash_total_cents": cash_total,
                "currency": reservation.currency,
                "payment_route_id": route.route_id,
                "order_id": order_id,
                "balance_after_cents": balance_after,
                "adapter": self._adapter.name,
                "provider_reference": provider_reference,
                "source_type": SourceType.SANDBOX.value,
            },
        )

        receipt = PaymentReceipt(
            payment_id=attempt_id,
            reservation_id=reservation.reservation_id,
            order_id=order_id,
            principal_id=reservation.principal_id,
            mandate_id=reservation.mandate_id,
            mandate_version=reservation.mandate_version,
            quote_id=reservation.quote_id,
            quote_hash=reservation.quote_hash,
            payment_route_id=route.route_id,
            rail=route.rail,
            cash_total_cents=cash_total,
            fee_cents=route.fee_cents,
            fx_cost_cents=route.fx_cost_cents,
            merchant_total_cents=route.merchant_total_cents,
            currency=reservation.currency,
            balance_after_cents=balance_after,
            reservation_status="SETTLED",
            order_status="PAID",
            reward_earned_cents=0,
            settled_at=moment,
            audit_event_id=event.event_id,
        )
        self._attempts.record_settlement(
            conn, attempt_id=attempt_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id,
            provider_reference=provider_reference, attempted_at=moment, receipt=receipt,
        )
        return SettlementOutcome(
            reservation=self._reservations.get(conn, reservation.reservation_id)
            or reservation,
            receipt=receipt,
        )

    def _apply_rail_refusal(self, conn, *, reservation: Reservation, attempt_id: str,
                            order_id: str, status: str, code: ErrorCode | None,
                            message: str, provider_reference: str | None,
                            moment: datetime, order_status: str) -> SettlementOutcome:
        """Apply a rail refusal, keeping ``UNKNOWN`` distinct from ``FAILED``."""
        unknown = status == "UNKNOWN"
        reservation_status = "UNKNOWN" if unknown else "RELEASED"
        self._reservations.set_status(
            conn, reservation_id=reservation.reservation_id, status=reservation_status)
        self._orders.set_status(conn, order_id=order_id, status=order_status, now=moment)

        reason = code or ErrorCode.PAYMENT_FAILED
        event = self._audit.append(
            conn,
            event_type=(AuditEventType.PAYMENT_UNKNOWN if unknown
                        else AuditEventType.PAYMENT_FAILED),
            actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
            mandate_id=reservation.mandate_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id, transaction_id=attempt_id,
            payload={
                "code": reason.value,
                "message": message,
                "order_id": order_id,
                "adapter": self._adapter.name,
                "provider_reference": provider_reference,
                "reservation_status": reservation_status,
            },
        )
        if not unknown:
            self._audit.append(
                conn, event_type=AuditEventType.RESERVATION_RELEASED,
                actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
                mandate_id=reservation.mandate_id,
                proposal_id=reservation.proposal_id,
                reservation_id=reservation.reservation_id,
                payload={"reason": reason.value},
            )
        return SettlementOutcome(
            reservation=self._reservations.get(conn, reservation.reservation_id)
            or reservation,
            failure=self._write_failure(
                conn, reservation=reservation, attempt_id=attempt_id, code=reason,
                message=message, provider_reference=provider_reference, moment=moment,
                attempt_status="UNKNOWN" if unknown else "FAILED",
                audit_event_id=event.event_id,
            ),
        )

    # -- refusals decided before the rail is called -------------------------

    def _refuse_before_the_rail(self, conn, *, reservation: Reservation, attempt_id: str,
                                code: ErrorCode, message: str, moment: datetime,
                                detail: dict | None = None) -> SettlementOutcome:
        """Give the budget back without ever calling the rail.

        Reached when C's own re-checks refuse the purchase. The reservation is
        released rather than left holding budget: the attempt is over, and a
        hold that outlives its attempt is headroom nobody can use.
        """
        self._reservations.set_status(
            conn, reservation_id=reservation.reservation_id, status="RELEASED")
        event = self._audit.append(
            conn, event_type=AuditEventType.PAYMENT_FAILED,
            actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
            mandate_id=reservation.mandate_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id, transaction_id=attempt_id,
            payload={"code": code.value, "message": message, "stage": "preflight",
                     **(detail or {})},
        )
        self._audit.append(
            conn, event_type=AuditEventType.RESERVATION_RELEASED,
            actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
            mandate_id=reservation.mandate_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id,
            payload={"reason": code.value},
        )
        return SettlementOutcome(
            reservation=self._reservations.get(conn, reservation.reservation_id)
            or reservation,
            failure=self._write_failure(
                conn, reservation=reservation, attempt_id=attempt_id, code=code,
                message=message, provider_reference=None, moment=moment,
                attempt_status="FAILED", audit_event_id=event.event_id,
            ),
        )

    def _unreconciled(self, conn, *, reservation: Reservation, attempt_id: str,
                      order_id: str, message: str, provider_reference: str | None,
                      moment: datetime) -> SettlementOutcome:
        """The rail settled and C could not complete. Fail closed and say so.

        Not a failure and not a success: the money may have moved, so the
        reservation keeps holding budget, the outcome is ``UNKNOWN``, and the
        reconciliation reader reports it. Recording this as ``FAILED`` would
        invite an automatic retry against a payment that already happened.
        """
        self._reservations.set_status(
            conn, reservation_id=reservation.reservation_id, status="UNKNOWN")
        self._orders.set_status(
            conn, order_id=order_id, status="PAYMENT_UNKNOWN", now=moment)
        event = self._audit.append(
            conn, event_type=AuditEventType.PAYMENT_UNKNOWN,
            actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=moment,
            mandate_id=reservation.mandate_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id, transaction_id=attempt_id,
            payload={
                "code": ErrorCode.PAYMENT_STATUS_UNKNOWN.value,
                "message": message,
                "order_id": order_id,
                "adapter": self._adapter.name,
                "provider_reference": provider_reference,
                "reservation_status": "UNKNOWN",
            },
        )
        return SettlementOutcome(
            reservation=self._reservations.get(conn, reservation.reservation_id)
            or reservation,
            failure=self._write_failure(
                conn, reservation=reservation, attempt_id=attempt_id,
                code=ErrorCode.PAYMENT_STATUS_UNKNOWN, message=message,
                provider_reference=provider_reference, moment=moment,
                attempt_status="UNKNOWN", audit_event_id=event.event_id,
            ),
        )

    def _write_failure(self, conn, *, reservation: Reservation, attempt_id: str,
                       code: ErrorCode, message: str, provider_reference: str | None,
                       moment: datetime, attempt_status: str,
                       audit_event_id: str | None = None,
                       retryable: bool | None = None) -> PaymentFailure:
        """Record the attempt and build the contract object A will explain.

        ``retryable`` deliberately departs from the error table on one point.
        ``PAYMENT_STATUS_UNKNOWN`` is absent from ``NON_RETRYABLE``, so the
        shared helper calls it retryable -- the wrong answer for an attempt that
        may already have moved the money. The plan is explicit that an unknown
        outcome is never retried automatically, so the flag says so here while
        every other code keeps the shared table's answer. A caller that knows
        better for its own case -- an expired hold, say -- passes the flag
        explicitly rather than having a second rule inferred from the code.
        """
        if retryable is None:
            retryable = (is_retryable(code)
                         and code is not ErrorCode.PAYMENT_STATUS_UNKNOWN)
        self._attempts.record_failure(
            conn, attempt_id=attempt_id, proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id, status=attempt_status,
            code=code.value, message=message, retryable=retryable,
            provider_reference=provider_reference, attempted_at=moment,
        )
        return PaymentFailure(
            failure_id=attempt_id,
            proposal_id=reservation.proposal_id,
            reservation_id=reservation.reservation_id,
            code=code,
            message=message,
            retryable=retryable,
            provider_reference=provider_reference,
            failed_at=moment,
            audit_event_id=audit_event_id,
        )


__all__ = ["PaymentService", "SettlementOutcome"]
