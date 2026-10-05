"""The A -> C boundary itself: the interface, the freeze, and the read model.

``CommerceClient`` is the promise that replacing C does not rewrite A. These
tests hold that promise to something checkable: the seven original method
signatures, the additions and the narrowing, and the rule that nothing A is
handed can be re-submitted as authority.
"""

from __future__ import annotations

import inspect

import pytest

from app.agent.clients import CommerceClient
from app.commerce.reconcile import reconcile
from app.commerce.service import CommerceService
from app.contracts.common import ErrorCode, SourceType
from app.errors import AgentError
from tests.commerce.conftest import (
    HP_APPROVE,
    HP_ESCALATE,
    HP_OVER_CAP,
    NOW,
    PRINCIPAL,
    a_proposal,
)

#: The interface as it stood before this work, recorded so a later change has to
#: be deliberate. Parameters are compared by name and kind, because a rename is
#: a break for a keyword caller even when the call still works positionally.
FROZEN_SIGNATURES: dict[str, list[tuple[str, str]]] = {
    "activate_mandate": [("draft", "POSITIONAL_OR_KEYWORD"),
                         ("principal_id", "KEYWORD_ONLY"),
                         ("agent_id", "KEYWORD_ONLY")],
    "get_mandate": [("mandate_id", "POSITIONAL_OR_KEYWORD")],
    "revoke_mandate": [("mandate_id", "POSITIONAL_OR_KEYWORD")],
    "create_quote": [("product_id", "KEYWORD_ONLY"), ("quantity", "KEYWORD_ONLY")],
    "spend_state": [("mandate", "POSITIONAL_OR_KEYWORD")],
    "submit_proposal": [("proposal", "POSITIONAL_OR_KEYWORD"), ("now", "KEYWORD_ONLY")],
}


def parameters_of(method) -> list[tuple[str, str]]:
    return [
        (name, parameter.kind.name)
        for name, parameter in inspect.signature(method).parameters.items()
        if name != "self"
    ]


class TestTheBoundaryIsImplemented:
    def test_every_protocol_method_exists(self, commerce):
        declared = [name for name in vars(CommerceClient) if not name.startswith("_")]
        missing = [name for name in declared if not hasattr(commerce, name)]
        assert missing == []

    def test_the_original_signatures_are_unchanged(self, commerce):
        """The freeze that makes Fake -> Real a one-line change.

        ``app/agent`` calls these; a service that satisfies the Protocol
        structurally can be swapped in ``app/main.py`` without touching the agent.
        """
        for name, expected in FROZEN_SIGNATURES.items():
            assert parameters_of(getattr(commerce, name))[:len(expected)] == expected, (
                f"{name} changed shape; callers in app/agent would have to change too"
            )

    def test_optional_clock_arguments_are_appended_never_inserted(self, commerce):
        """``now`` was added to four calls so a test can state its instant.

        Appended at the end and keyword-only, so no existing positional call site
        changes meaning.
        """
        for name in ("activate_mandate", "revoke_mandate", "create_quote",
                     "spend_state", "approve_escalation", "reject_escalation"):
            parameters = parameters_of(getattr(commerce, name))
            assert parameters[-1] == ("now", "KEYWORD_ONLY"), name

    def test_a_client_can_be_used_through_the_protocol_alone(self, db_path, mandate):
        """Nothing in A has to know this is an in-process object.

        The call site below names only ``CommerceClient``'s vocabulary, which is
        what an HTTP implementation would satisfy just as well.
        """
        client: CommerceClient = CommerceService(db_path=db_path)
        quote = client.create_quote(product_id=HP_APPROVE)
        assert quote.merchant_total_cents == 28900
        assert client.get_mandate(mandate.mandate_id).version == mandate.version
        assert client.spend_state(mandate).exposure_cents == 0


class TestTheNarrowedApproval:
    """``approve_escalation`` used to demand three of C's own objects."""

    def test_the_old_shape_was_unimplementable_from_a(self, commerce, roomy_mandate):
        """The reason the signature had to change, stated as a fact.

        A ``PaymentRouteEvaluation`` is C's derived object. The evaluator returns
        only a route *id* on the decision, and A has no call that produces one --
        the stand-in's ``evaluate_route`` was a test helper. So the previous
        signature asked A for something A could not obtain.
        """
        quote = commerce.create_quote(product_id=HP_ESCALATE, now=NOW)
        decision = commerce.submit_proposal(
            a_proposal(roomy_mandate, quote, proposal_id="prop_esc"), now=NOW)
        assert decision.outcome == "ESCALATE"
        assert decision.payment_route_id == "fps_demo"
        assert not hasattr(decision, "route"), (
            "the decision carries an id; the object lived only on the service"
        )
        assert parameters_of(commerce.approve_escalation) == [
            ("proposal_id", "POSITIONAL_OR_KEYWORD"),
            ("principal_id", "KEYWORD_ONLY"),
            ("now", "KEYWORD_ONLY"),
        ]

    def test_c_looks_up_its_own_context(self, commerce, roomy_mandate):
        """A supplies two identifiers; C finds the quote, the route and the amount."""
        quote = commerce.create_quote(product_id=HP_ESCALATE, now=NOW)
        commerce.submit_proposal(
            a_proposal(roomy_mandate, quote, proposal_id="prop_esc"), now=NOW)

        grant = commerce.approve_escalation("prop_esc", principal_id=PRINCIPAL, now=NOW)
        assert grant.quote_hash == quote.quote_hash
        assert grant.cash_total_cents == 31000
        assert grant.payment_route_id == "fps_demo"


class TestTheReadModel:
    def test_an_unknown_proposal_reads_as_none(self, commerce):
        assert commerce.get_proposal_outcome("prop_nope") is None

    def test_an_approval_outcome_carries_the_whole_history(
        self, commerce, mandate
    ):
        """One read, and A can explain every part of what happened."""
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        outcome = commerce.get_proposal_outcome("prop_0001")
        assert outcome.decision.outcome == "APPROVE"
        assert outcome.reservation is not None
        assert outcome.receipt is not None
        assert outcome.denial is None
        assert outcome.escalation is None
        assert outcome.is_settled is True
        assert outcome.settlement_source_type is SourceType.SANDBOX

    def test_a_denial_outcome_carries_the_receipt(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_OVER_CAP, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        outcome = commerce.get_proposal_outcome("prop_0001")
        assert outcome.denial is not None
        assert outcome.denial.primary_reason is ErrorCode.CAP_PER_TRANSACTION_EXCEEDED
        assert outcome.reservation is None
        assert outcome.receipt is None
        assert outcome.is_settled is False

    def test_reading_twice_changes_nothing(self, commerce, mandate):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)
        first = commerce.get_proposal_outcome("prop_0001")
        second = commerce.get_proposal_outcome("prop_0001")
        assert first == second

    def test_the_outcome_refuses_incoherent_combinations(self, commerce, mandate):
        """The read model is a contract type, so it validates itself.

        A settled proposal with no reservation, or a denial attached to an
        approval, is not a state C can produce -- and if one were ever assembled
        the model would say so rather than hand A something impossible.
        """
        from app.contracts.policy import ProposalOutcome

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)
        outcome = commerce.get_proposal_outcome("prop_0001")

        with pytest.raises(ValueError, match="implies a reservation"):
            ProposalOutcome(
                proposal_id=outcome.proposal_id, decision=outcome.decision,
                receipt=outcome.receipt,
            )


class TestReconciliationIsARead:
    def test_a_clean_run_reports_clean(self, commerce, mandate, db_path):
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        report = reconcile(db_path, now=NOW)
        assert report.is_clean
        assert report.summary()["unaccounted_holds"] == []
        assert report.summary()["audit_chain"]["ok"] is True

    def test_the_report_states_what_it_cannot_account_for(self, commerce, mandate,
                                                          db_path):
        from app.commerce.authority import AuthorityService

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        result = AuthorityService(db_path=db_path).submit(
            a_proposal(mandate, quote, proposal_id="prop_held"), now=NOW)
        assert result.reservation.status == "ACTIVE"

        report = reconcile(db_path, now=NOW)
        assert not report.is_clean, (
            "a hold with no recorded outcome is exactly what a reviewer needs to see"
        )
        assert report.unaccounted_holds[0].proposal_id == "prop_held"
        assert report.summary()["unaccounted_holds"][0]["status"] == "ACTIVE"

    def test_it_cannot_release_a_hold(self, db_path):
        """Reported, not tidied away.

        The plan's rule is that an unresolved attempt is reported; a module that
        could also clean one up is a module whose ledger cannot be trusted after
        a crash.
        """
        assert not hasattr(reconcile(db_path), "release")
        import app.commerce.reconcile as module

        public = [name for name in vars(module) if not name.startswith("_")]
        assert "release" not in public
        assert "retry" not in public


class TestErrorsAreNamed:
    def test_every_refusal_is_an_agent_error_with_a_code(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.create_quote(product_id="hp_9999", now=NOW)
        assert caught.value.code is ErrorCode.PRODUCT_NOT_FOUND
        assert isinstance(caught.value.details, dict)

    def test_internal_services_raise_rather_than_returning_errors(self, commerce):
        """The team contract: only the HTTP edge wraps.

        A function that returns a value must not also be able to return an error
        object, or every caller has to check both.
        """
        assert commerce.get_mandate("man_nope") is None
        assert commerce.get_proposal_outcome("prop_nope") is None
