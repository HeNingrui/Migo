"""The payment capability: authority over one transaction, spent once.

A capability is the only object in the system that says "this exact payment may
happen". Everything here is about the ways that sentence could be made to mean
more than it does: a different amount, a different route, a second use, a longer
life, or a token that was never issued at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.commerce.capability import Capability, CapabilityService
from app.commerce.database import read_connection, write_transaction
from app.contracts.common import ErrorCode
from app.errors import AgentError
from tests.commerce.conftest import NOW

QUOTE_HASH = "sha256:" + "a" * 64


@pytest.fixture
def capability_service() -> CapabilityService:
    """The service on its own.

    It owns no connection by design -- issuing and spending both happen inside a
    caller's transaction -- so the tests here supply one from the same temporary
    database the rest of the suite uses.
    """
    return CapabilityService()


class TestIssuance:
    def test_the_token_verifies_and_names_the_transaction(self, capability_service):
        capability = Capability(
            nonce="nonce_1", reservation_id="res_1", proposal_id="prop_1",
            quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
            cash_total_cents=28900, currency="HKD",
            issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        )
        token = capability_service.sign(capability)
        payload = capability_service.verify(token, now=NOW)
        assert payload["r"] == "res_1"
        assert payload["a"] == 28900
        assert payload["t"] == "fps_demo"
        assert payload["q"] == QUOTE_HASH

    def test_the_token_is_labelled_with_its_format(self, capability_service):
        token = capability_service.sign(Capability(
            nonce="n", reservation_id="r", proposal_id="p", quote_hash=QUOTE_HASH,
            payment_route_id="fps_demo", cash_total_cents=1, currency="HKD",
            issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        ))
        assert token.startswith("c1.")

    def test_the_token_is_never_stored(self, capability_service, db_path):
        """Only the nonce and a digest of the facts, because the chain is public.

        Storing the token would put a bearer credential at rest and, in the worst
        case, into an audit payload meant to be shown to people.
        """
        with write_transaction(db_path) as conn:
            capability, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with read_connection(db_path) as conn:
            row = capability_service._nonces.get(conn, capability.nonce)  # noqa: SLF001
        assert row is not None
        assert row["consumed_at"] is None
        assert row["context_hash"] == capability.context_hash()
        assert token not in str(dict(row)), (
            "the token is a bearer credential; a copy at rest is a copy that leaks"
        )


class TestRefusals:
    def test_a_tampered_payload_does_not_verify(self, capability_service, db_path):
        """Raising the amount is not a forgery that a later check has to catch.

        The signature covers the whole context, so the modified token simply is
        not a token C issued.
        """
        import base64
        import json

        service = CapabilityService()
        capability = Capability(
            nonce="n", reservation_id="r", proposal_id="p", quote_hash=QUOTE_HASH,
            payment_route_id="fps_demo", cash_total_cents=100, currency="HKD",
            issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        )
        token = service.sign(capability)
        version, body, mac = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body + "==="))
        payload["a"] = 1
        forged = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")

        with pytest.raises(AgentError) as caught:
            service.verify(f"{version}.{forged}.{mac}", now=NOW)
        assert caught.value.code == ErrorCode.CAPABILITY_INVALID

    def test_a_token_that_was_never_issued_is_missing(self, db_path):
        """A well-signed token with no nonce row is not authority either.

        The nonce table is the record of what C actually authorised; a token
        alone is a claim about it.
        """
        service = CapabilityService()
        capability = Capability(
            nonce="never_issued", reservation_id="res_1", proposal_id="prop_1",
            quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
            cash_total_cents=28900, currency="HKD",
            issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        )
        token = service.sign(capability)
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                service.spend(
                    conn, token=token, payment_id="pay_1", now=NOW,
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                    cash_total_cents=28900,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_MISSING

    def test_an_expired_token_is_refused(self, capability_service, db_path):
        with write_transaction(db_path) as conn:
            _, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                capability_service.spend(
                    conn, token=token, payment_id="pay_1",
                    now=NOW + timedelta(seconds=121),
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                    cash_total_cents=28900,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_EXPIRED

    def test_a_different_amount_is_a_context_mismatch(self, capability_service, db_path):
        """Authority over HK$289 is not authority over HK$290."""
        with write_transaction(db_path) as conn:
            _, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                capability_service.spend(
                    conn, token=token, payment_id="pay_1", now=NOW,
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                    cash_total_cents=29000,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_CONTEXT_MISMATCH

    def test_a_different_route_is_a_context_mismatch(self, capability_service, db_path):
        with write_transaction(db_path) as conn:
            _, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                capability_service.spend(
                    conn, token=token, payment_id="pay_1", now=NOW,
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash=QUOTE_HASH, payment_route_id="mastercard_demo",
                    cash_total_cents=28900,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_CONTEXT_MISMATCH

    def test_a_different_quote_is_a_context_mismatch(self, capability_service, db_path):
        with write_transaction(db_path) as conn:
            _, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                capability_service.spend(
                    conn, token=token, payment_id="pay_1", now=NOW,
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash="sha256:" + "b" * 64, payment_route_id="fps_demo",
                    cash_total_cents=28900,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_CONTEXT_MISMATCH

    def test_a_capability_is_spent_once(self, capability_service, db_path):
        """The replay refusal, and the check that decides it.

        Single use is a conditional UPDATE and a rowcount rather than a read
        followed by a write, so two attempts presenting one token cannot both
        match the unconsumed row.
        """
        with write_transaction(db_path) as conn:
            _, token = capability_service.issue(
                conn, reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900, currency="HKD", now=NOW,
            )
        with write_transaction(db_path) as conn:
            capability_service.spend(
                conn, token=token, payment_id="pay_1", now=NOW,
                reservation_id="res_1", proposal_id="prop_1",
                quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                cash_total_cents=28900,
            )
        with write_transaction(db_path) as conn:
            with pytest.raises(AgentError) as caught:
                capability_service.spend(
                    conn, token=token, payment_id="pay_2", now=NOW,
                    reservation_id="res_1", proposal_id="prop_1",
                    quote_hash=QUOTE_HASH, payment_route_id="fps_demo",
                    cash_total_cents=28900,
                )
        assert caught.value.code == ErrorCode.CAPABILITY_REPLAYED

    def test_a_malformed_token_is_invalid(self, capability_service):
        service = CapabilityService()
        for bad in ("", "not-a-token", "c1.only-two-parts", "zz.aaa.bbb"):
            with pytest.raises(AgentError) as caught:
                service.verify(bad, now=NOW)
            assert caught.value.code == ErrorCode.CAPABILITY_INVALID


class TestThroughSettlement:
    """The capability as the settlement flow actually uses it."""

    def test_a_settlement_issues_and_consumes_exactly_one(self, commerce, mandate,
                                                          db_path):
        from tests.commerce.conftest import HP_APPROVE, a_proposal

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        events = [e.event_type.value
                  for e in commerce.audit_events_for_proposal("prop_0001")]
        assert events.count("CAPABILITY_ISSUED") == 1
        assert events.count("CAPABILITY_CONSUMED") == 1

    def test_the_audit_payload_carries_the_nonce_and_not_the_token(
        self, commerce, mandate
    ):
        """The chain is meant to be shown to people.

        A capability is a bearer credential; a copy in a log that is published to
        a reviewer is a copy that leaks. The nonce identifies it without being
        usable.
        """
        from tests.commerce.conftest import HP_APPROVE, a_proposal

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        commerce.submit_proposal(a_proposal(mandate, quote), now=NOW)

        for event in commerce.audit_events_for_proposal("prop_0001"):
            if event.event_type.value.startswith("CAPABILITY"):
                assert "nonce" in event.payload
                assert not any("token" in key for key in event.payload)
                assert "signature" not in event.payload
