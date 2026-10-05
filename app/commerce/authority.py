"""The authority boundary: one proposal in, one decision and its consequences out.

This is the transaction the plan calls the critical one, and the shape of it is
the answer to a specific race: two proposals, each approved, together
overspending a rolling cap.

    BEGIN IMMEDIATE
        load proposal, mandate, quote, route, spend state
        decision = evaluate_policy(...)          <- pure, no IO
        DENY      -> write the denial receipt, no reservation
        ESCALATE  -> write the question, no reservation
        APPROVE   -> write the reservation
    COMMIT

Everything between the load and the reservation happens under one lock, which is
why ``SpendStateService`` takes a connection instead of opening one. If the
exposure read and the reservation write were separable, both proposals would
observe the same remaining budget and both would be approved -- the evaluator
would be correct and the system would still overspend.

Three consequences of that ordering are worth naming.

**The decision is recorded before it is acted on, and never instead of it.** A
proposal can legitimately have several decisions: one that escalated, and one
that approved after the principal answered. Both rows are kept, because
overwriting the first would erase the evidence that the agent stopped.

**A proposal is one purchase attempt.** It gets at most one reservation, ever,
and a released one is not re-usable: if the payment was refused, the attempt is
over and a new purchase needs a new proposal with a fresh quote. That is what
stops a retry loop from turning one refusal into two charges.

**Idempotency distinguishes a replay from a conflict.** The same
``idempotency_key`` with the same request digest is a replay and returns what was
recorded, changing nothing. The same key with a different digest is a conflict
and is refused. An approval grant is *not* consumed by the replay path, which is
what makes "approved once, used once" hold even if the caller submits twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.commerce.audit import AuditLog
from app.commerce.config import RESERVATION_TTL_SECONDS
from app.commerce.database import new_id, write_transaction
from app.commerce.escalation import EscalationService, deny_on_timeout
from app.commerce.policy_evaluator import PolicyInputs, as_utc, evaluate_policy
from app.commerce.quote_service import SandboxPaymentRouter
from app.commerce.repositories import (
    DecisionRepository,
    DenialRepository,
    EscalationRepository,
    GrantRepository,
    MandateRepository,
    ProposalRepository,
    QuoteRepository,
    ReservationRepository,
)
from app.commerce.spend_state import SpendStateService
from app.contracts.audit import AuditEventType
from app.contracts.commerce import PurchaseProposal, Reservation
from app.contracts.common import ActorType, ErrorCode
from app.contracts.policy import DenialReceipt, EscalationRequest, PolicyDecision

#: Actor C records as the author of a proposal's arrival. The agent asked; C is
#: what received the ask, and conflating the two would make the audit chain say
#: the commerce layer decided to buy something.
_AGENT_ACTOR = "agent"
_EVALUATOR_ACTOR = "policy_evaluator"
_RESERVATION_ACTOR = "reservation_service"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AuthorityDecision:
    """What one submission produced, in C's own contract types."""

    decision: PolicyDecision
    denial: DenialReceipt | None = None
    escalation: EscalationRequest | None = None
    reservation: Reservation | None = None

    #: True when C recognised the submission and returned what it had already
    #: recorded instead of evaluating anything again.
    replayed: bool = False


class AuthorityService:
    """Evaluates proposals and performs the act each outcome calls for."""

    def __init__(self, *, db_path=None, audit: AuditLog | None = None,
                 spend_state: SpendStateService | None = None,
                 escalations: EscalationService | None = None,
                 router: SandboxPaymentRouter | None = None) -> None:
        self._db_path = db_path
        self._audit = audit or AuditLog()
        self._spend_state = spend_state or SpendStateService(db_path=db_path)
        self._escalations = escalations or EscalationService(audit=self._audit)
        self._router = router or SandboxPaymentRouter()

        self._proposals = ProposalRepository()
        self._mandates = MandateRepository()
        self._quotes = QuoteRepository()
        self._decisions = DecisionRepository()
        self._denials = DenialRepository()
        self._escalation_rows = EscalationRepository()
        self._grants = GrantRepository()
        self._reservations = ReservationRepository()

    # -- the one entry point ------------------------------------------------

    def submit(self, proposal: PurchaseProposal, *,
               now: datetime | None = None) -> AuthorityDecision:
        """Evaluate one proposal and do whatever the outcome requires."""
        from app.errors import AgentError

        moment = as_utc(now or _utc())
        incoming = proposal.request_hash()

        with write_transaction(self._db_path) as conn:
            replay = self._proposals.by_idempotency_key(conn, proposal.idempotency_key)
            if replay is not None:
                if replay.request_hash != incoming:
                    # Same key, different purchase: refusing is the only safe
                    # answer, because returning the first one's result would
                    # report a decision about something else.
                    raise AgentError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "this idempotency key was used for a different proposal",
                        details={"proposal_id": proposal.proposal_id,
                                 "recorded_proposal_id": replay.proposal.proposal_id},
                    )
                return self._recorded(conn, replay.proposal.proposal_id, replayed=True)

            recorded = self._proposals.request_hash(conn, proposal.proposal_id)
            if recorded is None:
                self._proposals.add(conn, proposal, request_hash=incoming,
                                    submitted_at=moment)
                self._audit.append(
                    conn, event_type=AuditEventType.PROPOSAL_CREATED,
                    actor_type=ActorType.AGENT, actor_id=proposal.agent_id,
                    occurred_at=moment, mandate_id=proposal.mandate_id,
                    proposal_id=proposal.proposal_id,
                    payload={
                        "product_id": proposal.product_id,
                        "quantity": proposal.quantity,
                        "merchant_id": proposal.merchant_id,
                        "quote_id": proposal.quote_id,
                        "expected_mandate_version": proposal.expected_mandate_version,
                        "idempotency_key": proposal.idempotency_key,
                    },
                )
            elif recorded != incoming:
                # The same proposal_id may be submitted again after an escalation
                # was answered -- that is the retry the approval exists for. What
                # it may not do is become a different purchase.
                raise AgentError(
                    ErrorCode.CONFLICT,
                    "a proposal cannot change what it asks for; submit a new one",
                    details={"proposal_id": proposal.proposal_id},
                )

            mandate = self._mandates.current(conn, proposal.mandate_id)
            if mandate is None:
                raise AgentError(
                    ErrorCode.MANDATE_NOT_FOUND,
                    f"no mandate {proposal.mandate_id!r}",
                    details={"mandate_id": proposal.mandate_id},
                )
            quote = self._quotes.get(conn, proposal.quote_id)
            if quote is None:
                raise AgentError(
                    ErrorCode.QUOTE_NOT_FOUND,
                    f"no quote {proposal.quote_id!r}",
                    details={"quote_id": proposal.quote_id},
                )

            # A lapsed question is closed before the evaluation, so the
            # evaluator's ESCALATE can be turned into the refusal DC12 requires
            # without ever recording a second decision.
            timed_out = self._escalations.close_if_expired(
                conn, proposal_id=proposal.proposal_id, now=moment)

            route = self._router.choose(
                quote, mandate, list(proposal.preferred_payment_route_ids))
            spend_state = self._spend_state.compute(conn, mandate=mandate, now=moment)
            grant = self._grants.usable_for(conn, proposal.proposal_id)

            decision = evaluate_policy(PolicyInputs(
                proposal=proposal,
                mandate=mandate,
                quote=quote,
                route=route,
                spend_state=spend_state,
                now=moment,
                approval_grant=grant,
            ))
            if decision.outcome == "ESCALATE" and timed_out is not None:
                decision = deny_on_timeout(decision, timed_out, moment)

            self._decisions.add(conn, new_id("dec"), decision)
            self._audit.append(
                conn, event_type=AuditEventType.POLICY_EVALUATED,
                actor_type=ActorType.COMMERCE, actor_id=_EVALUATOR_ACTOR,
                occurred_at=moment, mandate_id=mandate.mandate_id,
                proposal_id=proposal.proposal_id,
                payload={
                    "outcome": decision.outcome,
                    "primary_reason": (decision.primary_reason.value
                                       if decision.primary_reason else None),
                    "policy_hash": decision.policy_hash,
                    "cash_total_cents": decision.cash_total_cents,
                    "payment_route_id": decision.payment_route_id,
                    "violation_codes": [v.code.value for v in decision.violations],
                    "observed_values": decision.observed_values,
                    "applicable_limits": decision.applicable_limits,
                },
            )

            if decision.outcome == "DENY":
                return self._deny(conn, decision, moment)
            if decision.outcome == "ESCALATE":
                escalation = self._escalations.raise_for(
                    conn, decision=decision, mandate=mandate, now=moment)
                self._audit.append(
                    conn, event_type=AuditEventType.DECISION_ESCALATED,
                    actor_type=ActorType.COMMERCE, actor_id=_EVALUATOR_ACTOR,
                    occurred_at=moment, mandate_id=mandate.mandate_id,
                    proposal_id=proposal.proposal_id,
                    payload={"escalation_id": escalation.escalation_id,
                             "cash_total_cents": escalation.cash_total_cents},
                )
                return AuthorityDecision(decision=decision, escalation=escalation)
            return self._approve(conn, decision=decision, proposal=proposal, quote=quote,
                                 route=route, grant=grant, moment=moment)

    # -- outcomes -----------------------------------------------------------

    def _deny(self, conn, decision: PolicyDecision,
              moment: datetime) -> AuthorityDecision:
        """Record the refusal. No reservation is created, and the model says so."""
        event = self._audit.append(
            conn, event_type=AuditEventType.DECISION_DENIED,
            actor_type=ActorType.COMMERCE, actor_id=_EVALUATOR_ACTOR,
            occurred_at=moment, mandate_id=decision.mandate_id,
            proposal_id=decision.proposal_id,
            payload={
                "primary_reason": (decision.primary_reason.value
                                   if decision.primary_reason else None),
                "violation_codes": [v.code.value for v in decision.violations],
            },
        )
        denial = DenialReceipt(
            denial_id=new_id("den"),
            proposal_id=decision.proposal_id,
            mandate_id=decision.mandate_id,
            mandate_version=decision.mandate_version,
            policy_hash=decision.policy_hash,
            primary_reason=decision.primary_reason or ErrorCode.INTERNAL_ERROR,
            violations=list(decision.violations),
            observed_values=dict(decision.observed_values),
            applicable_limits=dict(decision.applicable_limits),
            quote_id=decision.quote_id,
            cash_total_cents=decision.cash_total_cents,
            currency=decision.currency,
            reservation_created=False,
            payment_adapter_called=False,
            created_at=moment,
            audit_event_id=event.event_id,
        )
        self._denials.add(conn, denial)
        return AuthorityDecision(decision=decision, denial=denial)

    def _approve(self, conn, *, decision: PolicyDecision, proposal: PurchaseProposal,
                 quote, route, grant, moment: datetime) -> AuthorityDecision:
        """Create the reservation, and spend the approval that made it possible.

        The grant is consumed whenever one is on file, not only when escalation
        is what tipped the decision. Either the approval is what allowed this
        purchase, in which case it has now been used, or it was not needed --
        and in that case leaving a live single-use approval on file is a token
        somebody can present later. Consuming it is the safe direction, and the
        grant is bound to this quote hash, this route and this amount, so it
        could not have been used for anything else.
        """
        existing = self._reservations.for_proposal(conn, decision.proposal_id)
        if existing is not None:
            # At most one reservation per proposal, ever. A second approval of
            # the same purchase would be a second charge.
            return AuthorityDecision(decision=decision, reservation=existing,
                                     replayed=True)

        if grant is not None:
            consumed = self._grants.consume(
                conn, proposal_id=decision.proposal_id, consumed_at=moment)
            if consumed is None:  # pragma: no cover - the transaction holds the row
                raise RuntimeError(
                    "the approval grant vanished between reading and consuming it"
                )

        reservation = Reservation(
            reservation_id=new_id("res"),
            proposal_id=decision.proposal_id,
            principal_id=proposal.principal_id,
            mandate_id=decision.mandate_id,
            mandate_version=decision.mandate_version,
            quote_id=quote.quote_id,
            quote_hash=quote.quote_hash,
            payment_route_id=route.route_id,
            amount_cents=decision.cash_total_cents,
            currency=quote.currency,
            quantity=proposal.quantity,
            status="ACTIVE",
            created_at=moment,
            expires_at=moment + timedelta(seconds=RESERVATION_TTL_SECONDS),
        )
        self._reservations.add(conn, reservation)
        self._audit.append(
            conn, event_type=AuditEventType.DECISION_APPROVED,
            actor_type=ActorType.COMMERCE, actor_id=_EVALUATOR_ACTOR,
            occurred_at=moment, mandate_id=decision.mandate_id,
            proposal_id=decision.proposal_id,
            payload={
                "cash_total_cents": decision.cash_total_cents,
                "payment_route_id": decision.payment_route_id,
                "approval_grant_consumed": grant is not None,
            },
        )
        self._audit.append(
            conn, event_type=AuditEventType.RESERVATION_CREATED,
            actor_type=ActorType.COMMERCE, actor_id=_RESERVATION_ACTOR,
            occurred_at=moment, mandate_id=decision.mandate_id,
            proposal_id=decision.proposal_id,
            reservation_id=reservation.reservation_id,
            payload={
                "amount_cents": reservation.amount_cents,
                "quantity": reservation.quantity,
                "quote_hash": reservation.quote_hash,
                "payment_route_id": reservation.payment_route_id,
                "expires_at": reservation.expires_at.isoformat(),
            },
        )
        return AuthorityDecision(decision=decision, reservation=reservation)

    # -- replay -------------------------------------------------------------

    def _recorded(self, conn, proposal_id: str, *, replayed: bool) -> AuthorityDecision:
        """Reassemble what C already recorded, without touching anything."""
        from app.errors import AgentError

        decision = self._decisions.latest(conn, proposal_id)
        if decision is None:  # pragma: no cover - a replay implies a decision
            raise AgentError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "the recorded proposal has no decision to return",
                details={"proposal_id": proposal_id},
            )
        return AuthorityDecision(
            decision=decision,
            denial=self._denials.get(conn, proposal_id),
            escalation=self._escalation_rows.get(conn, proposal_id),
            reservation=self._reservations.for_proposal(conn, proposal_id),
            replayed=replayed,
        )


__all__ = ["AuthorityDecision", "AuthorityService"]
