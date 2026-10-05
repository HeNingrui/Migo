"""C, as A sees it: the seven calls of the commerce boundary and three additions.

This class is the adapter between two shapes that must not merge. ``app/agent``
declares :class:`~app.agent.clients.CommerceClient`, a Protocol; this object
satisfies it structurally, so swapping the in-process implementation for an HTTP
client later is a change in ``app/main.py`` and nowhere else. Nothing in
``app/agent`` imports ``app.commerce``, and nothing here imports ``app.agent``.

**Three additions to the interface, each with a reason that is a defect rather
than a preference.** They are recorded in ``docs/A_C_contract_changes.md`` with
the issue, the alternatives and the impact; the short version:

* ``approve_escalation`` was narrowed from
  ``(decision, proposal, quote, route)`` to ``(proposal_id, principal_id)``. The
  old shape was not merely awkward, it was *unimplementable from A's side*: a
  :class:`~app.contracts.commerce.PaymentRouteEvaluation` is C's own derived
  object, the evaluator returns only a route *id* on the decision, and A has no
  call that produces one. A could only have obtained a route from the fake's
  test-only helper.
* ``reject_escalation`` is new. ``EscalationRequest.resolution`` is the one C
  object the ownership matrix lets C update, and the intent vocabulary already
  has ``REJECT_ESCALATION`` -- but there was no path to record the answer, so an
  escalation could only ever be approved or time out.
* ``get_proposal_outcome`` is new, and it is a read. ``DenialReceipt``,
  ``EscalationRequest``, ``Reservation`` and ``PaymentReceipt`` were all defined
  with no method returning them, so four of the five things a user is owed an
  explanation for had no path from C to A.

None of the seven original signatures changed. The additions are additive or
narrowing, and every one of them is exercised by the integration tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.commerce.audit import AuditLog
from app.commerce.authority import AuthorityService
from app.commerce.config import CATALOG_MERCHANT_ID
from app.commerce.database import read_connection, write_transaction
from app.commerce.escalation import EscalationService
from app.commerce.mandate_registry import MandateRegistry
from app.commerce.payment import PaymentService
from app.commerce.quote_service import SANDBOX_ROUTES, QuoteService
from app.commerce.repositories import (
    DecisionRepository,
    DenialRepository,
    EscalationRepository,
    PaymentAttemptRepository,
    ProposalRepository,
    ReservationRepository,
)
from app.commerce.spend_state import SpendStateService
from app.contracts.commerce import PurchaseProposal, Quote, SpendState
from app.contracts.common import ErrorCode, SourceType
from app.contracts.mandate import ApprovalGrant, Mandate, MandateDraft
from app.contracts.policy import (
    EscalationRequest,
    PaymentFailure,
    PolicyDecision,
    ProposalOutcome,
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class CommerceService:
    """C's application service. Implements ``app.agent.clients.CommerceClient``.

    Every method either takes a connection owning its own transaction (the
    writes) or uses a read-only one (the reads). The dependency graph is
    deliberately shallow -- registry, quote service, spend state, authority,
    escalation, payment -- and no layer reaches upward into the agent.
    """

    def __init__(self, *, db_path=None, audit: AuditLog | None = None,
                 payment: PaymentService | None = None) -> None:
        self._db_path = db_path
        self._audit = audit or AuditLog()

        self._mandates = MandateRegistry(db_path=db_path, audit=self._audit)
        self._quotes = QuoteService(db_path=db_path, audit=self._audit)
        self._spend_state = SpendStateService(db_path=db_path)
        self._escalations = EscalationService(audit=self._audit)
        self._authority = AuthorityService(
            db_path=db_path, audit=self._audit, spend_state=self._spend_state,
            escalations=self._escalations,
        )
        self._payment = payment or PaymentService(db_path=db_path, audit=self._audit)

        self._proposals = ProposalRepository()
        self._decisions = DecisionRepository()
        self._denials = DenialRepository()
        self._escalation_rows = EscalationRepository()
        self._reservations = ReservationRepository()
        self._attempts = PaymentAttemptRepository()

    # -- mandates -----------------------------------------------------------

    def activate_mandate(self, draft: MandateDraft, *, principal_id: str,
                         agent_id: str, now: datetime | None = None) -> Mandate:
        """Compile a signed draft into an authoritative mandate.

        C, not A, decides the id, the version, the canonical policy and the
        hash. A's draft is the *input*, and the audit record keeps the clauses
        the user agreed to beside the rules that were derived from them.

        ``now`` is injectable for the same reason ``submit_proposal``'s is: when
        a mandate becomes valid is a fact about the request, and a test or a
        scripted demo has to be able to state it rather than observe it.
        """
        return self._mandates.activate(draft, principal_id=principal_id,
                                       agent_id=agent_id, now=now)

    def get_mandate(self, mandate_id: str) -> Mandate | None:
        return self._mandates.current(mandate_id)

    def revoke_mandate(self, mandate_id: str, *,
                       now: datetime | None = None) -> Mandate:
        """Withdraw an authorisation. A new version, never an edit."""
        return self._mandates.revoke(mandate_id, now=now)

    def account_overview(self, principal_id: str) -> dict:
        """Account data for the server-selected principal; no write authority."""
        from app.commerce.repositories import WalletRepository
        with read_connection(self._db_path) as conn:
            balance = WalletRepository().balance(conn, principal_id)
            rows = conn.execute("""SELECT o.*, json_extract(q.payload, '$.product_name') AS product_name
                FROM orders o JOIN purchase_proposals p ON p.proposal_id=o.proposal_id
                JOIN quotes q ON q.quote_id=p.quote_id
                WHERE o.principal_id=? ORDER BY o.created_at DESC, o.order_id DESC LIMIT 50""",
                (principal_id,)).fetchall()
            return {"principal_id": principal_id, "currency": "HKD", "balance_cents": balance,
                    "orders": [dict(row) for row in rows], "settlement_mode": "simulation"}

    # -- pricing ------------------------------------------------------------

    def create_quote(self, *, product_id: str, quantity: int = 1,
                     now: datetime | None = None) -> Quote:
        """Price a basket. A supplies an identifier and a count, never an amount."""
        return self._quotes.create(product_id=product_id, quantity=quantity, now=now)

    def spend_state(self, mandate: Mandate, *,
                    now: datetime | None = None) -> SpendState:
        """Exposure against one mandate, read from C's own records.

        The mandate is re-read by id rather than trusted as passed in. A holds a
        cached copy for display, and a cached copy is not authority: a mandate
        that was revoked since A last looked has to be the one this answer
        describes.

        ``now`` is injectable because two of the three numbers here are windowed
        and the window is measured from the evaluation instant. A caller that
        cannot state the instant cannot ask a question whose answer is stable.
        """
        from app.errors import AgentError

        current = self._mandates.current(mandate.mandate_id)
        if current is None:
            raise AgentError(
                ErrorCode.MANDATE_NOT_FOUND,
                f"no mandate {mandate.mandate_id!r}",
                details={"mandate_id": mandate.mandate_id},
            )
        return self._spend_state.snapshot(current, now=now)

    # -- purchase -----------------------------------------------------------

    def submit_proposal(self, proposal: PurchaseProposal, *,
                        now: datetime | None = None) -> PolicyDecision:
        """Evaluate, and act on the outcome.

        On APPROVE, C creates the reservation and then settles it: issues a
        capability, spends it, calls the rail and records the receipt. A is not
        given a way to trigger any of that, which is the plan's rule -- the only
        path to the money is inside C, and A's contribution is a proposal that
        carries no authority.

        The return value stays a :class:`PolicyDecision` because that is the
        frozen interface; what happened afterwards is read back through
        :meth:`get_proposal_outcome`.
        """
        result = self._authority.submit(proposal, now=now)

        reservation = result.reservation
        if result.decision.outcome == "APPROVE" and reservation is not None \
                and reservation.status == "ACTIVE":
            self._payment.pay_reservation(reservation.reservation_id, now=now)
        return result.decision

    def approve_escalation(self, proposal_id: str, *, principal_id: str,
                           now: datetime | None = None) -> ApprovalGrant:
        """Record the principal's answer to an escalation, and grant it once.

        A lapsed question is closed first, in its own transaction. Doing it
        inside the answer's transaction would roll the resolution back when the
        refusal is raised, leaving the record open -- so the deadline would be
        enforced in the error message and nowhere else.
        """
        with write_transaction(self._db_path) as conn:
            self._escalations.close_if_expired(conn, proposal_id=proposal_id,
                                               now=now or _utc())
        with write_transaction(self._db_path) as conn:
            return self._escalations.approve(
                conn, proposal_id=proposal_id, principal_id=principal_id,
                now=now or _utc(),
            )

    def reject_escalation(self, proposal_id: str, *, principal_id: str,
                          now: datetime | None = None) -> EscalationRequest:
        """Record a no. No grant is issued and the purchase does not happen."""
        with write_transaction(self._db_path) as conn:
            self._escalations.close_if_expired(conn, proposal_id=proposal_id,
                                               now=now or _utc())
        with write_transaction(self._db_path) as conn:
            return self._escalations.reject(
                conn, proposal_id=proposal_id, principal_id=principal_id,
                now=now or _utc(),
            )

    # -- reading back -------------------------------------------------------

    def get_proposal_outcome(self, proposal_id: str) -> ProposalOutcome | None:
        """Everything C recorded about one proposal. ``None`` if it never saw it.

        A read model, assembled from C's own rows: calling it twice returns the
        same answer and changes nothing. It is how A learns about a denial, an
        open question, a reservation and a receipt -- none of which the
        ``PolicyDecision`` returned by ``submit_proposal`` can carry.
        """
        with read_connection(self._db_path) as conn:
            if self._proposals.get(conn, proposal_id) is None:
                return None
            decision = self._decisions.latest(conn, proposal_id)
            if decision is None:  # pragma: no cover - a proposal implies a decision
                return None

            reservation = self._reservations.for_proposal(conn, proposal_id)
            receipt = self._attempts.latest_settled(conn, proposal_id)
            failure = self._failure(conn, proposal_id)

            return ProposalOutcome(
                proposal_id=proposal_id,
                decision=decision,
                denial=self._denials.get(conn, proposal_id),
                escalation=self._escalation_rows.get(conn, proposal_id),
                reservation=reservation,
                receipt=receipt,
                payment_failure=failure,
                settlement_source_type=(
                    self._payment.source_type if (receipt or failure) else None
                ),
            )

    def _failure(self, conn, proposal_id: str) -> PaymentFailure | None:
        """The last refusal for this proposal, as a contract object.

        Rebuilt from the attempt row rather than stored twice, so the record A
        explains and the record C keeps cannot disagree. A settled attempt has no
        failure row to find, and a proposal that never reached the rail has
        neither.
        """
        row = self._attempts.latest_failure(conn, proposal_id)
        if row is None:
            return None
        return PaymentFailure(
            failure_id=row["attempt_id"],
            proposal_id=proposal_id,
            reservation_id=row["reservation_id"],
            code=ErrorCode(row["code"]),
            message=row["message"] or "the payment did not complete",
            retryable=bool(row["retryable"]),
            provider_reference=row["provider_reference"],
            failed_at=datetime.fromisoformat(row["attempted_at"]),
        )

    # -- facts A cannot invent ----------------------------------------------

    def merchant_of_record(self) -> str:
        """The merchant this catalog is bought from.

        A mandate must name the merchants it permits, and A has nowhere else to
        get one: ``Product`` carries no merchant and neither does
        ``SearchResponse``, which is the D-09 SPEC GAP. C owns the merchant of
        record for the catalog it prices from -- it already puts the same value
        on every ``Quote`` -- so this is where the fact lives until B carries it
        per search result, at which point this method is deleted and A reads it
        from the candidate instead.
        """
        return CATALOG_MERCHANT_ID

    def payment_routes(self) -> list[str]:
        """The rails this commerce layer can actually settle on.

        Same reasoning as :meth:`merchant_of_record`: a mandate that allowed only
        routes nobody implements would activate and then refuse every purchase,
        and A cannot know which ones exist. Sorted, so a mandate compiled from it
        is reproducible.
        """
        return sorted(SANDBOX_ROUTES)

    # -- introspection ------------------------------------------------------

    @property
    def adapter_name(self) -> str:
        """Which payment rail settles purchases here. ``sandbox``, currently."""
        return self._payment.adapter_name

    @property
    def settlement_source_type(self) -> SourceType:
        """How a settlement from this configuration must be labelled."""
        return self._payment.source_type

    def audit_events(self, mandate_id: str) -> list:
        """The chain for one mandate, for the verification view."""
        with read_connection(self._db_path) as conn:
            return self._audit.events_for_mandate(conn, mandate_id)

    def audit_events_for_proposal(self, proposal_id: str) -> list:
        with read_connection(self._db_path) as conn:
            return self._audit.events_for_proposal(conn, proposal_id)

    def verify_audit_chain(self):
        """Recompute the whole chain. A third party's check, runnable by anyone."""
        with read_connection(self._db_path) as conn:
            return self._audit.verify(conn)


__all__ = ["CommerceService"]
