"""What a purchase costs, and how it would be paid for.

Two jobs, deliberately in one module: pricing a basket, and pricing the rail
that basket would travel on. They share a single responsibility -- *C's costing
model* -- and they share the rule the contract states in one line:

    cash_total_cents = merchant_total_cents + fee_cents + fx_cost_cents

It is worth restating why this module exists at all rather than letting A do
arithmetic. A never submits an amount. ``create_quote`` takes a product id and a
quantity, and nothing else, so a purchase whose price A got wrong is not
expressible: the price is read from D's catalog by C, and the total is computed
here. ``Quote`` and ``PaymentRouteEvaluation`` both re-check their own
arithmetic on construction, so a pricing defect fails where it is written
instead of being discovered by a user.

**Where the numbers come from.** Unit price and stock come from D. Shipping,
tax, discount and the platform fee come from ``app/commerce/config`` and are
labelled ``SANDBOX`` wherever they appear, because they exist nowhere in the
product data and must never read as observed market rates. FX is zero because
the settlement currency is pinned to HKD. Rewards are zero because no reward
programme is implemented -- reported as zero rather than omitted, so
``effective_cost_cents`` still reconciles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.commerce.audit import AuditLog
from app.commerce.config import (
    CATALOG_MERCHANT_ID,
    QUOTE_TTL_SECONDS,
    SANDBOX_DISCOUNT_CENTS,
    SANDBOX_PLATFORM_FEE_CENTS,
    SANDBOX_SHIPPING_CENTS,
    SANDBOX_TAX_CENTS,
)
from app.commerce.database import new_id, read_connection, write_transaction
from app.commerce.repositories import QuoteRepository
from app.contracts.audit import AuditEventType
from app.contracts.commerce import PaymentRouteEvaluation, Quote
from app.contracts.common import ActorType, ErrorCode, PaymentRail, SourceType
from app.contracts.mandate import Mandate

#: The rails the sandbox router knows how to price. A route id outside this
#: table is not silently accepted: it is reported as ineligible with a reason,
#: which is how a mandate naming a route nobody implements produces an
#: explainable denial instead of an exception.
SANDBOX_ROUTES: dict[str, PaymentRail] = {
    "fps_demo": PaymentRail.FPS,
    "mastercard_demo": PaymentRail.MASTERCARD,
    "visa_demo": PaymentRail.VISA,
    "wallet_demo": PaymentRail.VIRTUAL_WALLET,
}

#: The rail used when a mandate permits several and the caller states no
#: preference. Chosen for being the demo rail, not for being better.
DEFAULT_ROUTE_ID = "fps_demo"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class QuoteService:
    """Prices one product for one quantity, from D's catalog. Owns the transaction."""

    def __init__(self, *, db_path=None, audit: AuditLog | None = None) -> None:
        self._db_path = db_path
        self._audit = audit or AuditLog()
        self._quotes = QuoteRepository()

    def create(self, *, product_id: str, quantity: int = 1,
               now: datetime | None = None) -> Quote:
        """Read the catalog, price the basket, record the offer.

        Stock is checked before pricing, because a quote for something that
        cannot be shipped is a price nobody can act on, and reporting it as
        available would put the failure at settlement where the user has already
        been told the purchase is going ahead.
        """
        from app.catalog import ProductRepository
        from app.errors import AgentError

        if type(quantity) is not int or not 1 <= quantity <= 10:
            raise AgentError(ErrorCode.VALIDATION_ERROR, "quantity must be an integer from 1 to 10", details={"field": "quantity"})
        moment = now or _utc()
        product = ProductRepository(self._db_path).get_product(product_id)
        if product is None:
            raise AgentError(
                ErrorCode.PRODUCT_NOT_FOUND,
                f"no product {product_id!r}",
                details={"product_id": product_id},
            )
        if product.stock < quantity:
            raise AgentError(
                ErrorCode.OUT_OF_STOCK,
                f"{product_id} has {product.stock} in stock, {quantity} requested",
                details={"product_id": product_id, "stock": product.stock,
                         "requested": quantity},
            )

        subtotal = product.price_cents * quantity
        shipping = SANDBOX_SHIPPING_CENTS
        merchant_total = (subtotal + shipping + SANDBOX_TAX_CENTS
                          - SANDBOX_DISCOUNT_CENTS)

        provisional = Quote.model_construct(
            quote_id=new_id("q"),
            merchant_id=CATALOG_MERCHANT_ID,
            product_id=product.product_id,
            product_name=product.name,
            category=product.category,
            connection=product.connection,
            form_factor=product.form_factor,
            anc=product.anc,
            supported_devices=product.supported_devices,
            quantity=quantity,
            unit_price_cents=product.price_cents,
            subtotal_cents=subtotal,
            shipping_cents=shipping,
            tax_cents=SANDBOX_TAX_CENTS,
            discount_cents=SANDBOX_DISCOUNT_CENTS,
            merchant_total_cents=merchant_total,
            currency="HKD",
            source_type=SourceType.SANDBOX,
            source_ref=None,
            issued_at=moment,
            expires_at=moment + timedelta(seconds=QUOTE_TTL_SECONDS),
            quote_hash="",
        )
        quote = Quote.model_validate({
            **provisional.model_dump(), "quote_hash": provisional.compute_hash(),
        })

        with write_transaction(self._db_path) as conn:
            self._quotes.add(conn, quote)
            self._audit.append(
                conn,
                event_type=AuditEventType.QUOTE_ISSUED,
                actor_type=ActorType.COMMERCE,
                actor_id="quote_service",
                occurred_at=moment,
                payload={
                    "quote_id": quote.quote_id,
                    "product_id": quote.product_id,
                    "quantity": quote.quantity,
                    "merchant_total_cents": quote.merchant_total_cents,
                    "quote_hash": quote.quote_hash,
                    "source_type": quote.source_type.value,
                    "expires_at": quote.expires_at.isoformat(),
                },
            )
        return quote

    def get(self, quote_id: str) -> Quote | None:
        with read_connection(self._db_path) as conn:
            return self._quotes.get(conn, quote_id)


class SandboxPaymentRouter:
    """Prices the rails a mandate permits, and picks the one to use.

    The evaluator takes exactly one :class:`PaymentRouteEvaluation`, so choosing
    which rail to price is C's job. The rule: the candidate set is the mandate's
    own allowlist, because a route the mandate does not permit can never be used
    and offering it would only produce a denial; the order is the caller's
    stated preference first, then the mandate's order.

    A route nobody implements is *priced and refused*, not skipped. If every
    candidate is unusable the caller still receives an evaluation, carrying the
    reasons, so the evaluator returns a denial that names the problem instead of
    the router raising an error the user cannot act on.
    """

    def candidates(self, mandate: Mandate, preferred: list[str]) -> list[str]:
        allowed = list(mandate.allowed_payment_routes)
        ordered = [route for route in preferred if route in allowed]
        ordered += [route for route in allowed if route not in ordered]
        return ordered or [DEFAULT_ROUTE_ID]

    def price(self, quote: Quote, mandate: Mandate, route_id: str) -> PaymentRouteEvaluation:
        rail = SANDBOX_ROUTES.get(route_id)
        if rail is None:
            return PaymentRouteEvaluation(
                route_id=route_id,
                rail=PaymentRail.FPS,  # the rail is unknown, so any value is a guess
                accepted_by_merchant=False,
                allowed_by_mandate=route_id in mandate.allowed_payment_routes,
                owned_by_principal=True,
                eligible=False,
                merchant_total_cents=quote.merchant_total_cents,
                fee_cents=0,
                fx_cost_cents=0,
                cash_total_cents=quote.merchant_total_cents,
                reward_value_cents=0,
                effective_cost_cents=quote.merchant_total_cents,
                evidence_type=SourceType.SANDBOX,
                rejection_reasons=[f"no sandbox rail is configured for {route_id!r}"],
            )

        fee = SANDBOX_PLATFORM_FEE_CENTS
        fx = 0  # HKD settles in HKD; there is no conversion to cost.
        cash = quote.merchant_total_cents + fee + fx
        return PaymentRouteEvaluation(
            route_id=route_id,
            rail=rail,
            accepted_by_merchant=True,
            allowed_by_mandate=route_id in mandate.allowed_payment_routes,
            owned_by_principal=True,
            eligible=True,
            merchant_total_cents=quote.merchant_total_cents,
            fee_cents=fee,
            fx_cost_cents=fx,
            cash_total_cents=cash,
            reward_value_cents=0,
            effective_cost_cents=cash,
            evidence_type=SourceType.SANDBOX,
        )

    def choose(self, quote: Quote, mandate: Mandate,
               preferred: list[str]) -> PaymentRouteEvaluation:
        """The first usable candidate, or the first candidate with its reasons.

        Falling back to an ineligible route rather than raising is what lets the
        decision carry ``PAYMENT_ROUTE_NOT_ALLOWED`` with
        ``route.rejection_reasons`` attached -- a denial the user can act on.
        """
        priced = [self.price(quote, mandate, route_id)
                  for route_id in self.candidates(mandate, preferred)]
        for evaluation in priced:
            if evaluation.eligible:
                return evaluation
        return priced[0]


__all__ = ["DEFAULT_ROUTE_ID", "QuoteService", "SANDBOX_ROUTES", "SandboxPaymentRouter"]
