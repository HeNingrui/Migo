"""A's only exits: the two boundaries, and nothing else.

``app/agent/`` reaches B and C through exactly one module, so a test can
substitute them and an integration change lands in one file. The orchestrator
imports these Protocols and never a concrete client.

Three rules the real implementations inherit:

* **B is read-only.** There is deliberately no path from here to C's write
  endpoints other than ``submit_proposal``, which carries no authority of its
  own -- the decision comes back, it is not made.
* **C is never re-implemented.** A's own code contains no policy.
* **Nothing here can approve, reserve, issue a capability or settle.** The
  methods that change C's state say what A is asking for; what happens is C's
  answer. ``submit_proposal`` returns a decision, and the reservation and the
  payment that follow an approval happen inside C with no call from A at all.

C's working implementation is ``app.commerce.CommerceService``, which satisfies
:class:`CommerceClient` structurally. B's stand-in lives next door in
``local_search``, in its own file so that the scaffolding is obvious and easy to
remove.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.contracts.commerce import (
    PurchaseProposal,
    Quote,
    SpendState,
)
from app.contracts.mandate import ApprovalGrant, Mandate, MandateDraft
from app.contracts.policy import EscalationRequest, PolicyDecision, ProposalOutcome
from app.contracts.search import SearchRequest, SearchResponse


@runtime_checkable
class SearchClient(Protocol):
    """B, as A sees it. Read-only, and the only source of candidates."""

    def search(self, request: SearchRequest) -> SearchResponse:
        ...


@runtime_checkable
class CommerceClient(Protocol):
    """C, as A sees it.

    ``runtime_checkable`` buys one check and nothing more: ``isinstance`` on an
    object confirms it has the methods, so the composition root can refuse a
    client that is missing one instead of failing on the first turn that needs
    it. It does **not** check signatures -- ``isinstance`` never does -- so the
    signatures are pinned separately by the tests, which is the only place they
    can be.

    Every method here is a *question* or a *submission*. None of them lets A
    approve anything: ``submit_proposal`` returns C's decision, it does not take
    one from the caller.

    The roster below is the original seven plus three, and the reason each
    addition exists is recorded in ``docs/A_C_contract_changes.md``. In short:

    * ``approve_escalation`` was **narrowed**. It used to demand a
      ``PolicyDecision``, a ``PurchaseProposal``, a ``Quote`` and a
      ``PaymentRouteEvaluation`` -- three of them C's own products, and one of
      them, the route evaluation, an object A has no call to obtain at all. A
      supplied two identifiers now and C looks up its own context.
    * ``reject_escalation`` is new. The intent has always existed and
      ``EscalationRequest.resolution`` was always C's to write, but there was no
      path to record a no, so the only answers were yes and silence.
    * ``get_proposal_outcome`` is new and is a **read**. ``DenialReceipt``,
      ``EscalationRequest``, ``Reservation`` and ``PaymentReceipt`` were defined
      with no method returning them.
    * ``merchant_of_record`` and ``payment_routes`` are new and are **reads**.
      A mandate must name the merchants and rails it permits, and A cannot
      invent either: they are facts about the catalog C prices from. This is the
      D-09 SPEC GAP made explicit at the boundary instead of hidden in A's
      configuration. When B carries a merchant on each search result, the first
      of these disappears.

    Optional ``now`` keyword arguments were appended to four calls so that a
    test or a scripted demo can state its instant. Appended and keyword-only, so
    no existing positional call site changes meaning.
    """

    # -- authorisation ------------------------------------------------------

    def activate_mandate(
        self, draft: MandateDraft, *, principal_id: str, agent_id: str
    ) -> Mandate:
        ...

    def get_mandate(self, mandate_id: str) -> Mandate | None:
        ...

    def revoke_mandate(self, mandate_id: str) -> Mandate:
        ...

    # -- pricing ------------------------------------------------------------

    def create_quote(self, *, product_id: str, quantity: int = 1) -> Quote:
        ...

    def spend_state(self, mandate: Mandate) -> SpendState:
        ...

    # -- purchase -----------------------------------------------------------

    def submit_proposal(
        self, proposal: PurchaseProposal, *, now: datetime | None = None
    ) -> PolicyDecision:
        ...

    def approve_escalation(
        self, proposal_id: str, *, principal_id: str
    ) -> ApprovalGrant:
        ...

    def reject_escalation(
        self, proposal_id: str, *, principal_id: str
    ) -> EscalationRequest:
        ...

    def get_proposal_outcome(self, proposal_id: str) -> ProposalOutcome | None:
        ...

    # -- facts A cannot invent ---------------------------------------------

    def merchant_of_record(self) -> str:
        ...

    def payment_routes(self) -> list[str]:
        ...


__all__ = [
    "CommerceClient",
    "SearchClient",
]
