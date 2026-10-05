"""What one turn produced, before it becomes a response.

Kept in its own module so the pieces of the turn loop can hand results to each
other without importing the orchestrator back. A collaborator that needs to
return "here is what to say, and what the user may do next" should not have to
know who is going to record it.

The payload fields are C's and B's own contract objects, carried through
unmodified. They are here rather than looked up from the session at the end of
the turn because a cached copy is a copy that can be stale: the receipt a turn
produces is the receipt that turn must show.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.agent import Clarification
from app.contracts.commerce import Quote, Reservation, SpendState
from app.contracts.common import AgentPhase, NextAction
from app.contracts.mandate import Mandate, MandateDraft
from app.contracts.policy import (
    DenialReceipt,
    EscalationRequest,
    PaymentFailure,
    PaymentReceipt,
    PolicyDecision,
)


@dataclass
class TurnOutcome:
    """The reply a handler decided on, minus the bookkeeping.

    ``produced`` names the payload kinds for the transcript. The payload itself
    travels in the fields below, so a handler that produced a receipt hands over
    the receipt rather than a string saying it has one.

    ``clarification`` is carried here as well as on the parse because some
    questions are not the parser's to ask: "nothing matched, which condition
    shall I relax?" is about the *results*, and the parser never saw them.
    """

    message: str
    next_action: NextAction = NextAction.NONE
    phase: AgentPhase = AgentPhase.IDLE
    produced: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarification: Clarification | None = None

    #: Set when the user picked something, so the client can bind a control to
    #: the product rather than to its position in a list that will change.
    selected_product_id: str | None = None

    # -- structured payloads, unmodified ------------------------------------
    mandate_draft: MandateDraft | None = None
    quote: Quote | None = None
    mandate: Mandate | None = None
    spend_state: SpendState | None = None
    decision: PolicyDecision | None = None
    denial: DenialReceipt | None = None
    escalation: EscalationRequest | None = None
    reservation: Reservation | None = None
    receipt: PaymentReceipt | None = None
    payment_failure: PaymentFailure | None = None


__all__ = ["TurnOutcome"]
