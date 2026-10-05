"""Settlement: the ledger, the receipt, and the three ways a rail can answer.

The distinctions that matter, and that a naive implementation collapses:

* ``UNKNOWN`` is not ``FAILED``. A timeout means the money may have moved, so the
  reservation keeps holding budget and the outcome says so;
* a refusal *before* the rail is called must not call the rail. The adapter
  records every request it was handed, so "it was never asked" is checkable;
* a settle that the rail confirms and C cannot complete is reported as unknown
  rather than as a failure, because the first is true and the second is
  convenient.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.commerce.adapters import PaymentResult
from app.commerce.adapters.sandbox import SandboxPaymentAdapter
from app.commerce.database import read_connection, write_transaction
from app.commerce.payment import PaymentService
from app.commerce.repositories import ReservationRepository, WalletRepository
from app.contracts.common import ErrorCode, SourceType
from app.errors import AgentError
from tests.commerce.conftest import (
    HP_APPROVE,
    HP_ESCALATE,
    HP_OUT_OF_STOCK,
    NOW,
    PRINCIPAL,
    a_proposal,
)


def reserve(commerce, mandate, product_id=HP_APPROVE, proposal_id="prop_pay"):
    """Get a reservation onto the books without settling it.

    The authority service settles on APPROVE, so a test that wants to drive the
    payment path itself runs the authority step with an adapter that never gets
    the chance -- by asking the authority layer directly. That keeps the
    reservation ACTIVE for the settlement under test.
    """
    from app.commerce.authority import AuthorityService

    quote = commerce.create_quote(product_id=product_id, now=NOW)
    proposal = a_proposal(mandate, quote, proposal_id=proposal_id)
    result = AuthorityService(db_path=commerce._db_path).submit(proposal, now=NOW)
    assert result.reservation is not None
    return quote, proposal, result.reservation


def balance(db_path) -> int:
    with read_connection(db_path) as conn:
        return WalletRepository().balance(conn, PRINCIPAL)


def stock(db_path, product_id: str) -> int:
    """Read the shelf through D's repository, against this test's database."""
    from app.catalog import ProductRepository

    return ProductRepository(db_path).get_product(product_id).stock


class TestASettledPurchase:
    def test_the_ledger_the_order_and_the_receipt_all_move_together(
        self, commerce, mandate, db_path
    ):
        _, _, reservation = reserve(commerce, mandate)
        before = balance(db_path)
        shelf = stock(db_path, HP_APPROVE)

        outcome = PaymentService(db_path=db_path).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.is_settled
        assert outcome.reservation.status == "SETTLED"
        assert balance(db_path) == before - 28900
        assert stock(db_path, HP_APPROVE) == shelf - 1

        receipt = outcome.receipt
        assert receipt.cash_total_cents == 28900
        assert receipt.balance_after_cents == before - 28900
        assert receipt.reservation_status == "SETTLED"
        assert receipt.order_status == "PAID"
        assert receipt.merchant_total_cents + receipt.fee_cents + receipt.fx_cost_cents == (
            receipt.cash_total_cents
        )
        assert receipt.audit_event_id

    def test_the_receipt_says_which_rail_produced_it(self, commerce, mandate, db_path):
        """A sandbox settlement must not be readable as observed behaviour."""
        outcome = _settle(commerce, mandate, db_path)
        assert outcome.receipt is not None
        assert PaymentService(db_path=db_path).source_type is SourceType.SANDBOX

    def test_the_settlement_is_recorded_in_the_chain(self, commerce, mandate, db_path):
        _settle(commerce, mandate, db_path)
        events = [e.event_type.value
                  for e in commerce.audit_events_for_proposal("prop_pay")]
        assert "PAYMENT_SETTLED" in events
        assert "RESERVATION_CREATED" in events

    def test_a_settled_reservation_cannot_be_paid_again(self, commerce, mandate,
                                                        db_path):
        outcome = _settle(commerce, mandate, db_path)
        with pytest.raises(AgentError) as caught:
            PaymentService(db_path=db_path).pay_reservation(
                outcome.reservation.reservation_id, now=NOW)
        assert caught.value.code == ErrorCode.RESERVATION_NOT_ACTIVE

    def test_an_unknown_reservation_is_a_named_error(self, db_path):
        with pytest.raises(AgentError) as caught:
            PaymentService(db_path=db_path).pay_reservation("res_nope", now=NOW)
        assert caught.value.code == ErrorCode.RESERVATION_NOT_FOUND


def _settle(commerce, mandate, db_path):
    _, _, reservation = reserve(commerce, mandate)
    return PaymentService(db_path=db_path).pay_reservation(
        reservation.reservation_id, now=NOW)


class TestRefusalsBeforeTheRail:
    """C's own re-checks. The rail must not be asked about any of these."""

    def test_an_emptied_wallet_is_refused(self, commerce, mandate, db_path):
        _, _, reservation = reserve(commerce, mandate)
        with write_transaction(db_path) as conn:
            conn.execute("UPDATE wallets SET balance_cents = 10 WHERE principal_id = ?",
                         (PRINCIPAL,))

        adapter = SandboxPaymentAdapter()
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.failure is not None
        assert outcome.failure.code is ErrorCode.INSUFFICIENT_BALANCE
        assert outcome.reservation.status == "RELEASED"
        assert adapter.calls == [], "the rail was never asked"

    def test_an_empty_shelf_is_refused(self, commerce, mandate, db_path):
        _, _, reservation = reserve(commerce, mandate)
        with write_transaction(db_path) as conn:
            conn.execute("UPDATE products SET stock = 0 WHERE product_id = ?",
                         (HP_APPROVE,))

        adapter = SandboxPaymentAdapter()
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.failure.code is ErrorCode.OUT_OF_STOCK
        assert outcome.reservation.status == "RELEASED"
        assert adapter.calls == []

    def test_a_revocation_landing_after_approval_withdraws_the_purchase(
        self, commerce, mandate, db_path
    ):
        """The plan's revocation rule, exercised end to end.

        Approved and not yet submitted: the version moved, so the reservation is
        released rather than spent. This is the case a wallet debited before the
        re-check would get wrong.
        """
        _, _, reservation = reserve(commerce, mandate)
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)

        adapter = SandboxPaymentAdapter()
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.failure.code in (
            ErrorCode.MANDATE_REVOKED, ErrorCode.MANDATE_VERSION_STALE,
        )
        assert outcome.reservation.status == "RELEASED"
        assert adapter.calls == []
        assert balance(db_path) == 500000

    def test_an_expired_reservation_is_refused(self, commerce, mandate, db_path):
        _, _, reservation = reserve(commerce, mandate)
        late = NOW + timedelta(seconds=901)

        adapter = SandboxPaymentAdapter()
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=late)

        assert outcome.failure.code is ErrorCode.RESERVATION_NOT_ACTIVE
        assert outcome.failure.retryable is False
        assert outcome.reservation.status == "EXPIRED"
        assert adapter.calls == []


class TestTheRailSaysNo:
    def test_a_refusal_releases_the_hold(self, commerce, mandate, db_path):
        _, _, reservation = reserve(commerce, mandate)
        adapter = SandboxPaymentAdapter(outcome=PaymentResult(
            status="FAILED", code=ErrorCode.PAYMENT_FAILED, message="declined"))
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.failure.code is ErrorCode.PAYMENT_FAILED
        assert outcome.reservation.status == "RELEASED"
        assert balance(db_path) == 500000
        assert stock(db_path, HP_APPROVE) == 7, "nothing was taken from the shelf"

        events = [e.event_type.value
                  for e in commerce.audit_events_for_proposal("prop_pay")]
        assert "PAYMENT_FAILED" in events
        assert "RESERVATION_RELEASED" in events

    def test_an_unknown_outcome_keeps_holding_budget(self, commerce, mandate, db_path):
        """The distinction the plan is explicit about.

        A timeout means the money may have moved. Releasing the hold would hand
        the same budget out again while the first payment might still settle, and
        retrying automatically is how a customer is charged twice.
        """
        _, _, reservation = reserve(commerce, mandate)
        adapter = SandboxPaymentAdapter(outcome=PaymentResult(
            status="UNKNOWN", code=ErrorCode.PAYMENT_STATUS_UNKNOWN,
            message="the rail did not answer"))
        outcome = PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        assert outcome.failure.code is ErrorCode.PAYMENT_STATUS_UNKNOWN
        assert outcome.reservation.status == "UNKNOWN"
        assert outcome.reservation.holds_budget is True
        assert outcome.failure.retryable is False, (
            "an unknown outcome must never be retried automatically: the first "
            "attempt may already have moved the money"
        )
        assert balance(db_path) == 500000

        from app.commerce.reconcile import reconcile

        report = reconcile(db_path, now=NOW)
        assert [r.reservation_id for r in report.unaccounted_holds] == [
            reservation.reservation_id
        ]
        assert report.is_clean is False

    def test_each_attempt_is_recorded_rather_than_forgotten(self, commerce, mandate,
                                                            db_path):
        """A system that keeps only its successes cannot show it ever stopped."""
        _, _, reservation = reserve(commerce, mandate)
        adapter = SandboxPaymentAdapter(outcome=PaymentResult(
            status="FAILED", code=ErrorCode.PAYMENT_FAILED, message="declined"))
        PaymentService(db_path=db_path, adapter=adapter).pay_reservation(
            reservation.reservation_id, now=NOW)

        from app.commerce.database import read_connection as read

        with read(db_path) as conn:
            rows = conn.execute(
                "SELECT status, code FROM payment_attempts WHERE proposal_id = ?",
                ("prop_pay",)).fetchall()
        assert [(r["status"], r["code"]) for r in rows] == [
            ("FAILED", ErrorCode.PAYMENT_FAILED.value)
        ]


class TestTheRailSettlesAndCcannot:
    """The case the ordering exists for, reached with a hostile adapter.

    A real rail can take the last item, or the balance can move, while the
    payment is in flight. The adapter here mutates the database from a second
    connection before answering, which is exactly what another buyer would do.
    """

    class _DrainsTheWallet:
        name = "sandbox"
        source_type = SourceType.SANDBOX

        def __init__(self, db_path):
            self._db_path = db_path
            self.calls = []

        def charge(self, request):
            self.calls.append(request)
            with write_transaction(self._db_path) as conn:
                conn.execute(
                    "UPDATE wallets SET balance_cents = 0 WHERE principal_id = ?",
                    (request.principal_id,))
            return PaymentResult(status="SETTLED", provider_reference="sandbox_x")

    class _EmptiesTheShelf:
        name = "sandbox"
        source_type = SourceType.SANDBOX

        def __init__(self, db_path, product_id):
            self._db_path = db_path
            self._product_id = product_id
            self.calls = []

        def charge(self, request):
            self.calls.append(request)
            with write_transaction(self._db_path) as conn:
                conn.execute("UPDATE products SET stock = 0 WHERE product_id = ?",
                             (self._product_id,))
            return PaymentResult(status="SETTLED", provider_reference="sandbox_x")

    def test_a_vanished_balance_is_reported_as_unknown_not_as_failure(
        self, commerce, mandate, db_path
    ):
        _, _, reservation = reserve(commerce, mandate)
        outcome = PaymentService(
            db_path=db_path, adapter=self._DrainsTheWallet(db_path)
        ).pay_reservation(reservation.reservation_id, now=NOW)

        assert outcome.receipt is None
        assert outcome.failure.code is ErrorCode.PAYMENT_STATUS_UNKNOWN
        assert outcome.reservation.status == "UNKNOWN"
        assert "reconcile" in outcome.failure.message

    def test_a_vanished_shelf_reverses_the_debit_and_says_so(
        self, commerce, mandate, db_path
    ):
        _, _, reservation = reserve(commerce, mandate)
        outcome = PaymentService(
            db_path=db_path, adapter=self._EmptiesTheShelf(db_path, HP_APPROVE)
        ).pay_reservation(reservation.reservation_id, now=NOW)

        assert outcome.failure.code is ErrorCode.PAYMENT_STATUS_UNKNOWN
        assert "reversed" in outcome.failure.message
        assert balance(db_path) == 500000, (
            "the debit is reversed because the rail cannot be un-asked; leaving the "
            "money taken and the goods unsent would be worse than either"
        )


class TestToppingUpAndEscalation:
    def test_a_purchase_that_needs_no_escalation_never_asks(
        self, commerce, mandate
    ):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        decision = commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)
        assert decision.outcome == "APPROVE"
        assert commerce.get_proposal_outcome("prop_0001").escalation is None

    def test_an_out_of_stock_product_never_becomes_a_proposal(self, commerce, mandate):
        with pytest.raises(AgentError) as caught:
            commerce.create_quote(product_id=HP_OUT_OF_STOCK, now=NOW)
        assert caught.value.code == ErrorCode.OUT_OF_STOCK

    def test_an_escalated_purchase_pays_only_after_the_answer(
        self, commerce, roomy_mandate, db_path
    ):
        quote = commerce.create_quote(product_id=HP_ESCALATE, now=NOW)
        proposal = a_proposal(roomy_mandate, quote, proposal_id="prop_esc")
        assert commerce.submit_proposal(proposal, now=NOW).outcome == "ESCALATE"
        assert balance(db_path) == 500000

        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)
        retry = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"})
        assert commerce.submit_proposal(retry, now=NOW).outcome == "APPROVE"
        assert balance(db_path) == 500000 - 31000


class TestReservationBookkeeping:
    def test_the_reservation_names_the_priced_facts(self, commerce, mandate):
        quote, _, reservation = reserve(commerce, mandate)
        assert reservation.quote_hash == quote.quote_hash
        assert reservation.amount_cents == quote.merchant_total_cents
        assert reservation.mandate_version == mandate.version
        assert reservation.status == "ACTIVE"

    def test_a_released_reservation_stops_holding_budget(self, commerce, mandate,
                                                         db_path):
        _, _, reservation = reserve(commerce, mandate)
        PaymentService(db_path=db_path, adapter=SandboxPaymentAdapter(
            outcome=PaymentResult(status="FAILED", code=ErrorCode.PAYMENT_FAILED,
                                  message="declined"),
        )).pay_reservation(reservation.reservation_id, now=NOW)

        from app.commerce.database import read_connection as read

        with read(db_path) as conn:
            stored = ReservationRepository().get(conn, reservation.reservation_id)
        assert stored.holds_budget is False
        assert commerce.spend_state(mandate, now=NOW).exposure_cents == 0
