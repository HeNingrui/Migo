"""A's endpoints. Thin: parse the body, call the orchestrator, wrap the answer.

No domain logic lives here, and nothing here decides anything. The three routes
below are the whole of A's HTTP surface, and each one is a translation:

    ChatRequest -> orchestrator.handle       -> Envelope[AgentResponse]
    AgentActionRequest -> handle_action      -> Envelope[AgentResponse]
    nothing -> database_status + reconcile   -> Envelope[health]

**The response is never flattened to text.** ``AgentResponse`` already carries
``results``, ``quote``, ``mandate``, ``decision``, ``denial``, ``escalation``,
``receipt``, ``payment_failure``, ``constraints``, ``clarification``,
``ambiguities``, ``notes`` and ``trace``, and the envelope passes every one of
them through untouched. An endpoint that answered ``{"message": "selected"}``
would express only prose, and the frontend would have to read the price and the
decision back out of a sentence -- which is how a UI starts disagreeing with the
system behind it.

``next_action`` and ``requires_user_action()`` are what the client renders
controls from, and ``selected_product_id`` is what it binds them to. A position
in a list is not a handle: the next search replaces the list.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.envelope import ok, request_id_for
from app.contracts.agent import AgentActionRequest, ChatRequest

router = APIRouter(tags=["agent"])


def _orchestrator(request: Request):
    """The one orchestrator this process runs.

    Built once in the composition root and reached through application state, so
    the session store and the commerce client are shared. Constructing one per
    request would silently drop every conversation, because a session lives in
    the object that created it.
    """
    return request.app.state.orchestrator


@router.get("/api/v1/health")
def health(request: Request) -> object:
    """Whether the pieces this process needs are actually there.

    The database status is D's own check, reused rather than re-implemented; the
    commerce half is C's reconciliation reader, which reports holds with no
    recorded outcome and re-verifies the audit chain. A health endpoint that only
    says "ok" cannot tell a reviewer which of those is broken.
    """
    from app.commerce.reconcile import reconcile
    from app.commerce.service import CommerceService
    from app.db import database_status

    commerce: CommerceService = request.app.state.commerce
    report = reconcile(request.app.state.db_path).summary()

    return ok({
        "status": "ok" if database_status(request.app.state.db_path)["db_ready"] else "degraded",
        "database": database_status(request.app.state.db_path),
        "commerce": {
            "adapter": commerce.adapter_name,
            "settlement_source_type": commerce.settlement_source_type.value,
            "reconciled": report["clean"],
            "unaccounted_holds": len(report["unaccounted_holds"]),
            "audit_chain": {
                "ok": report["audit_chain"]["ok"],
                "events": report["audit_chain"]["checked"],
            },
        },
        "search": "integrated B — live shared catalog, weighted preferences, review screening",
        "parser_mode": request.app.state.parser_mode,
        "llm": (request.app.state.llm_client.public_status()
                if request.app.state.llm_client else {"configured": False, "successful_calls": 0}),
    }, request_id_for(request))


@router.post("/api/v1/agent/chat")
def chat(payload: ChatRequest, request: Request) -> object:
    """One conversational turn."""
    response = _orchestrator(request).handle(payload)
    return ok(response, response.request_id)


@router.post("/api/v1/agent/actions")
def actions(payload: AgentActionRequest, request: Request) -> object:
    """One explicit action, taken from a control rather than from prose.

    This path never goes through a parser. It exists so that the two consent
    gates -- signing a mandate and running a purchase -- can be expressed as a
    click, which is what ``AgentResponse.requires_user_action()`` promises the
    client it can render.
    """
    response = _orchestrator(request).handle_action(payload)
    return ok(response, response.request_id)


__all__ = ["router"]
