"""Fixtures for C's tests: a real database, on a temporary file.

Not a mock and not an in-memory database. Everything worth testing in
``app/commerce`` is a property of a transaction: that two writes are atomic,
that a conditional UPDATE matches once, that a trigger refuses an edit. None of
those can be observed through a fake, so the tests run against a real SQLite
file created by D's own initializer, on the same code path a demo uses.

The file is temporary and never committed; ``var/demo.sqlite3`` is for running
the thing, not for testing it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.commerce.payment import PaymentService  # noqa: E402
from app.commerce.schema import create_commerce_schema, initialize_wallets  # noqa: E402
from app.commerce.service import CommerceService  # noqa: E402
from app.contracts.commerce import PurchaseProposal  # noqa: E402
from app.contracts.common import new_request_id  # noqa: E402
from app.contracts.mandate import MandateDraft  # noqa: E402
from app.db import initialize_database  # noqa: E402

#: A fixed instant, so every deadline in a test is stated rather than observed.
#: Nothing in ``app/commerce`` reads a clock that a caller cannot supply.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

MERCHANT = "demo_audio_store"
PRINCIPAL = "demo_user"
AGENT = "demo_agent"
ADDRESS = "addr_demo_01"
ROUTE_ID = "fps_demo"

#: Catalog rows used across the tests, all real products in D's seed. The
#: landed cost adds the sandbox shipping rate (HK$10) to the catalog price.
#:
#:   hp_0018  HK$279 wireless ANC  -> HK$289 landed   APPROVE under the demo mandate
#:   hp_0007  HK$300 wireless ANC  -> HK$310 landed   ESCALATE: over the ask-me line
#:   hp_0003  HK$499 wireless ANC  -> HK$509 landed   DENY: over the per-transaction cap
#:   hp_0005  HK$199, stock 0                          OUT_OF_STOCK at pricing time
HP_APPROVE = "hp_0018"
HP_ESCALATE = "hp_0007"
HP_OVER_CAP = "hp_0003"
HP_OUT_OF_STOCK = "hp_0005"


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh catalog plus C's tables and the demo wallet."""
    path = tmp_path / "commerce.sqlite3"
    initialize_database(
        path, commerce_schema=create_commerce_schema,
        wallet_initializer=initialize_wallets,
    )
    return path


@pytest.fixture
def commerce(db_path) -> CommerceService:
    return CommerceService(db_path=db_path)


@pytest.fixture
def payment(db_path) -> PaymentService:
    return PaymentService(db_path=db_path)


def a_draft(**overrides) -> MandateDraft:
    """A draft A could plausibly have collected, with every clause decided.

    The figures are chosen so the catalog produces one clean example of each
    outcome, which is what the plan's demo needs:

        cap HK$320, 24-hour rolling HK$600, 2 per 5 minutes, 3 items, 7 days,
        and "ask me above HK$300".
    """
    fields = dict(
        allowed_merchants=[MERCHANT],
        allowed_categories=["headphones"],
        required_connection="wireless",
        anc_required=True,
        cap_per_transaction_cents=32000,
        rolling_cap_cents=60000,
        rolling_window_seconds=86400,
        velocity_max_count=2,
        velocity_window_seconds=300,
        max_quantity_total=3,
        valid_for_seconds=604800,
        escalate_above_cents=30000,
        allowed_payment_routes=[ROUTE_ID],
        shipping_address_id=ADDRESS,
        address_change_allowed=False,
    )
    fields.update(overrides)
    return MandateDraft(**fields)


@pytest.fixture
def mandate(commerce):
    return commerce.activate_mandate(a_draft(), principal_id=PRINCIPAL, agent_id=AGENT,
                                     now=NOW)


@pytest.fixture
def roomy_mandate(commerce):
    """A mandate whose only binding constraint is the escalation threshold.

    The demo mandate's caps are deliberately tight enough to produce one example
    of each outcome, which also means a second purchase of the same product trips
    the rolling cap. A test about *approvals* needs those out of the way, so this
    one leaves room and asks to be consulted above HK$300 -- which is what makes
    the escalated product escalate for the reason under test rather than by
    coincidence.
    """
    return commerce.activate_mandate(
        a_draft(cap_per_transaction_cents=200000, rolling_cap_cents=1000000,
                escalate_above_cents=30000, max_quantity_total=10,
                velocity_max_count=10),
        principal_id=PRINCIPAL, agent_id=AGENT, now=NOW,
    )


def a_proposal(mandate, quote, *, proposal_id: str = "prop_0001",
               attempt: str = "1", **overrides) -> PurchaseProposal:
    """A proposal A could submit for this mandate and this quote.

    ``attempt`` is part of the idempotency key on purpose: a deliberate
    re-submission needs a fresh key, while a transport retry reuses one. The
    distinction is tested rather than assumed.
    """
    fields = dict(
        proposal_id=proposal_id,
        mandate_id=mandate.mandate_id,
        expected_mandate_version=mandate.version,
        principal_id=PRINCIPAL,
        agent_id=AGENT,
        product_id=quote.product_id,
        quantity=1,
        merchant_id=quote.merchant_id,
        quote_id=quote.quote_id,
        preferred_payment_route_ids=[ROUTE_ID],
        shipping_address_id=ADDRESS,
        request_id=new_request_id(),
        idempotency_key=f"idem_{proposal_id}_{attempt}",
        created_at=NOW,
    )
    fields.update(overrides)
    return PurchaseProposal(**fields)
