"""Commerce layer: mandate, quote, policy, reservation, capability, payment.

Ownership boundary (see docs/A_C_data_handoff.md):

    A proposes.  C decides.  C reserves.  C pays.  C records.  A explains.

Nothing in this package may be reached from ``app/agent`` except through the
explicit application services named in that document. ``CommerceService`` is the
one object A holds, and it satisfies ``app.agent.clients.CommerceClient``
structurally, so replacing this package with a remote implementation is a change
in the composition root and nowhere else.

Layout:

    config.py            sandbox rates, demo identities, lifetimes
    database.py          the connection, the transaction, the id
    schema.py            C's tables, created through D's hook
    repositories.py      one repository per aggregate; no SQL above it
    audit.py             the append-only hash chain
    mandate_registry.py  draft -> immutable hashed mandate; revocation
    quote_service.py     pricing a basket, and pricing a rail
    spend_state.py       exposure from C's own records
    policy_evaluator.py  the pure decision function (unchanged)
    authority.py         evaluate + reserve, in one transaction
    escalation.py        asking the principal, and spending the answer once
    capability.py        authority over one transaction
    payment.py           settlement, the ledger, the receipt
    reconcile.py         read-only report on holds with no recorded outcome
    service.py           the boundary A calls
"""

from app.commerce.service import CommerceService

__all__ = ["CommerceService"]
