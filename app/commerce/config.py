"""C's parameters, and the labels that keep them from being read as market data.

Everything here is either a demo identity or a fictional rate. The naming rule
is deliberate and load-bearing: ``SANDBOX_`` marks a *rate* that exists nowhere
in the product data, ``DEMO_`` marks an *identity* that a real deployment would
take from configuration or an upstream system. A value that is neither is a
value C derived, and it does not belong in this module.

Why it is a module rather than constants scattered through the services: the
problem statement treats a fabricated rate as fabrication, so every one of these
has to be findable by grep and quotable in ``docs/evidence_sources.md``. A
figure that is only ever an inline literal is a figure nobody audits.

Two of them are overridable by environment so a demo can be replayed at a
different size without editing code; the rest are fixed, because a demo that
silently changes its wallet balance between runs is not a demo.
"""

from __future__ import annotations

import os
import secrets

# ---------------------------------------------------------------------------
# Fictional rates -- SANDBOX
# ---------------------------------------------------------------------------
#
# The catalog has a price and nothing else: no shipping table, no tax rule, no
# fee schedule. These two numbers are the whole of C's costing model, and they
# are the reason every quote carries ``source_type = SANDBOX``.

#: Flat shipping, applied per quote regardless of quantity or origin. There is
#: no shipping field in the product data, so this cannot be derived.
SANDBOX_SHIPPING_CENTS = int(os.environ.get("SANDBOX_SHIPPING_CENTS", "1000"))

#: The platform fee charged by the sandbox rail. Zero, and recorded as zero
#: rather than omitted, so a receipt's arithmetic still reconciles.
SANDBOX_PLATFORM_FEE_CENTS = int(os.environ.get("SANDBOX_PLATFORM_FEE_CENTS", "0"))

#: No tax rule exists for the fictional catalog.
SANDBOX_TAX_CENTS = 0

#: No discount programme exists yet.
SANDBOX_DISCOUNT_CENTS = 0

# ---------------------------------------------------------------------------
# Demo identities -- DEMO
# ---------------------------------------------------------------------------
#
# These are the values a live deployment would read from a merchant registry,
# a route registry and an address book. There is exactly one of each because
# the catalog is one synthetic store.

#: The single merchant of record for the synthetic catalog.
#:
#: SPEC GAP (D-09 / H-C-03h): neither ``Product`` nor ``SearchResponse``
#: carries a merchant, so A cannot obtain one from B. C owns the merchant of
#: record for the catalog it prices from and returns it on the ``Quote``; A
#: copies it back into the ``PurchaseProposal`` and C re-checks it against the
#: mandate. When the catalog becomes multi-merchant this constant becomes a
#: product-level field, and the only code that changes is this module plus
#: ``QuoteService``.
CATALOG_MERCHANT_ID = "demo_audio_store"

#: The one sandbox payment rail. A real deployment reads a route registry.
DEMO_PAYMENT_ROUTE_ID = "fps_demo"

#: The one demo shipping address.
DEMO_SHIPPING_ADDRESS_ID = "addr_demo_01"

#: Opening wallet balance for the demo principal, in minor units. Seeded once
#: by ``initialize_wallets`` and never overwritten, so spending accumulates
#: across runs the way a real ledger would.
DEMO_WALLET_OPENING_BALANCE_CENTS = int(
    os.environ.get("DEMO_WALLET_OPENING_BALANCE_CENTS", "500000")
)

#: The demo principal. A real deployment takes this from authenticated context.
DEMO_PRINCIPAL_ID = os.environ.get("DEMO_PRINCIPAL_ID", "demo_user")

# ---------------------------------------------------------------------------
# Lifetimes
# ---------------------------------------------------------------------------
#
# Every one of these is a fail-closed deadline: past it, the artefact is refused
# rather than renewed implicitly.

#: A quote is a priced offer with a short life. Past it the evaluator refuses
#: with QUOTE_EXPIRED and A must re-price.
QUOTE_TTL_SECONDS = int(os.environ.get("COMMERCE_QUOTE_TTL_SECONDS", "900"))

#: How long an unanswered escalation stays open. DC12: past it, DENY.
ESCALATION_TTL_SECONDS = int(os.environ.get("COMMERCE_ESCALATION_TTL_SECONDS", "300"))

#: How long an approval grant stays usable. Short, because it authorises one
#: specific amount against one specific route.
GRANT_TTL_SECONDS = int(os.environ.get("COMMERCE_GRANT_TTL_SECONDS", "300"))

#: How long a payment capability is valid. It is consumed inside one settlement
#: attempt, so this only bounds a crash between issue and use.
CAPABILITY_TTL_SECONDS = int(os.environ.get("COMMERCE_CAPABILITY_TTL_SECONDS", "120"))

#: How long budget stays held if settlement never completes. A reservation that
#: outlives this is released, which is what stops a crashed attempt from
#: freezing the mandate's headroom forever.
RESERVATION_TTL_SECONDS = int(os.environ.get("COMMERCE_RESERVATION_TTL_SECONDS", "900"))

# ---------------------------------------------------------------------------
# Capability signing
# ---------------------------------------------------------------------------

#: Environment variable holding the HMAC key for payment capabilities (DC2:
#: stdlib ``hmac``, no third-party JWT library).
CAPABILITY_SECRET_ENV = "COMMERCE_CAPABILITY_SECRET"

#: Generated once per process when the environment does not supply a key.
#:
#: A per-process key is the safe default, not a shortcut: a capability is
#: issued and consumed inside one settlement attempt, so nothing needs to
#: survive a restart, and a key that never reaches disk cannot leak from a
#: committed file. It does mean an outstanding capability is void after a
#: restart, which is the correct direction to fail. Never hard-code a key here;
#: a literal in a repository is a literal in every deployment.
_PROCESS_CAPABILITY_SECRET = secrets.token_hex(32)


def capability_secret() -> bytes:
    """The HMAC key used to sign and verify payment capabilities."""
    configured = os.environ.get(CAPABILITY_SECRET_ENV)
    if configured:
        return configured.encode("utf-8")
    return _PROCESS_CAPABILITY_SECRET.encode("ascii")


__all__ = [
    "CAPABILITY_SECRET_ENV",
    "CAPABILITY_TTL_SECONDS",
    "CATALOG_MERCHANT_ID",
    "DEMO_PAYMENT_ROUTE_ID",
    "DEMO_PRINCIPAL_ID",
    "DEMO_SHIPPING_ADDRESS_ID",
    "DEMO_WALLET_OPENING_BALANCE_CENTS",
    "ESCALATION_TTL_SECONDS",
    "GRANT_TTL_SECONDS",
    "QUOTE_TTL_SECONDS",
    "RESERVATION_TTL_SECONDS",
    "SANDBOX_DISCOUNT_CENTS",
    "SANDBOX_PLATFORM_FEE_CENTS",
    "SANDBOX_SHIPPING_CENTS",
    "SANDBOX_TAX_CENTS",
    "capability_secret",
]
