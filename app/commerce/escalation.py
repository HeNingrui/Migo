"""Escalation: the agent stops and asks, and the answer is recorded once.

Escalation is the only outcome that asks the principal instead of refusing, and
almost all of its difficulty is in what happens *after* the question.

**An unanswered question is a refusal (DC12).** An escalation has a deadline,
and past it the purchase does not proceed. The plan is explicit that this is
fail-closed: "the agent asked and nobody answered" must never decay into "the
agent went ahead". That is why :meth:`EscalationService.close_if_expired`
resolves the record as ``TIMEOUT`` rather than leaving it open, and why
:func:`deny_on_timeout` exists.

**The approval is a grant, and a grant is spent once.** The evaluator can only
read a grant, so single use cannot be enforced there. It is enforced in
``approval_grants``, by a conditional UPDATE inside the same transaction that
creates the reservation the approval pays for. An approval that could be
replayed would turn "ask me above HK$280" into "ask me once, ever".

**``deny_on_timeout`` is a state transition, not a second evaluator.** It runs
only when the evaluator has already returned ``ESCALATE`` and C has established
that the question it wanted to ask has expired unanswered. No rule is
re-evaluated and no threshold is compared: the decision is rewritten to say what
C is actually going to do, which is refuse. That distinction is the reason this
function lives here rather than as a branch inside ``evaluate_policy`` -- the
evaluator stays a pure function of the facts it was handed, and it never learns
what time it is or what happened before.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.commerce.audit import AuditLog
from app.commerce.config import GRANT_TTL_SECONDS
from app.commerce.database import new_id
from app.commerce.policy_evaluator import as_utc
from app.commerce.repositories import (
    DecisionRepository,
    EscalationRepository,
    GrantRepository,
    ProposalRepository,
)
from app.contracts.audit import AuditEventType
from app.contracts.common import ActorType, ErrorCode
from app.contracts.mandate import ApprovalGrant
from app.contracts.policy import EscalationRequest, PolicyDecision, PolicyViolation

#: The question C records. Facts only: A renders the amounts from
#: ``cash_total_cents`` and ``escalate_above_cents``, so the wording here carries
#: no number and no formatting decision.
ESCALATION_QUESTION = "这笔购买超过了你授权时设定的金额上限，需要你确认后才能继续。"

_ACTOR = "escalation_service"


class EscalationService:
    """Raises escalations, records answers, and issues the grant an answer buys."""

    def __init__(self, *, audit: AuditLog | None = None) -> None:
        self._audit = audit or AuditLog()
        self._escalations = EscalationRepository()
        self._grants = GrantRepository()
        self._proposals = ProposalRepository()
        self._decisions = DecisionRepository()

    # -- raising ------------------------------------------------------------

    def raise_for(self, conn, *, decision: PolicyDecision, mandate,
                  now: datetime) -> EscalationRequest:
        """Record the question. Idempotent per proposal.

        Returns the escalation already on file when there is one, so a
        re-submission that escalates a second time does not silently fail to
        write a second row: ``proposal_id`` is unique, and an insert that hit
        the conflict would report success for a row that was never created.
        """
        from app.errors import AgentError

        existing = self._escalations.get(conn, decision.proposal_id)
        if existing is not None:
            if existing.resolved_at is not None:
                raise AgentError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    "the escalation for this proposal was already answered as "
                    f"{existing.resolution}; an answer is good for one purchase and "
                    "for a limited time, so continuing needs a new proposal",
                    details={"proposal_id": decision.proposal_id,
                             "resolution": existing.resolution},
                )
            return existing

        escalation = EscalationRequest(
            escalation_id=new_id("esc"),
            proposal_id=decision.proposal_id,
            mandate_id=decision.mandate_id,
            mandate_version=decision.mandate_version,
            quote_id=decision.quote_id,
            quote_hash=decision.quote_hash,
            cash_total_cents=decision.cash_total_cents,
            escalate_above_cents=int(mandate.escalate_above_cents or 0),
            currency=decision.currency,
            question=ESCALATION_QUESTION,
            created_at=as_utc(now),
            expires_at=as_utc(now) + timedelta(seconds=_escalation_ttl()),
        )
        self._escalations.add(conn, escalation)
        self._audit.append(
            conn, event_type=AuditEventType.ESCALATION_RAISED,
            actor_type=ActorType.COMMERCE, actor_id=_ACTOR, occurred_at=as_utc(now),
            mandate_id=escalation.mandate_id, proposal_id=escalation.proposal_id,
            payload={
                "escalation_id": escalation.escalation_id,
                "cash_total_cents": escalation.cash_total_cents,
                "escalate_above_cents": escalation.escalate_above_cents,
                "expires_at": escalation.expires_at.isoformat(),
            },
        )
        return escalation

    # -- answering ----------------------------------------------------------

    def approve(self, conn, *, proposal_id: str, principal_id: str,
                now: datetime) -> ApprovalGrant:
        """Record the principal's yes, and issue the grant it buys."""
        escalation = self._answerable(conn, proposal_id=proposal_id,
                                      principal_id=principal_id, now=now)
        decision = self._latest_escalating_decision(conn, proposal_id)
        if not decision.payment_route_id:  # pragma: no cover - the evaluator sets it
            from app.errors import AgentError

            raise AgentError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "the escalated decision names no payment route to approve",
                details={"proposal_id": proposal_id},
            )

        moment = as_utc(now)
        grant = ApprovalGrant(
            grant_id=new_id("grant"),
            proposal_id=proposal_id,
            principal_id=principal_id,
            mandate_id=decision.mandate_id,
            mandate_version=decision.mandate_version,
            quote_id=decision.quote_id,
            quote_hash=decision.quote_hash,
            payment_route_id=decision.payment_route_id,
            cash_total_cents=decision.cash_total_cents,
            currency="HKD",
            issued_at=moment,
            expires_at=moment + timedelta(seconds=GRANT_TTL_SECONDS),
        )
        self._grants.add(conn, grant)
        self._escalations.resolve(conn, proposal_id=proposal_id, resolution="APPROVED",
                                  resolved_at=moment)
        self._audit.append(
            conn, event_type=AuditEventType.ESCALATION_APPROVED,
            actor_type=ActorType.USER, actor_id=principal_id, occurred_at=moment,
            mandate_id=grant.mandate_id, proposal_id=proposal_id,
            payload={
                "escalation_id": escalation.escalation_id,
                "grant_id": grant.grant_id,
                "quote_hash": grant.quote_hash,
                "payment_route_id": grant.payment_route_id,
                "cash_total_cents": grant.cash_total_cents,
                "expires_at": grant.expires_at.isoformat(),
            },
        )
        return grant

    def reject(self, conn, *, proposal_id: str, principal_id: str,
               now: datetime) -> EscalationRequest:
        """Record the principal's no. No grant, and the purchase does not happen."""
        escalation = self._answerable(conn, proposal_id=proposal_id,
                                      principal_id=principal_id, now=now)
        moment = as_utc(now)
        self._escalations.resolve(conn, proposal_id=proposal_id, resolution="REJECTED",
                                  resolved_at=moment)
        self._audit.append(
            conn, event_type=AuditEventType.ESCALATION_REJECTED,
            actor_type=ActorType.USER, actor_id=principal_id, occurred_at=moment,
            mandate_id=escalation.mandate_id, proposal_id=proposal_id,
            payload={"escalation_id": escalation.escalation_id,
                     "cash_total_cents": escalation.cash_total_cents},
        )
        return self._escalations.get(conn, proposal_id) or escalation

    # -- timeouts -----------------------------------------------------------

    def close_if_expired(self, conn, *, proposal_id: str,
                         now: datetime) -> EscalationRequest | None:
        """Resolve an unanswered escalation whose deadline has passed.

        Returns the resolved record, so the caller can refuse the purchase and
        say why. Resolving it is the point: leaving it open would let a later
        answer arrive against a question that has already lapsed.
        """
        escalation = self._escalations.open_for(conn, proposal_id)
        if escalation is None or not escalation.has_expired(as_utc(now)):
            return None
        moment = as_utc(now)
        self._escalations.resolve(conn, proposal_id=proposal_id, resolution="TIMEOUT",
                                  resolved_at=moment)
        self._audit.append(
            conn, event_type=AuditEventType.ESCALATION_TIMED_OUT,
            actor_type=ActorType.SYSTEM, actor_id=_ACTOR, occurred_at=moment,
            mandate_id=escalation.mandate_id, proposal_id=proposal_id,
            payload={"escalation_id": escalation.escalation_id,
                     "expires_at": escalation.expires_at.isoformat()},
        )
        return self._escalations.get(conn, proposal_id) or escalation

    # -- internals ----------------------------------------------------------

    def _answerable(self, conn, *, proposal_id: str, principal_id: str,
                    now: datetime) -> EscalationRequest:
        """The escalation this principal may answer, or a named refusal.

        A lapsed question is *not* closed here. Resolving it needs its own
        committed transaction, and an exception raised from inside the caller's
        would roll the resolution back -- leaving the record open and the
        deadline unenforced, which is the opposite of what a timeout means.
        ``CommerceService.approve_escalation`` closes it first, in a transaction
        of its own, and only then asks this question.
        """
        from app.errors import AgentError

        proposal = self._proposals.get(conn, proposal_id)
        if proposal is None:
            raise AgentError(
                ErrorCode.PROPOSAL_NOT_FOUND,
                f"no proposal {proposal_id!r}",
                details={"proposal_id": proposal_id},
            )
        if proposal.principal_id != principal_id:
            raise AgentError(
                ErrorCode.MANDATE_OWNER_MISMATCH,
                "this proposal belongs to a different principal",
                details={"proposal_id": proposal_id},
            )

        escalation = self._escalations.get(conn, proposal_id)
        if escalation is None:
            raise AgentError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "no escalation was raised for this proposal",
                details={"proposal_id": proposal_id},
            )
        if escalation.resolution == "TIMEOUT":
            raise AgentError(
                ErrorCode.ESCALATION_TIMEOUT,
                "the escalation expired unanswered, so the purchase is refused",
                details={"proposal_id": proposal_id,
                         "expires_at": escalation.expires_at.isoformat()},
            )
        if escalation.resolved_at is not None:
            raise AgentError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"the escalation was already answered as {escalation.resolution}",
                details={"proposal_id": proposal_id,
                         "resolution": escalation.resolution},
            )
        if escalation.has_expired(as_utc(now)):
            raise AgentError(
                ErrorCode.ESCALATION_TIMEOUT,
                "the escalation expired unanswered, so the purchase is refused",
                details={"proposal_id": proposal_id,
                         "expires_at": escalation.expires_at.isoformat()},
            )
        return escalation

    def _latest_escalating_decision(self, conn, proposal_id: str) -> PolicyDecision:
        from app.errors import AgentError

        decision = self._decisions.latest(conn, proposal_id)
        if decision is None or decision.outcome != "ESCALATE":
            raise AgentError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "the latest decision for this proposal is not an escalation",
                details={"proposal_id": proposal_id,
                         "outcome": None if decision is None else decision.outcome},
            )
        return decision


def deny_on_timeout(decision: PolicyDecision, escalation: EscalationRequest,
                    now: datetime) -> PolicyDecision:
    """Rewrite an escalation into the refusal a lapsed question produces.

    Pure, and narrow by construction: it is only meaningful for a decision that
    already carries exactly the escalation violation, and it does not touch the
    recorded amounts, the mandate version or the policy hash. The rule that was
    applied is the same rule the evaluator applied; what changed is that the
    question it wanted to ask can no longer be answered.

    ``ESCALATION_TIMEOUT`` is added as the primary reason, and the original
    violation is kept so a reader can still see which threshold was crossed.
    """
    if decision.outcome != "ESCALATE":  # pragma: no cover - guarded by the caller
        raise ValueError(f"only an escalated decision can time out, not {decision.outcome}")

    violations = list(decision.violations) + [
        PolicyViolation(
            code=ErrorCode.ESCALATION_TIMEOUT,
            field="escalation.expires_at",
            message="the escalation expired unanswered, so the purchase is refused",
            observed=as_utc(now).isoformat(),
            limit=escalation.expires_at.isoformat(),
        )
    ]
    return PolicyDecision(
        outcome="DENY",
        primary_reason=ErrorCode.ESCALATION_TIMEOUT,
        violations=violations,
        mandate_id=decision.mandate_id,
        mandate_version=decision.mandate_version,
        policy_hash=decision.policy_hash,
        proposal_id=decision.proposal_id,
        quote_id=decision.quote_id,
        quote_hash=decision.quote_hash,
        cash_total_cents=decision.cash_total_cents,
        currency=decision.currency,
        payment_route_id=decision.payment_route_id,
        observed_values={**decision.observed_values, "escalation_expired_at":
                         escalation.expires_at.isoformat()},
        applicable_limits=decision.applicable_limits,
        reservation_created=False,
        payment_adapter_called=False,
        evaluated_at=as_utc(now),
    )


def _escalation_ttl() -> int:
    from app.commerce.config import ESCALATION_TTL_SECONDS

    return ESCALATION_TTL_SECONDS


__all__ = ["ESCALATION_QUESTION", "EscalationService", "deny_on_timeout"]
