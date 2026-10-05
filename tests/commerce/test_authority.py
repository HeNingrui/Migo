"""The authority boundary: one proposal in, a decision and its consequences out.

What is being tested is not that the evaluator returns the right word -- that is
covered exhaustively in ``test_policy_evaluator.py``. It is that the right word
*does the right thing*: that a denial leaves no hold on the budget, that an
approval leaves one, and that submitting the same purchase twice cannot produce
two charges.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.contracts.common import ErrorCode
from app.errors import AgentError
from tests.commerce.conftest import (
    ADDRESS,
    HP_APPROVE,
    HP_ESCALATE,
    HP_OVER_CAP,
    HP_OUT_OF_STOCK,
    NOW,
    PRINCIPAL,
    a_proposal,
)


def submit(commerce, mandate, product_id: str, proposal_id: str = "prop_0001",
           attempt: str = "1", now=NOW, **overrides):
    quote = commerce.create_quote(product_id=product_id, now=now)
    proposal = a_proposal(mandate, quote, proposal_id=proposal_id, attempt=attempt,
                          **overrides)
    decision = commerce.submit_proposal(proposal, now=now)
    return quote, proposal, decision


class TestApprove:
    def test_an_approval_reserves_and_settles(self, commerce, mandate):
        _, _, decision = submit(commerce, mandate, HP_APPROVE)
        assert decision.outcome == "APPROVE"
        assert decision.reservation_created is True
        assert decision.cash_total_cents == 28900

        outcome = commerce.get_proposal_outcome("prop_0001")
        assert outcome.reservation.status == "SETTLED"
        assert outcome.receipt is not None

    def test_the_payment_rail_is_only_reached_through_an_approval(
        self, commerce, mandate
    ):
        """A denied purchase never reaches the money.

        The decision carries the flag for exactly this reason: it makes the
        claim checkable without trusting the prose.
        """
        _, _, decision = submit(commerce, mandate, HP_OVER_CAP)
        assert decision.outcome == "DENY"
        assert decision.payment_adapter_called is False
        assert decision.reservation_created is False


class TestDeny:
    def test_a_denial_records_a_receipt_and_holds_nothing(self, commerce, mandate):
        _, _, decision = submit(commerce, mandate, HP_OVER_CAP)
        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.CAP_PER_TRANSACTION_EXCEEDED

        outcome = commerce.get_proposal_outcome("prop_0001")
        assert outcome.denial is not None
        assert outcome.denial.cash_total_cents == 50900
        assert outcome.denial.reservation_created is False
        assert outcome.reservation is None
        assert commerce.spend_state(mandate, now=NOW).exposure_cents == 0

    def test_every_violation_is_kept_and_one_is_named(self, commerce, mandate):
        """The user gets an actionable reason; the audit keeps the rest."""
        _, _, decision = submit(commerce, mandate, HP_OVER_CAP,
                                shipping_address_id="addr_somewhere_else")
        assert decision.primary_reason is not None
        codes = {v.code for v in decision.violations}
        assert ErrorCode.CAP_PER_TRANSACTION_EXCEEDED in codes
        assert ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED in codes

    def test_the_denial_names_the_measured_value_and_the_recorded_limit(
        self, commerce, mandate
    ):
        _, _, decision = submit(commerce, mandate, HP_OVER_CAP)
        assert decision.observed_values["cash_total_cents"] == 50900
        assert decision.applicable_limits["cap_per_transaction_cents"] == 32000
        violation = next(v for v in decision.violations
                         if v.code is ErrorCode.CAP_PER_TRANSACTION_EXCEEDED)
        assert violation.observed == 50900
        assert violation.limit == 32000
        assert "50900" in violation.describe()


class TestEscalate:
    def test_an_escalation_asks_and_holds_nothing(self, commerce, mandate):
        _, _, decision = submit(commerce, mandate, HP_ESCALATE)
        assert decision.outcome == "ESCALATE"
        assert decision.reservation_created is False

        outcome = commerce.get_proposal_outcome("prop_0001")
        assert outcome.escalation is not None
        assert outcome.escalation.resolution is None
        assert outcome.escalation.cash_total_cents == 31000
        assert outcome.escalation.escalate_above_cents == 30000
        assert outcome.reservation is None
        assert commerce.spend_state(mandate, now=NOW).exposure_cents == 0


class TestMandateState:
    def test_a_stale_version_is_refused(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        stale = a_proposal(mandate, quote, expected_mandate_version=mandate.version)
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)

        decision = commerce.submit_proposal(stale, now=NOW)
        assert decision.outcome == "DENY"
        assert decision.primary_reason in (
            ErrorCode.MANDATE_REVOKED, ErrorCode.MANDATE_VERSION_STALE,
        )

    def test_a_proposal_written_before_a_revocation_lands_on_the_revocation(
        self, commerce, mandate
    ):
        """The check that makes revocation mean something.

        An agent that read the mandate a moment ago must not be able to spend
        against it after the principal withdrew it, and the mechanism is the
        version number rather than a re-read A is trusted to perform.
        """
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        proposal = a_proposal(mandate, quote)
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)

        decision = commerce.submit_proposal(proposal, now=NOW)
        assert decision.outcome == "DENY"
        assert commerce.get_proposal_outcome("prop_0001").reservation is None

    def test_an_expired_mandate_is_refused(self, commerce, mandate):
        after = NOW + timedelta(seconds=604800)
        quote = commerce.create_quote(product_id=HP_APPROVE, now=after)
        proposal = a_proposal(mandate, quote, created_at=after)
        decision = commerce.submit_proposal(proposal, now=after)
        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.MANDATE_EXPIRED

    def test_an_unknown_mandate_is_a_named_error(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        proposal = a_proposal(mandate, quote, mandate_id="man_nope")
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(proposal, now=NOW)
        assert caught.value.code == ErrorCode.MANDATE_NOT_FOUND

    def test_an_unknown_quote_is_a_named_error(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        proposal = a_proposal(mandate, quote, quote_id="q_nope")
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(proposal, now=NOW)
        assert caught.value.code == ErrorCode.QUOTE_NOT_FOUND

    def test_a_different_principal_is_refused(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        proposal = a_proposal(mandate, quote, principal_id="someone_else")
        decision = commerce.submit_proposal(proposal, now=NOW)
        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.PRINCIPAL_MISMATCH

    def test_an_out_of_stock_product_cannot_even_be_priced(self, commerce, mandate):
        with pytest.raises(AgentError) as caught:
            commerce.create_quote(product_id=HP_OUT_OF_STOCK, now=NOW)
        assert caught.value.code == ErrorCode.OUT_OF_STOCK


class TestIdempotency:
    def test_a_replay_returns_the_recorded_decision_and_changes_nothing(
        self, commerce, mandate
    ):
        """A retried request must not become a second purchase.

        The replay is recognised by the key *and* the content digest: returning
        the first result for a different purchase would report a decision about
        something else.
        """
        _, proposal, first = submit(commerce, mandate, HP_APPROVE)
        before = len(commerce.audit_events(mandate.mandate_id))

        second = commerce.submit_proposal(proposal, now=NOW)
        assert second == first
        assert len(commerce.audit_events(mandate.mandate_id)) == before, (
            "a replay must write nothing, or the chain stops being evidence of "
            "what happened once"
        )
        assert commerce.get_proposal_outcome("prop_0001").reservation.status == "SETTLED"

    def test_the_wallet_is_debited_once(self, commerce, mandate):
        from app.commerce.database import read_connection
        from app.commerce.repositories import WalletRepository

        _, proposal, _ = submit(commerce, mandate, HP_APPROVE)
        commerce.submit_proposal(proposal, now=NOW)

        with read_connection(commerce._db_path) as conn:
            assert WalletRepository().balance(conn, PRINCIPAL) == 500000 - 28900

    def test_a_key_reused_for_a_different_purchase_is_a_conflict(
        self, commerce, mandate
    ):
        _, proposal, _ = submit(commerce, mandate, HP_APPROVE)
        other = commerce.create_quote(product_id=HP_ESCALATE, now=NOW)
        collided = a_proposal(mandate, other, proposal_id="prop_0002")
        collided = collided.model_copy(update={
            "idempotency_key": proposal.idempotency_key,
        })
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(collided, now=NOW)
        assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT

    def test_a_proposal_cannot_change_what_it_asks_for(self, commerce, mandate):
        """The same proposal id may be submitted again; it may not become other.

        Re-submission is how an answered escalation is continued, so it has to
        be allowed. Changing the product, the quantity or the quote under an
        existing identifier is a different thing and is refused.
        """
        _, proposal, _ = submit(commerce, mandate, HP_OVER_CAP)
        mutated = proposal.model_copy(update={"quantity": 2, "idempotency_key": "idem_x"})
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(mutated, now=NOW)
        assert caught.value.code == ErrorCode.CONFLICT

    def test_a_proposal_gets_at_most_one_reservation(self, commerce, mandate):
        """A released attempt is not re-usable.

        If the reservation could be recreated, a payment refusal followed by a
        retry loop would be a way to charge twice for one purchase.
        """
        from app.commerce.database import read_connection
        from app.commerce.repositories import ReservationRepository, WalletRepository

        _, proposal, _ = submit(commerce, mandate, HP_APPROVE)
        first = commerce.get_proposal_outcome("prop_0001").reservation

        again = proposal.model_copy(update={"idempotency_key": "idem_again"})
        commerce.submit_proposal(again, now=NOW)

        second = commerce.get_proposal_outcome("prop_0001").reservation
        assert second.reservation_id == first.reservation_id
        with read_connection(commerce._db_path) as conn:
            assert ReservationRepository().for_proposal(conn, "prop_0001") is not None
            assert WalletRepository().balance(conn, PRINCIPAL) == 500000 - 28900, (
                "a second submission of the same proposal must not charge again"
            )


class TestAddressAndQuantity:
    def test_a_shipping_address_change_is_refused(self, commerce, mandate):
        _, _, decision = submit(commerce, mandate, HP_APPROVE,
                                shipping_address_id="addr_elsewhere")
        assert decision.outcome == "DENY"
        assert ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED in {
            v.code for v in decision.violations
        }

    def test_the_landed_address_is_accepted(self, commerce, mandate):
        _, _, decision = submit(commerce, mandate, HP_APPROVE,
                                shipping_address_id=ADDRESS)
        assert decision.outcome == "APPROVE"
