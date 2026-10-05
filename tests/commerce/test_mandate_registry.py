"""The mandate registry: what makes an authorisation enforceable and checkable.

The two claims worth pinning, both of which fail silently if they fail at all:

* **the rule that is hashed is the rule that is displayed.** The ``policy_hash``
  is what the demo answers "why did it do that?" from, so a divergence between
  the stored canonical policy and ``executable_policy()`` would make the
  explanation describe a rule set nobody was governed by;
* **a version is immutable.** Revocation adds a row. If it edited one, an agent
  holding a copy of version 1 could not be distinguished from an agent that read
  the current state, and the optimism the evaluator's version check depends on
  would be gone.

Every call states its instant. Nothing here observes a clock, so a deadline in a
test is a value rather than a hope.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.contracts.common import ErrorCode
from app.contracts.mandate import Mandate, MandateDraft, policy_hash
from app.errors import AgentError
from tests.commerce.conftest import AGENT, NOW, PRINCIPAL, a_draft


def activate(commerce, **overrides) -> Mandate:
    """Activate a complete draft at the fixed instant."""
    return commerce.activate_mandate(a_draft(**overrides), principal_id=PRINCIPAL,
                                     agent_id=AGENT, now=NOW)


class TestActivation:
    def test_the_hashed_rule_is_the_displayed_rule(self, commerce):
        mandate = activate(commerce)
        stored = json.loads(mandate.canonical_policy)
        assert stored == mandate.executable_policy(), (
            "the policy the hash commits to and the policy shown to a third party "
            "differ, so 'why did it do that?' would be answered from a rule set "
            "nobody was governed by"
        )
        assert policy_hash(stored) == mandate.policy_hash

    def test_activation_records_the_clauses_the_user_agreed_to(self, commerce):
        """The consent record has to be evidence, not a boolean.

        The event is what answers "what was on screen when they said yes", so it
        carries the draft's own fields rather than a summary of them.
        """
        mandate = activate(commerce)
        events = commerce.audit_events(mandate.mandate_id)
        consent = [e for e in events if e.event_type.value == "CONSENT_RECORDED"]
        assert len(consent) == 1
        clauses = consent[0].payload["draft_fields"]
        assert clauses["cap_per_transaction_cents"] == 32000
        assert clauses["allowed_payment_routes"] == ["fps_demo"]
        assert mandate.consent_event_id == consent[0].event_id

    def test_a_draft_missing_a_clause_is_refused(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.activate_mandate(MandateDraft(cap_per_transaction_cents=30000),
                                      principal_id=PRINCIPAL, agent_id=AGENT, now=NOW)
        assert caught.value.code == ErrorCode.MANDATE_VALIDATION_FAILED
        assert "allowed_merchants" in caught.value.details["missing_fields"]

    def test_a_contradictory_draft_is_refused(self, commerce):
        with pytest.raises(AgentError) as caught:
            activate(commerce, cap_per_transaction_cents=70000, rolling_cap_cents=60000)
        assert caught.value.code == ErrorCode.MANDATE_VALIDATION_FAILED
        assert caught.value.details["structural_problems"]

    def test_a_refused_draft_writes_no_consent_record(self, db_path):
        """Validation runs before the transaction opens, and it has to.

        A consent record written for a draft that was then refused would be
        evidence of an agreement that never took effect.
        """
        from app.commerce.audit import AuditLog
        from app.commerce.database import read_connection
        from app.commerce.service import CommerceService

        service = CommerceService(db_path=db_path)
        with pytest.raises(AgentError):
            service.activate_mandate(MandateDraft(), principal_id=PRINCIPAL,
                                     agent_id=AGENT, now=NOW)
        with read_connection(db_path) as conn:
            assert AuditLog().all_events(conn) == []

    def test_the_expiry_is_derived_from_the_draft(self, commerce):
        mandate = activate(commerce, valid_for_seconds=3600)
        assert mandate.expires_at - mandate.valid_from == timedelta(seconds=3600)
        assert mandate.valid_from == NOW


class TestRevocation:
    def test_revocation_adds_a_version_and_keeps_the_policy(self, commerce):
        original = activate(commerce)
        revoked = commerce.revoke_mandate(original.mandate_id, now=NOW)

        assert revoked.version == original.version + 1
        assert revoked.status == "REVOKED"
        assert revoked.policy_hash == original.policy_hash, (
            "the rules did not change, permission did; a new hash would say the "
            "user agreed to different terms"
        )

    def test_the_previous_version_is_superseded_not_deleted(self, commerce, db_path):
        from app.commerce.mandate_registry import MandateRegistry

        original = activate(commerce)
        commerce.revoke_mandate(original.mandate_id, now=NOW)

        history = MandateRegistry(db_path=db_path).history(original.mandate_id)
        assert [(m.version, m.status) for m in history] == [
            (1, "SUPERSEDED"), (2, "REVOKED"),
        ]

    def test_revoking_twice_is_idempotent(self, commerce):
        original = activate(commerce)
        first = commerce.revoke_mandate(original.mandate_id, now=NOW)
        second = commerce.revoke_mandate(original.mandate_id, now=NOW)
        assert second.version == first.version
        assert second.status == "REVOKED"

    def test_revoking_an_unknown_mandate_is_a_named_error(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.revoke_mandate("man_nope", now=NOW)
        assert caught.value.code == ErrorCode.MANDATE_NOT_FOUND

    def test_a_settled_payment_is_recorded_as_a_missed_revocation(self, commerce):
        """Withdrawing permission does not recall money that already moved.

        The plan is explicit: record ``REVOCATION_MISSED``, never present the
        revocation as a cancellation and never as a refund.
        """
        from tests.commerce.conftest import HP_APPROVE, a_proposal

        mandate = activate(commerce)
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        commerce.revoke_mandate(mandate.mandate_id, now=NOW)
        events = commerce.audit_events(mandate.mandate_id)
        missed = [e for e in events if e.event_type.value == "REVOCATION_MISSED"]
        assert len(missed) == 1
        assert missed[0].payload["settled_cents"] == 28900
        assert "not a refund" in missed[0].payload["note"]

    def test_a_revocation_with_nothing_settled_writes_no_missed_event(self, commerce):
        mandate = activate(commerce)
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)
        events = commerce.audit_events(mandate.mandate_id)
        assert not [e for e in events if e.event_type.value == "REVOCATION_MISSED"]


class TestLookup:
    def test_the_current_version_is_the_highest_one(self, commerce):
        mandate = activate(commerce)
        assert commerce.get_mandate(mandate.mandate_id).version == 1
        commerce.revoke_mandate(mandate.mandate_id, now=NOW)
        assert commerce.get_mandate(mandate.mandate_id).version == 2

    def test_an_unknown_mandate_reads_as_none(self, commerce):
        assert commerce.get_mandate("man_nope") is None
