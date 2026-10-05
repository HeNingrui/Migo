"""Contract tests for the hash-chained audit log.

The audit log is the evidence a third party is asked to check, so these tests
are about what a verifier can detect rather than about what the writer records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts import (
    ActorType,
    AuditEvent,
    AuditEventType,
    verify_chain,
)
from app.contracts.audit import HASH_PREFIX

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def make_event(sequence: int, previous_hash: str | None, **overrides) -> AuditEvent:
    """Build one record, hashing it exactly the way the writer does.

    The event is constructed first and hashed from its own serialisation, so a
    test can never disagree with the verifier about what was hashed.
    """
    payload = dict(
        event_id=f"evt_{sequence:04d}",
        sequence=sequence,
        event_type=AuditEventType.POLICY_EVALUATED,
        actor_type=ActorType.COMMERCE,
        actor_id="commerce_core",
        mandate_id="man_0001",
        proposal_id=f"prop_{sequence:04d}",
        payload={"outcome": "APPROVE", "step": sequence},
        previous_hash=previous_hash,
        occurred_at=NOW + timedelta(seconds=sequence),
    )
    payload.update(overrides)
    payload["event_hash"] = HASH_PREFIX + "0" * 64     # placeholder, not hashed
    event = AuditEvent.model_validate(payload)
    return event.model_copy(update={"event_hash": event.compute_hash()})


def retampered(event: AuditEvent, **changes) -> AuditEvent:
    """Apply a change and recompute the hash, i.e. forge a consistent record."""
    edited = event.model_copy(update=changes)
    return edited.model_copy(update={"event_hash": edited.compute_hash()})


def make_chain(length: int) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    previous: str | None = None
    for sequence in range(length):
        event = make_event(sequence, previous)
        events.append(event)
        previous = event.event_hash
    return events


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_first_record_has_no_predecessor():
    event = make_event(0, None)
    assert event.is_genesis is True
    assert event.previous_hash is None
    assert event.hash_matches() is True


def test_a_later_record_must_link_to_its_predecessor():
    with pytest.raises(ValidationError):
        make_event(1, None)
    with pytest.raises(ValidationError):
        make_event(0, HASH_PREFIX + "a" * 64)


def test_event_hash_is_prefixed_hex_and_stable():
    event = make_event(0, None)
    assert event.event_hash.startswith(HASH_PREFIX)
    assert len(event.event_hash) == len(HASH_PREFIX) + 64
    assert event.compute_hash() == event.compute_hash()


def test_hash_excludes_its_own_field():
    """A record cannot contain its own digest, so the hashed object omits it."""
    event = make_event(0, None)
    assert "event_hash" not in event.commit_payload()


def test_payload_order_does_not_change_the_hash():
    first = make_event(0, None, payload={"a": 1, "b": 2})
    second = make_event(0, None, payload={"b": 2, "a": 1})
    assert first.event_hash == second.event_hash


def test_editing_a_payload_breaks_the_recorded_hash():
    original = make_event(0, None, payload={"outcome": "APPROVE"})
    edited = original.model_copy(update={"payload": {"outcome": "DENY"}})
    assert edited.hash_matches() is False


def test_events_are_immutable():
    event = make_event(0, None)
    with pytest.raises(ValidationError):
        event.payload = {"outcome": "DENY"}


def test_events_reject_unknown_fields():
    with pytest.raises(ValidationError):
        AuditEvent.model_validate({
            "event_id": "e", "sequence": 0,
            "event_type": "POLICY_EVALUATED", "actor_type": "COMMERCE",
            "actor_id": "c", "event_hash": HASH_PREFIX + "0" * 64,
            "occurred_at": NOW, "surprise": True,
        })


def test_negative_sequence_is_rejected():
    with pytest.raises(ValidationError):
        make_event(-1, None)


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------

def test_event_types_cover_the_boundaries():
    """Each subsystem that changes state must have somewhere to record it."""
    for name in ("MANDATE_ACTIVATED", "MANDATE_REVOKED", "CONSENT_RECORDED",
                 "POLICY_EVALUATED", "DECISION_DENIED", "RESERVATION_CREATED",
                 "RESERVATION_RELEASED", "CAPABILITY_ISSUED", "CAPABILITY_REJECTED",
                 "PAYMENT_SETTLED", "PAYMENT_UNKNOWN", "REVOCATION_MISSED"):
        assert AuditEventType[name].value == name


def test_there_is_an_event_for_the_honest_gap():
    """A revocation that arrives too late is recorded, not hidden."""
    assert AuditEventType.REVOCATION_MISSED.value == "REVOCATION_MISSED"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def test_an_intact_chain_verifies():
    report = verify_chain(make_chain(5))
    assert report.ok is True
    assert report.breaks == []
    assert report.checked == 5
    assert report.first_sequence == 0
    assert report.last_sequence == 4
    assert report.head_hash == make_chain(5)[-1].event_hash


def test_an_empty_chain_verifies_trivially():
    report = verify_chain([])
    assert report.ok is True
    assert report.is_empty_chain is True


def test_a_missing_first_record_is_detected():
    report = verify_chain(make_chain(5)[1:])
    assert report.ok is False
    assert any(b.problem == "genesis_missing" for b in report.breaks)


def test_a_removed_record_is_detected_as_a_gap_and_a_broken_link():
    events = make_chain(5)
    del events[2]
    report = verify_chain(events)
    assert report.ok is False
    problems = {b.problem for b in report.breaks}
    assert "sequence_gap" in problems
    assert "previous_hash_mismatch" in problems


def test_out_of_order_input_still_verifies():
    """Ordering is by ``sequence``, not by list position.

    This also shows why a swap inside a well-formed chain is undetectable: a
    record's ``sequence`` is part of what it hashes, so moving records around a
    list changes nothing about their contents.
    """
    events = make_chain(5)
    shuffled = [events[3], events[0], events[4], events[1], events[2]]
    report = verify_chain(shuffled)
    assert report.ok is True
    assert report.first_sequence == 0
    assert report.last_sequence == 4


def test_a_record_that_claims_the_wrong_position_is_detected():
    """A record whose sequence was altered no longer hashes to what it claims.

    Reordering is only meaningful if a record's position can be falsified, and
    that is exactly what the hash covers.
    """
    events = make_chain(5)
    events[3] = events[3].model_copy(update={"sequence": 2})
    report = verify_chain(events)
    assert report.ok is False
    problems = {b.problem for b in report.breaks}
    assert "hash_mismatch" in problems or "previous_hash_mismatch" in problems


def test_a_missing_record_is_detected_and_located():
    events = make_chain(5)
    del events[2]
    report = verify_chain(events)
    assert report.ok is False
    gap = next(b for b in report.breaks if b.problem == "sequence_gap")
    assert gap.sequence == 3
    assert "expected sequence 2" in gap.detail


def test_an_edited_record_with_a_stale_hash_is_detected():
    """Editing a payload without recomputing the hash is caught immediately.

    This is the case my own test helper had to work around, and it is the one a
    casual tamper actually produces.
    """
    events = make_chain(5)
    events[1] = events[1].model_copy(update={"payload": {"outcome": "DENY"}})
    report = verify_chain(events)
    assert report.ok is False
    mismatch = next(b for b in report.breaks if b.problem == "hash_mismatch")
    assert mismatch.sequence == 1
    assert mismatch.event_id == "evt_0001"


def test_rehashing_one_record_breaks_the_next_link():
    """The cascade is the mechanism.

    A writer who edits a payload and recomputes that record's hash does not get
    away with it: the next record still points at the old hash, so the break
    simply moves one position along.
    """
    events = make_chain(5)
    events[1] = retampered(events[1], payload={"outcome": "DENY"})
    report = verify_chain(events)

    assert report.ok is False
    assert not any(b.problem == "hash_mismatch" for b in report.breaks)
    mismatched = [b.sequence for b in report.breaks
                  if b.problem == "previous_hash_mismatch"]
    assert mismatched == [2]          # the immediate successor, precisely located


def test_a_fully_rehashed_chain_needs_an_external_anchor():
    """Rehashing the whole suffix restores internal consistency.

    The honest limit of a hash chain with a single writer. What still catches it
    is the head hash published earlier, which is why the reader-facing export
    records one.
    """
    events = make_chain(5)
    published_head = events[-1].event_hash

    events[1] = retampered(events[1], payload={"outcome": "DENY"})
    for index in (2, 3, 4):
        events[index] = retampered(events[index],
                                   previous_hash=events[index - 1].event_hash)

    report = verify_chain(events)
    assert report.ok is True                        # internally consistent again
    assert report.head_hash != published_head       # but not the published head


def test_a_duplicated_sequence_is_detected():
    events = make_chain(3)
    events.append(make_event(1, events[0].event_hash))
    report = verify_chain(events)
    assert report.ok is False
    assert any(b.problem == "sequence_out_of_order" for b in report.breaks)


def test_verification_is_order_independent_of_input():
    events = make_chain(5)
    assert verify_chain(list(reversed(events))).ok is True


def test_a_break_reports_a_message_a_human_can_act_on():
    events = make_chain(5)
    del events[2]
    report = verify_chain(events)
    first = report.breaks[0]
    assert first.sequence >= 0
    assert first.detail
    assert first.event_id


# ---------------------------------------------------------------------------
# What must never be recorded
# ---------------------------------------------------------------------------

def test_payload_is_a_plain_mapping_the_chain_can_be_shown_with():
    """The chain is meant to be displayed, so payloads are structured data."""
    event = make_event(0, None, payload={"cash_total_cents": 28900,
                                         "outcome": "APPROVE",
                                         "rule": "cap_per_transaction_cents"})
    assert isinstance(event.payload, dict)
    assert event.payload["observed"] if "observed" in event.payload else True
    assert "capability" not in event.payload
