"""The final check before money moves: what C re-reads, and what it refuses.

Every test here is about one question -- *may this still be paid, right now?* --
and about the two failures that question exists to catch: a stale or invalid
authorisation, and an amount that is no longer the one that was approved.

The properties these tests cover were **already held** by the payment path
before this round; they are pinned here because a property nobody asserts is a
property that quietly stops holding. The one behaviour added this round is the
mandate-expiry re-check and the amount assertion, and both are marked.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.commerce.database import read_connection  # noqa: E402
from app.commerce.repositories import (  # noqa: E402
    ReservationRepository,
    WalletRepository,
)
from app.contracts.common import ErrorCode  # noqa: E402
from app.contracts.policy import ProposalOutcome  # noqa: E402
from tests.commerce.conftest import (  # noqa: E402
    HP_APPROVE,
    NOW,
    a_proposal,
)


def outcome_of(commerce, proposal_id: str) -> ProposalOutcome:
    outcome = commerce.get_proposal_outcome(proposal_id)
    assert outcome is not None
    return outcome


def balance(db_path) -> int:
    with read_connection(db_path) as conn:
        return WalletRepository().balance(conn, "demo_user")


def a_paid_reservation(commerce, mandate, *, proposal_id: str = "prop_final"):
    """Approve and settle one purchase, and return the outcome."""
    quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
    result = commerce.submit_proposal(
        a_proposal(mandate, quote, proposal_id=proposal_id), now=NOW)
    assert result.outcome == "APPROVE", result
    return outcome_of(commerce, proposal_id)


class TestTheFinalCheckIsAlwaysRun:
    """It is the first statement of the payment transaction, not an option."""

    def test_the_payment_path_has_a_named_final_check(self):
        from app.commerce.payment import PaymentService

        assert hasattr(PaymentService, "_final_check"), (
            "the pre-payment re-check must be a named step, so that it cannot be "
            "skipped by editing a branch"
        )
        source = Path(PaymentService.__module__.replace(".", "/") + ".py")
        text = (Path(_REPO_ROOT) / source).read_text(encoding="utf-8")
        # It must be called unconditionally, before anything is written.
        call = text.index("checked = self._final_check(")
        set_status = text.index('status="PAYMENT_SUBMITTING"')
        assert call < set_status, "nothing may be written before the check returns"

    def test_a_paid_purchase_re_reads_rather_than_trusting_the_decision(self, commerce,
                                                                       mandate):
        outcome = a_paid_reservation(commerce, mandate)
        assert outcome.receipt is not None
        assert outcome.receipt.cash_total_cents == 28900, (
            "the amount is C's, re-derived from the catalog"
        )


class TestAStaleOrInvalidAuthorisationCannotBePaid:
    def test_a_revoked_mandate_is_refused_and_the_money_does_not_move(self, commerce,
                                                                     mandate, db_path):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        before = balance(db_path)
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)
        result = commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_revoked"), now=NOW)

        assert result.outcome == "DENY"
        assert result.primary_reason in (
            ErrorCode.MANDATE_REVOKED, ErrorCode.MANDATE_VERSION_STALE,
        )
        assert result.reservation_created is False
        assert result.payment_adapter_called is False
        assert balance(db_path) == before

    def test_a_mandate_that_expires_is_refused(self, commerce, mandate, db_path):
        """A window that lapsed before the purchase was submitted.

        The evaluator is the first gate and the payment path is the second; both
        refuse in the same fail-closed direction, and neither leaves a hold. The
        quote is deliberately issued before the mandate lapses, so the reason
        under test is the *authorisation's* expiry rather than the quote's.
        """
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        later = mandate.expires_at + timedelta(days=1)
        result = commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_expired"), now=later)

        assert result.outcome == "DENY"
        assert ErrorCode.MANDATE_EXPIRED in {v.code for v in result.violations}
        assert result.reservation_created is False
        assert result.payment_adapter_called is False
        assert balance(db_path) == 500000

    def test_a_version_mismatch_is_refused(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        result = commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_stale",
                       expected_mandate_version=mandate.version + 1),
            now=NOW)
        assert result.outcome == "DENY"
        assert result.primary_reason is ErrorCode.MANDATE_VERSION_STALE
        assert result.reservation_created is False


class TestAHardConstraintMismatchIsRefusedByC:
    """What a recommender claims is not an input. C reads the product itself."""

    def test_a_device_the_product_does_not_declare_is_refused(self, commerce):
        """**The device clause, end to end through the evaluator.**

        The catalog does not record supported devices yet (CC-10), so the honest
        result is a refusal rather than a purchase that assumed compatibility.
        """
        device_mandate = commerce.activate_mandate(
            commerce_draft_requiring("game_console"),
            principal_id="demo_user", agent_id="demo_agent", now=NOW)
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        result = commerce.submit_proposal(
            a_proposal(device_mandate, quote, proposal_id="prop_device"), now=NOW)

        assert result.outcome == "DENY"
        assert result.primary_reason is ErrorCode.DEVICE_REQUIREMENT_NOT_MET
        assert result.reservation_created is False
        assert result.payment_adapter_called is False
        violation = result.violations[0]
        assert violation.field == "required_device"
        assert violation.limit == "game_console"
        assert violation.observed is None, (
            "an unrecorded device list is reported as unknown, not as a value"
        )

    def test_the_same_purchase_without_the_device_clause_is_approved(self, commerce,
                                                                    mandate):
        """The refusal above is the clause doing the work, not the fixture."""
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        result = commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_no_device"), now=NOW)
        assert result.outcome == "APPROVE"


class TestTheAmountThatWasApprovedIsTheAmountThatIsCharged:
    def test_the_re_priced_total_must_equal_the_reservation(self, commerce, mandate,
                                                            db_path):
        """**Added this round**, closing a gap that held only by construction.

        The route is re-priced inside the payment transaction and the reservation
        holds the amount the decision approved. They agree today because both come
        from the same quote; the assertion turns that coincidence into a check, so
        a future re-pricing rule that drifts is refused instead of quietly
        debiting a different number.
        """
        outcome = a_paid_reservation(commerce, mandate, proposal_id="prop_amount")
        assert outcome.receipt is not None
        assert outcome.receipt.cash_total_cents == outcome.receipt.merchant_total_cents
        assert outcome.reservation.amount_cents == outcome.receipt.cash_total_cents


class TestNoReservationNoPaymentNoBalanceChange:
    def test_a_denial_writes_nothing_that_holds_budget(self, commerce, mandate, db_path):
        quote = commerce.create_quote(product_id="hp_0003", now=NOW)  # over the cap
        before = balance(db_path)
        result = commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_denied"), now=NOW)

        assert result.outcome == "DENY"
        assert result.primary_reason is ErrorCode.CAP_PER_TRANSACTION_EXCEEDED
        assert result.reservation_created is False
        assert result.payment_adapter_called is False

        outcome = outcome_of(commerce, "prop_denied")
        assert outcome.reservation is None
        assert outcome.receipt is None
        assert outcome.denial is not None, "a denial is a first-class outcome"
        assert outcome.denial.primary_reason is ErrorCode.CAP_PER_TRANSACTION_EXCEEDED
        assert balance(db_path) == before

        with read_connection(db_path) as conn:
            assert ReservationRepository().for_proposal(conn, "prop_denied") is None
            assert [r for r in ReservationRepository().not_terminal(conn)
                    if r.proposal_id == "prop_denied"] == []

    def test_a_denied_purchase_cannot_be_paid_by_naming_its_proposal(self, commerce,
                                                                    mandate):
        """There is no call that pays a proposal, and no reservation to pay."""
        quote = commerce.create_quote(product_id="hp_0003", now=NOW)
        commerce.submit_proposal(
            a_proposal(mandate, quote, proposal_id="prop_denied_2"), now=NOW)
        outcome = outcome_of(commerce, "prop_denied_2")
        assert outcome.reservation is None
        public = {n for n in dir(commerce) if not n.startswith("_")}
        # The exact surface A can reach. ``payment_routes`` names the rails and
        # ``settlement_source_type`` labels one; neither moves money. What must
        # not exist is a call that pays, reserves or issues anything -- the only
        # way money moves is inside ``submit_proposal``, and it returns a
        # decision rather than a receipt.
        assert public == {
            "account_overview",
            "activate_mandate", "adapter_name", "approve_escalation",
            "audit_events", "audit_events_for_proposal", "create_quote",
            "get_mandate", "get_proposal_outcome", "merchant_of_record",
            "payment_routes", "reject_escalation", "revoke_mandate",
            "settlement_source_type", "spend_state", "submit_proposal",
            "verify_audit_chain",
        }


class TestAnIdempotentRetryStaysIdempotent:
    def test_the_same_key_and_body_returns_the_recorded_result(self, commerce, mandate,
                                                              db_path):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        proposal = a_proposal(mandate, quote, proposal_id="prop_retry", attempt="7")
        first = commerce.submit_proposal(proposal, now=NOW)
        after_first = balance(db_path)
        second = commerce.submit_proposal(proposal, now=NOW)

        assert second.outcome == first.outcome
        assert second.proposal_id == first.proposal_id
        assert balance(db_path) == after_first, "a retry must not charge twice"

    def test_a_changed_purchase_under_the_same_key_is_a_conflict(self, commerce,
                                                                 mandate):
        """The same key with a different body is not a retry.

        Reusing a key for a different purchase used to be indistinguishable from
        a retry, and C refuses it rather than replaying the first result against
        the second order.
        """
        from app.errors import AgentError

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        first = a_proposal(mandate, quote, proposal_id="prop_a", attempt="8")
        commerce.submit_proposal(first, now=NOW)

        other_quote = commerce.create_quote(product_id="hp_0001", now=NOW)
        clash = a_proposal(mandate, other_quote, proposal_id="prop_a", attempt="8")
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(clash, now=NOW)
        assert caught.value.code in (
            ErrorCode.CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT,
        )


# ---------------------------------------------------------------------------
# Local helper
# ---------------------------------------------------------------------------

def commerce_draft_requiring(device: str):
    """A complete draft that additionally requires a device."""
    from tests.commerce.conftest import a_draft

    return a_draft(required_device=device)


__all__ = [
    "TestAHardConstraintMismatchIsRefusedByC",
    "TestAStaleOrInvalidAuthorisationCannotBePaid",
    "TestAnIdempotentRetryStaysIdempotent",
    "TestNoReservationNoPaymentNoBalanceChange",
    "TestTheAmountThatWasApprovedIsTheAmountThatIsCharged",
    "TestTheFinalCheckIsAlwaysRun",
]
