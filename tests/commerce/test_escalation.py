"""Escalation, and the single-use approval it produces.

The claim under test is narrow and load-bearing: **the principal is asked once,
and the answer is good for one purchase.** A grant that could be spent twice
would turn "ask me above HK$300" into "ask me once, ever", which is worse than
not asking at all -- it looks like a control while behaving like a formality.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.commerce.config import GRANT_TTL_SECONDS
from app.commerce.database import read_connection, write_transaction
from app.commerce.repositories import GrantRepository, WalletRepository
from app.contracts.common import ErrorCode
from app.errors import AgentError
from tests.commerce.conftest import HP_APPROVE, HP_ESCALATE, NOW, PRINCIPAL, a_proposal


def escalate(commerce, mandate, proposal_id: str = "prop_esc"):
    quote = commerce.create_quote(product_id=HP_ESCALATE, now=NOW)
    proposal = a_proposal(mandate, quote, proposal_id=proposal_id)
    decision = commerce.submit_proposal(proposal, now=NOW)
    assert decision.outcome == "ESCALATE"
    return quote, proposal, decision


class TestRaising:
    def test_one_open_question_per_proposal(self, commerce, roomy_mandate):
        _, proposal, _ = escalate(commerce, roomy_mandate)
        again = commerce.submit_proposal(
            proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"}), now=NOW)
        assert again.outcome == "ESCALATE"
        events = [e for e in commerce.audit_events(roomy_mandate.mandate_id)
                  if e.event_type.value == "ESCALATION_RAISED"]
        assert len(events) == 1, (
            "a re-submission that escalates again must return the question already "
            "on file, not try to write a second row for the same proposal"
        )

    def test_the_question_states_no_number(self, commerce, roomy_mandate):
        """C writes the question because the contract makes it a field on C's
        object; the amounts are rendered by A from the fields beside it, so the
        wording here carries no formatting decision and no figure to drift."""
        _, _, _ = escalate(commerce, roomy_mandate)
        escalation = commerce.get_proposal_outcome("prop_esc").escalation
        assert "HK$" not in escalation.question
        assert escalation.cash_total_cents == 31000


class TestAnswering:
    def test_approving_issues_a_grant_bound_to_the_purchase(self, commerce, roomy_mandate):
        quote, proposal, decision = escalate(commerce, roomy_mandate)
        grant = commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)

        assert grant.proposal_id == "prop_esc"
        assert grant.quote_id == quote.quote_id
        assert grant.quote_hash == quote.quote_hash
        assert grant.cash_total_cents == decision.cash_total_cents
        assert grant.payment_route_id == decision.payment_route_id
        assert grant.expires_at - grant.issued_at == timedelta(seconds=GRANT_TTL_SECONDS)

    def test_rejecting_records_the_answer_and_issues_nothing(self, commerce, roomy_mandate):
        escalate(commerce, roomy_mandate)
        escalation = commerce.reject_escalation("prop_esc", principal_id=PRINCIPAL,
                                                now=NOW)
        assert escalation.resolution == "REJECTED"

        with read_connection(commerce._db_path) as conn:
            assert GrantRepository().usable_for(conn, "prop_esc") is None
        outcome = commerce.get_proposal_outcome("prop_esc")
        assert outcome.reservation is None
        assert outcome.receipt is None

    def test_a_second_answer_is_refused(self, commerce, roomy_mandate):
        escalate(commerce, roomy_mandate)
        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)
        with pytest.raises(AgentError) as caught:
            commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)
        assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION

    def test_a_different_principal_cannot_answer(self, commerce, roomy_mandate):
        escalate(commerce, roomy_mandate)
        with pytest.raises(AgentError) as caught:
            commerce.approve_escalation("prop_esc", principal_id="someone_else", now=NOW)
        assert caught.value.code == ErrorCode.MANDATE_OWNER_MISMATCH

    def test_answering_an_unknown_proposal_is_a_named_error(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.approve_escalation("prop_nope", principal_id=PRINCIPAL, now=NOW)
        assert caught.value.code == ErrorCode.PROPOSAL_NOT_FOUND

    def test_answering_a_proposal_that_never_escalated_is_refused(self, commerce,
                                                                  mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)
        with pytest.raises(AgentError) as caught:
            commerce.approve_escalation("prop_0001", principal_id=PRINCIPAL, now=NOW)
        assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION


class TestTheApprovalIsSpentOnce:
    def test_the_approved_purchase_goes_through(self, commerce, roomy_mandate):
        _, proposal, _ = escalate(commerce, roomy_mandate)
        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)

        retry = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"})
        decision = commerce.submit_proposal(retry, now=NOW)

        assert decision.outcome == "APPROVE"
        outcome = commerce.get_proposal_outcome("prop_esc")
        assert outcome.receipt is not None
        assert outcome.receipt.cash_total_cents == 31000
        assert outcome.reservation.status == "SETTLED"

    def test_the_grant_is_consumed(self, commerce, roomy_mandate):
        _, proposal, _ = escalate(commerce, roomy_mandate)
        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)
        assert commerce.get_proposal_outcome("prop_esc").escalation.resolution == "APPROVED"

        retry = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"})
        commerce.submit_proposal(retry, now=NOW)

        with read_connection(commerce._db_path) as conn:
            assert GrantRepository().usable_for(conn, "prop_esc") is None, (
                "an approval that survives its purchase is a token somebody can "
                "present later"
            )

    def test_using_the_approval_a_second_time_is_refused(self, commerce, roomy_mandate):
        """First use succeeds, second use is refused, and nothing is charged twice.

        The refusal is explicit rather than incidental: with the grant spent, the
        evaluator asks for the principal again, and C knows the question has
        already been answered.
        """
        _, proposal, _ = escalate(commerce, roomy_mandate)
        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)

        first = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"})
        assert commerce.submit_proposal(first, now=NOW).outcome == "APPROVE"
        settled = commerce.get_proposal_outcome("prop_esc").receipt.payment_id

        second = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_3"})
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(second, now=NOW)
        assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION

        outcome = commerce.get_proposal_outcome("prop_esc")
        assert outcome.receipt.payment_id == settled
        with read_connection(commerce._db_path) as conn:
            assert WalletRepository().balance(conn, PRINCIPAL) == 500000 - 31000

    def test_consuming_a_grant_twice_at_the_repository_is_impossible(self, db_path):
        """The mechanism, tested where it lives.

        Single use is a conditional UPDATE and a rowcount, not a convention, so
        it holds regardless of the order in which callers happen to run.
        """
        from app.commerce.mandate_registry import MandateRegistry
        from app.contracts.mandate import ApprovalGrant
        from tests.commerce.conftest import AGENT, a_draft

        mandate = MandateRegistry(db_path=db_path).activate(
            a_draft(), principal_id=PRINCIPAL, agent_id=AGENT, now=NOW)
        grant = ApprovalGrant(
            grant_id="grant_x", proposal_id="prop_x", principal_id=PRINCIPAL,
            mandate_id=mandate.mandate_id, mandate_version=mandate.version,
            quote_id="q_x", quote_hash="sha256:x", payment_route_id="fps_demo",
            cash_total_cents=100, currency="HKD", issued_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        )
        repository = GrantRepository()
        with write_transaction(db_path) as conn:
            repository.add(conn, grant)
            assert repository.consume(conn, proposal_id="prop_x",
                                      consumed_at=NOW) is not None
        with write_transaction(db_path) as conn:
            assert repository.consume(conn, proposal_id="prop_x",
                                      consumed_at=NOW) is None

    def test_an_expired_grant_authorises_nothing(self, commerce, roomy_mandate):
        """A lapsed approval is not an approval.

        With the grant expired the evaluator asks for the principal again, and C
        cannot ask: the question has already been answered. The purchase is
        refused outright rather than quietly re-escalated, which is the
        fail-closed direction -- an approval that outlives its window is a
        permission nobody is watching any more.
        """
        _, proposal, _ = escalate(commerce, roomy_mandate)
        commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)

        late = NOW + timedelta(seconds=GRANT_TTL_SECONDS + 1)
        retry = proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2"})
        with pytest.raises(AgentError) as caught:
            commerce.submit_proposal(retry, now=late)
        assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION

        outcome = commerce.get_proposal_outcome("prop_esc")
        assert outcome.receipt is None
        assert outcome.reservation is None
        with read_connection(commerce._db_path) as conn:
            assert WalletRepository().balance(conn, PRINCIPAL) == 500000


class TestTimeouts:
    def test_an_unanswered_question_becomes_a_refusal(self, commerce, roomy_mandate):
        """DC12, fail closed.

        "The agent asked and nobody answered" must never decay into "the agent
        went ahead", so the escalation is closed as a timeout and the purchase is
        denied with a reason that says so.
        """
        _, proposal, _ = escalate(commerce, roomy_mandate)
        late = NOW + timedelta(seconds=301)

        retry = proposal.model_copy(update={
            "idempotency_key": "idem_prop_esc_2",
            "created_at": late,
        })
        decision = commerce.submit_proposal(retry, now=late)

        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.ESCALATION_TIMEOUT
        outcome = commerce.get_proposal_outcome("prop_esc")
        assert outcome.escalation.resolution == "TIMEOUT"
        assert outcome.reservation is None

    def test_the_original_violation_is_kept_beside_the_timeout(self, commerce, roomy_mandate):
        """A reader should still see which threshold was crossed."""
        _, proposal, _ = escalate(commerce, roomy_mandate)
        late = NOW + timedelta(seconds=301)
        decision = commerce.submit_proposal(
            proposal.model_copy(update={"idempotency_key": "idem_prop_esc_2",
                                        "created_at": late}),
            now=late,
        )
        codes = {v.code for v in decision.violations}
        assert ErrorCode.ESCALATION_TIMEOUT in codes
        assert ErrorCode.ESCALATION_REQUIRED in codes

    def test_answering_after_the_deadline_is_refused(self, commerce, roomy_mandate):
        escalate(commerce, roomy_mandate)
        late = NOW + timedelta(seconds=301)
        with pytest.raises(AgentError) as caught:
            commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=late)
        assert caught.value.code == ErrorCode.ESCALATION_TIMEOUT
        assert commerce.get_proposal_outcome("prop_esc").escalation.resolution == "TIMEOUT"
