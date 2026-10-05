"""The audit chain: append-only, dense, chained, and free of credentials.

The claim under test is the one the demo makes to a judge: *a third party can
check this without trusting the operator*. That claim is only worth anything if
the record cannot be edited afterwards and if every commerce transaction writes
to it, so the tests here attack the record rather than reading it.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.commerce.audit import AuditLog
from app.commerce.database import read_connection, write_transaction
from app.contracts.audit import AuditEventType, verify_chain
from app.contracts.common import ActorType
from tests.commerce.conftest import HP_APPROVE, NOW, a_proposal


def run_a_purchase(commerce, mandate):
    quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
    commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)


class TestTheChain:
    def test_a_settled_purchase_leaves_a_verifiable_chain(self, commerce, mandate):
        run_a_purchase(commerce, mandate)
        report = commerce.verify_audit_chain()
        assert report.ok
        assert report.checked > 0
        assert report.breaks == []

    def test_the_sequence_is_dense_and_starts_at_zero(self, commerce, mandate):
        run_a_purchase(commerce, mandate)
        events = commerce.audit_events(mandate.mandate_id)
        sequences = [e.sequence for e in events]
        assert sequences == sorted(sequences)

        with read_connection(commerce._db_path) as conn:
            everything = AuditLog().all_events(conn)
        assert [e.sequence for e in everything] == list(range(len(everything))), (
            "a missing number must itself be evidence, which is why the sequence is "
            "dense and recorded rather than implied by insertion order"
        )
        assert everything[0].is_genesis

    def test_every_event_commits_to_its_predecessor(self, commerce, mandate):
        run_a_purchase(commerce, mandate)
        with read_connection(commerce._db_path) as conn:
            events = AuditLog().all_events(conn)
        assert events[0].previous_hash is None
        for previous, event in zip(events, events[1:]):
            assert event.previous_hash == previous.event_hash

    def test_an_edited_record_breaks_the_chain(self, commerce, mandate):
        """The attack the chain exists to catch: a payload quietly changed.

        Editing a row is allowed by SQLite and refused by nothing but the
        recomputation, which is the point -- the check is what makes the record
        evidence rather than a log.
        """
        run_a_purchase(commerce, mandate)
        with read_connection(commerce._db_path) as conn:
            events = AuditLog().all_events(conn)

        tampered = events[1].model_copy(update={"payload": {"outcome": "APPROVE"}})
        report = verify_chain([events[0], tampered, *events[2:]])
        assert not report.ok
        assert any(b.problem == "hash_mismatch" for b in report.breaks)

    def test_a_removed_record_is_detected(self, commerce, mandate):
        run_a_purchase(commerce, mandate)
        with read_connection(commerce._db_path) as conn:
            events = AuditLog().all_events(conn)

        without_one = [e for e in events if e.sequence != 2]
        report = verify_chain(without_one)
        assert not report.ok
        assert any(b.problem == "sequence_gap" for b in report.breaks)


class TestAppendOnly:
    def test_the_database_refuses_an_update(self, commerce, mandate):
        """Enforced by a trigger, not by this package's discipline.

        A rule that lives in the writer is a rule that lasts until somebody
        writes a second writer.
        """
        run_a_purchase(commerce, mandate)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with write_transaction(commerce._db_path) as conn:
                conn.execute("UPDATE audit_events SET actor_id = 'someone' WHERE sequence = 0")

    def test_the_database_refuses_a_delete(self, commerce, mandate):
        run_a_purchase(commerce, mandate)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with write_transaction(commerce._db_path) as conn:
                conn.execute("DELETE FROM audit_events WHERE sequence = 0")


class TestPayloads:
    def test_a_credential_in_a_payload_is_refused(self, commerce, mandate):
        """The chain is meant to be shown to people.

        The contract states the rule; this makes it a check rather than a note,
        because the payload is assembled by callers and a caller will eventually
        reach for the obvious key name.
        """
        with write_transaction(commerce._db_path) as conn:
            with pytest.raises(ValueError, match="credential"):
                AuditLog().append(
                    conn, event_type=AuditEventType.PAYMENT_SUBMITTED,
                    actor_type=ActorType.COMMERCE, actor_id="test", occurred_at=NOW,
                    payload={"capability_token": "c1.abc.def"},
                )

    def test_the_capability_events_carry_the_nonce_and_nothing_usable(
        self, commerce, mandate
    ):
        run_a_purchase(commerce, mandate)
        events = commerce.audit_events_for_proposal("prop_0001")
        for event in events:
            assert "token" not in event.payload
            assert "signature" not in event.payload
            assert "secret" not in event.payload

    def test_the_event_types_cover_the_whole_purchase(self, commerce, mandate):
        """A reader should be able to reconstruct the flow from the types alone."""
        run_a_purchase(commerce, mandate)
        seen = [e.event_type.value for e in commerce.audit_events_for_proposal("prop_0001")]
        assert seen == [
            "PROPOSAL_CREATED",
            "POLICY_EVALUATED",
            "DECISION_APPROVED",
            "RESERVATION_CREATED",
            "CAPABILITY_ISSUED",
            "CAPABILITY_CONSUMED",
            "PAYMENT_SETTLED",
        ]

    def test_a_denial_records_the_codes_that_caused_it(self, commerce, mandate):
        from tests.commerce.conftest import HP_OVER_CAP

        quote = commerce.create_quote(product_id=HP_OVER_CAP, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        events = commerce.audit_events_for_proposal("prop_0001")
        denied = next(e for e in events if e.event_type.value == "DECISION_DENIED")
        assert "CAP_PER_TRANSACTION_EXCEEDED" in denied.payload["violation_codes"]
        assert denied.payload["primary_reason"] == "CAP_PER_TRANSACTION_EXCEEDED"

    def test_the_evaluated_event_carries_what_was_measured(self, commerce, mandate):
        """So "why did it do that?" is answered from the record, not from prose."""
        run_a_purchase(commerce, mandate)
        events = commerce.audit_events_for_proposal("prop_0001")
        evaluated = next(e for e in events if e.event_type.value == "POLICY_EVALUATED")
        assert evaluated.payload["cash_total_cents"] == 28900
        assert evaluated.payload["outcome"] == "APPROVE"
        assert evaluated.payload["applicable_limits"]["cap_per_transaction_cents"] == 32000
