"""Pricing: what a purchase costs, and how it would be paid for.

The property that matters most here is negative: **A cannot influence the
amount.** ``create_quote`` takes a product id and a count. There is no parameter
for a price, a shipping rate or a total, so a purchase whose price A got wrong
is not expressible -- and these tests pin the shape as much as the arithmetic.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.commerce.config import (
    CATALOG_MERCHANT_ID,
    SANDBOX_PLATFORM_FEE_CENTS,
    SANDBOX_SHIPPING_CENTS,
)
from app.commerce.quote_service import QuoteService, SandboxPaymentRouter
from app.contracts.common import ErrorCode, PaymentRail, SourceType
from app.errors import AgentError
from tests.commerce.conftest import NOW, ROUTE_ID, HP_OUT_OF_STOCK


class TestQuote:
    def test_the_total_reconciles(self, commerce):
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert quote.subtotal_cents == quote.unit_price_cents * quote.quantity
        assert quote.merchant_total_cents == (
            quote.subtotal_cents + quote.shipping_cents
            + quote.tax_cents - quote.discount_cents
        )

    def test_the_price_comes_from_the_catalog_and_not_from_the_caller(self, commerce):
        """A supplies an identifier, so there is no amount for it to get wrong."""
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert quote.unit_price_cents == 27900
        assert quote.quantity == 1
        assert quote.merchant_total_cents == 27900 + SANDBOX_SHIPPING_CENTS

    def test_the_quantity_multiplies_before_shipping(self, commerce):
        quote = commerce.create_quote(product_id="hp_0018", quantity=2, now=NOW)
        assert quote.subtotal_cents == 2 * 27900
        assert quote.shipping_cents == SANDBOX_SHIPPING_CENTS

    def test_the_hash_verifies_and_is_stable(self, commerce):
        first = commerce.create_quote(product_id="hp_0018", now=NOW)
        second = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert first.hash_matches() and second.hash_matches()
        assert first.quote_hash == second.quote_hash, (
            "re-pricing the same basket at the same amount must yield the same hash, "
            "or a genuine price change cannot be told from a re-quote"
        )
        assert first.quote_id != second.quote_id

    def test_the_shipping_rate_is_labelled_sandbox(self, commerce):
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert quote.shipping_cents > 0
        assert quote.source_type is SourceType.SANDBOX, (
            "the flat shipping rate exists nowhere in the product data and must "
            "never be presented as observed"
        )

    def test_the_quote_carries_the_merchant_of_record(self, commerce):
        """The merchant A copies back into a proposal comes from here.

        D-09 is a SPEC GAP: neither Product nor SearchResponse carries a
        merchant, so A cannot obtain one from B. C owns it for the catalog it
        prices from, and the proposal's copy is re-checked against the mandate.
        """
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert quote.merchant_id == CATALOG_MERCHANT_ID

    def test_an_unknown_product_is_a_named_error(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.create_quote(product_id="hp_9999", now=NOW)
        assert caught.value.code == ErrorCode.PRODUCT_NOT_FOUND

    def test_stock_is_checked_before_pricing(self, commerce):
        with pytest.raises(AgentError) as caught:
            commerce.create_quote(product_id=HP_OUT_OF_STOCK, now=NOW)
        assert caught.value.code == ErrorCode.OUT_OF_STOCK
        assert caught.value.details["stock"] == 0

    def test_the_quote_expires_after_its_stated_life(self, commerce, db_path):
        from app.commerce.config import QUOTE_TTL_SECONDS

        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        assert quote.expires_at - quote.issued_at == timedelta(seconds=QUOTE_TTL_SECONDS)
        assert QuoteService(db_path=db_path).get(quote.quote_id) == quote

    def test_issuing_a_quote_is_recorded(self, commerce):
        """A priced offer is an event in the chain, even before a proposal exists.

        The enum had no value for it, so a quote could only have been recorded as
        untyped free text -- which the enum's own docstring rules out.
        """
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        events = commerce.audit_events_for_proposal("nothing")
        assert events == [], "no proposal yet, so nothing is attributed to one"

        from app.commerce.audit import AuditLog
        from app.commerce.database import read_connection

        with read_connection(commerce._db_path) as conn:
            issued = [e for e in AuditLog().all_events(conn)
                      if e.event_type.value == "QUOTE_ISSUED"]
        assert len(issued) == 1
        assert issued[0].payload["quote_hash"] == quote.quote_hash
        assert commerce.verify_audit_chain().ok


class TestPaymentRoute:
    def test_the_route_reconciles_with_the_quote(self, commerce, mandate):
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        route = SandboxPaymentRouter().choose(quote, mandate, [ROUTE_ID])
        assert route.cash_total_cents == (
            route.merchant_total_cents + route.fee_cents + route.fx_cost_cents
        )
        assert route.merchant_total_cents == quote.merchant_total_cents
        assert route.fee_cents == SANDBOX_PLATFORM_FEE_CENTS
        assert route.effective_cost_cents == route.cash_total_cents

    def test_an_eligible_route_carries_no_rejections(self, commerce, mandate):
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        route = SandboxPaymentRouter().choose(quote, mandate, [ROUTE_ID])
        assert route.eligible and not route.rejection_reasons
        assert route.rail is PaymentRail.FPS

    def test_a_route_the_mandate_does_not_permit_is_never_chosen(self, commerce, mandate):
        """The candidate set is the mandate's allowlist.

        Offering a route the mandate excludes could only ever produce a denial,
        and a router that proposed one would be reporting an option the user does
        not have.
        """
        router = SandboxPaymentRouter()
        assert router.candidates(mandate, ["mastercard_demo"]) == [ROUTE_ID]

    def test_an_unimplemented_route_is_priced_and_refused(self, commerce):
        """A route nobody implements must produce an explainable denial.

        Returning an evaluation with reasons lets the decision carry
        ``PAYMENT_ROUTE_NOT_ALLOWED``; raising instead would surface a router
        crash for something the user could have been told about.
        """
        from tests.commerce.conftest import a_draft

        mandate = commerce.activate_mandate(
            a_draft(allowed_payment_routes=["wallet_not_implemented"]),
            principal_id="demo_user", agent_id="demo_agent", now=NOW,
        )
        quote = commerce.create_quote(product_id="hp_0018", now=NOW)
        route = SandboxPaymentRouter().choose(quote, mandate, [])
        assert not route.eligible
        assert route.rejection_reasons
        assert route.cash_total_cents == quote.merchant_total_cents
