"""Read-only views of C's records: one transaction, and the audit chain.

These are the "a third party can check it" endpoints. Both are reads with no
side effects, and neither re-derives anything: the transaction view is C's own
:class:`~app.contracts.policy.ProposalOutcome` plus its reconciliation report,
and the chain view is C's own events with
:func:`~app.contracts.audit.verify_chain` recomputing every hash.

**Why the chain is keyed by mandate and not by session.** The plan lists
``GET /api/v1/audit/{session_id}``. An ``AuditEvent`` has no session field: it
carries ``mandate_id``, ``proposal_id``, ``reservation_id`` and
``transaction_id``, and a session is A's in-memory construct that the chain has
never heard of. Keying by mandate is the honest mapping -- a session that
authorised a mandate and a mandate that was authorised by a session are the same
pair, and the mandate id is the one that survives a restart.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.envelope import ok, request_id_for
from app.contracts.common import ErrorCode

router = APIRouter(tags=["records"])


def _commerce(request: Request):
    return request.app.state.commerce


@router.get("/api/v1/transactions/{proposal_id}")
def transaction(proposal_id: str, request: Request) -> object:
    """Everything C recorded about one purchase proposal.

    Includes the decision, the denial or the escalation, the reservation, the
    receipt and any payment failure -- and the reconciliation report, so a reader
    can see whether anything is still unresolved rather than having to ask.
    """
    from app.commerce.reconcile import reconcile
    from app.errors import AgentError

    commerce = _commerce(request)
    outcome = commerce.get_proposal_outcome(proposal_id)
    if outcome is None:
        raise AgentError(
            ErrorCode.PROPOSAL_NOT_FOUND,
            f"no proposal {proposal_id!r}",
            details={"proposal_id": proposal_id},
        )

    report = reconcile(request.app.state.db_path).summary()
    return ok({
        "outcome": outcome.model_dump(mode="json"),
        "audit_events": [event.model_dump(mode="json")
                         for event in commerce.audit_events_for_proposal(proposal_id)],
        "reconciliation": report,
    }, request_id_for(request))


@router.get("/api/v1/audit/{mandate_id}")
def audit(mandate_id: str, request: Request) -> object:
    """The chain for one mandate, unverified: the records as written."""
    events = _commerce(request).audit_events(mandate_id)
    return ok({
        "mandate_id": mandate_id,
        "count": len(events),
        "events": [event.model_dump(mode="json") for event in events],
    }, request_id_for(request))


@router.get("/api/v1/audit/{mandate_id}/verify")
def verify(mandate_id: str, request: Request) -> object:
    """Recompute the whole chain and say where it first breaks.

    The whole chain rather than the mandate's slice, because that is what makes
    the answer worth anything: a record removed from *anywhere* breaks every hash
    after it, and verifying a filtered view would report a chain as intact while
    the file it came from had been edited.
    """
    commerce = _commerce(request)
    report = commerce.verify_audit_chain()
    return ok({
        "mandate_id": mandate_id,
        "ok": report.ok,
        "checked": report.checked,
        "first_sequence": report.first_sequence,
        "last_sequence": report.last_sequence,
        "head_hash": report.head_hash,
        "breaks": [b.model_dump(mode="json") for b in report.breaks],
    }, request_id_for(request))


__all__ = ["router"]
