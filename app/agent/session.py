"""Session state, so a second turn can mean something.

"the second one", "cheaper than that", "make it lighter" are only answerable
against what the conversation already established. This module is that memory.

Three rules keep it from becoming a second source of truth:

**1. The session holds criteria, not money.** There is no balance here, no cap,
no remaining budget, no policy hash. Those are C's, they change without A
knowing, and a cached copy is how a stale number reaches a user.

**2. References resolve against what was shown, not against a fresh search.**
``last_shown_product_ids`` is written when results are rendered, so "the second
one" means the second one the user actually saw -- even if the catalog changed
underneath.

**3. Storage is in-process and deliberately not durable.** The plan fixes a
single backend worker and keeps sessions in memory; mandate, audit chain and
reservations are the things that must survive a restart, and C owns all three.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.contracts.agent import (
    Intent,
    ParseSource,
    SessionContext,
)
from app.contracts.commerce import PurchaseProposal, Quote
from app.contracts.common import AgentPhase, ErrorCode
from app.contracts.mandate import Mandate, MandateDraft
from app.contracts.product import HardConstraints, Product
from app.contracts.profile import UserProfile
from app.contracts.search import PreferenceProfile, SearchResponse

#: How many turns to keep. A transcript is shown to the user, not replayed into
#: the model, so the bound is for display and for debugging.
MAX_TURNS = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Turn:
    """One exchange, kept for the transcript and for debugging a parse."""

    index: int
    user_text: str
    assistant_message: str
    at: datetime
    intent: Intent | None = None
    parse_source: ParseSource | None = None
    #: Structured payload kinds produced this turn, e.g. ``["results"]``.
    produced: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "at": self.at.isoformat(),
            "user": self.user_text,
            "assistant": self.assistant_message,
            "intent": self.intent.value if self.intent else None,
            "parse_source": self.parse_source.value if self.parse_source else None,
            "produced": list(self.produced),
        }


@dataclass
class AgentSession:
    """What one conversation remembers between turns.

    The distinction this class is built around: **it holds context, never
    authority.** A mandate id, a version number, a quote the user was shown and
    the product they picked are all *references* to something C owns. None of
    them is a fact about the world that A gets to decide, and every one of them
    can be stale by the time the next turn reads it -- which is why the purchase
    flow re-reads the mandate and hands C the version it last saw rather than
    asserting anything.

    What is deliberately absent is the other half of the same idea: no approval
    flag, no "paid" boolean, no remaining budget, no policy hash. A cached
    ``Mandate`` or ``Quote`` exists so the conversation can talk about the
    amounts it is looking at, and a cached copy is not authority.
    """

    session_id: str
    principal_id: str

    phase: AgentPhase = AgentPhase.IDLE

    #: The current search criteria, or None before the first search. This is a
    #: *merged* value: each turn applies a patch to it.
    #:
    #: There is no stored ``preferences`` beside it. Soft criteria are derived on
    #: read from :attr:`profile` (see :meth:`soft_preferences`), because a
    #: preference the user cannot see the reason for is a preference they cannot
    #: correct -- and because a stored copy is a second place for the same facts
    #: to be wrong. The attribute existed here unwritten for a while; deriving is
    #: what it was for.
    constraints: HardConstraints | None = None

    #: What A understands about the user: how they use it, what they plug into,
    #: what they care about. Each fact records whether the user said it or A
    #: inferred it, which is what keeps an inference from being presented as the
    #: user's own words.
    #:
    #: Not authority, and not money: a profile shapes ranking and never a
    #: payment. The same rule as ``constraints`` above -- see the module
    #: docstring -- applies with more force here, because a profile is the most
    #: tempting thing in the session to mistake for a preference the user signed.
    profile: UserProfile = field(default_factory=UserProfile)

    #: A mandate being drafted, before the user signs it. Never an activated
    #: mandate -- that lives in C.
    mandate_draft: MandateDraft | None = None
    mandate_id: str | None = None
    #: The version this conversation last saw. Sent back as
    #: ``expected_mandate_version`` so a revocation lands on the purchase
    #: instead of being missed.
    mandate_version: int | None = None
    #: C's mandate as it was when last read, for display only.
    last_mandate: Mandate | None = None

    #: Product ids in the order they were last shown, so ranks resolve to what
    #: the user saw rather than to a fresh search.
    last_shown_product_ids: list[str] = field(default_factory=list)
    last_results: SearchResponse | None = None
    browse_seen_ids: set[str] = field(default_factory=set)
    browse_limit: int = 3
    stated_criteria: list = field(default_factory=list)

    #: The product the user chose, and C's price for it.
    selected_product_id: str | None = None
    pending_quote: Quote | None = None
    #: The rails the user named when authorising. Empty means no preference of
    #: their own, and the proposal then carries the mandate's own list.
    preferred_route_ids: list[str] = field(default_factory=list)

    #: A proposal awaiting the user's answer, and the purchase it belongs to.
    pending_proposal_id: str | None = None
    #: The proposal itself, so an answered escalation can be continued.
    #:
    #: An ``ApprovalGrant`` is bound to one ``proposal_id``, so a purchase the
    #: principal just approved has to be *re-submitted* rather than replaced --
    #: a new proposal is a different purchase and the grant would not cover it.
    #: Cleared as soon as the attempt is over.
    pending_proposal: PurchaseProposal | None = None
    open_escalation_proposal_id: str | None = None

    #: How many submissions this session has made. The idempotency key is
    #: derived from it, because a deliberate re-submission needs a fresh key
    #: while a transport retry reuses one -- and the two are different requests.
    #: The counter alone is not enough to make a key unique; see
    #: :meth:`next_idempotency_key`.
    submissions: int = 0

    turns: list[Turn] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    # -- derived ------------------------------------------------------------

    @property
    def has_shown_products(self) -> bool:
        return bool(self.last_shown_product_ids)

    @property
    def has_active_mandate(self) -> bool:
        return self.mandate_id is not None

    def context(self) -> SessionContext:
        """The minimum a parser needs to read the next turn in context."""
        return SessionContext(
            principal_id=self.principal_id,
            phase=self.phase,
            current_constraints=self.constraints,
            current_mandate_draft=self.mandate_draft,
            current_profile=self.profile if not self.profile.is_empty() else None,
            last_shown_product_ids=list(self.last_shown_product_ids),
            selected_product_id=self.selected_product_id,
            has_active_mandate=self.has_active_mandate,
            open_escalation_proposal_id=self.open_escalation_proposal_id,
        )

    def product_at_rank(self, rank: int) -> str | None:
        """Resolve "the second one" against what was shown. 1-based."""
        if 1 <= rank <= len(self.last_shown_product_ids):
            return self.last_shown_product_ids[rank - 1]
        return None

    def product(self, product_id: str) -> Product | None:
        """The product as it was last shown, or None if it is no longer here."""
        for candidate in (self.last_results.candidates if self.last_results else []):
            if candidate.product_id == product_id:
                return candidate.product
        return None

    def next_idempotency_key(self) -> str:
        """A fresh key for the next submission.

        Random, and not merely ``session-<n>``. The session counter restarts with
        the process and session ids are minted from a counter too, so a second
        run of the same application hands its first conversation ``sess_0001``
        and its first submission ``sess_0001-1`` -- which the database already
        holds, against a different purchase. C's idempotency rule then refuses
        the purchase with ``IDEMPOTENCY_CONFLICT``: a correct refusal of a
        mistake A made, and a demo that breaks on its second start.

        The session id and the counter stay in the key because a human reads it
        in an audit log; the random suffix is what makes it unique.
        """
        self.submissions += 1
        return f"{self.session_id}-{self.submissions}-{uuid.uuid4().hex[:8]}"

    # -- mutation -----------------------------------------------------------

    def remember_results(self, results: SearchResponse,
                         previous: "SearchResponse | None" = None) -> None:
        """Record what was shown. Ranks resolve against this, not a re-query.

        ``previous`` is what was on screen before this search. It matters when
        the new result is *empty*: the products the user was reacting to are gone
        from the new response, and forgetting them would make the next "太重了"
        unanswerable -- there would be nothing left to be too heavy relative to.
        A rejection is feedback about what someone saw, so what they saw has to
        survive the search that followed it.
        """
        self.last_results = results
        self.browse_seen_ids.update(c.product_id for c in results.candidates)
        shown = [c.product_id for c in results.candidates]
        if shown:
            self.last_shown_product_ids = shown
            # The list moved, so a selection against the old one is no longer
            # what the user is looking at.
            if self.selected_product_id not in shown:
                self.selected_product_id = None
                self.pending_quote = None
        elif previous is not None and previous.candidates:
            # Keep the last list that actually had something in it, and keep the
            # empty response as the record of the search itself.
            self.last_shown_product_ids = [c.product_id for c in previous.candidates]
            self.selected_product_id = None
            self.pending_quote = None
        else:
            self.last_shown_product_ids = []
            self.selected_product_id = None
            self.pending_quote = None
        self.phase = AgentPhase.SHOWING_PRODUCTS

    def capture_results(self) -> SearchResponse | None:
        """What is on screen now, for a caller that is about to replace it."""
        return self.last_results

    def select(self, product_id: str, quote: Quote) -> None:
        self.selected_product_id = product_id
        self.pending_quote = quote

    def forget_selection(self) -> None:
        self.selected_product_id = None
        self.pending_quote = None

    def remember_profile(self, incoming: UserProfile | None) -> UserProfile:
        """Fold what this turn said about the user into what is already known.

        An empty or absent update changes nothing: a turn about a product must
        not quietly reset what an earlier turn established about the person.
        """
        if incoming is not None and not incoming.is_empty():
            self.profile = self.profile.merge(incoming)
        return self.profile

    def soft_preferences(self) -> PreferenceProfile:
        """The B-facing preference profile derived from the current facts.

        Derived rather than stored, on every read. A cached copy would be a
        second source of truth for the same facts, and the first thing to go
        stale the moment a user corrects an inference.
        """
        from app.contracts.profile import preference_profile_from

        return preference_profile_from(self.profile, base_criteria=self.stated_criteria)

    def forget_purchase(self) -> None:
        """The attempt is over: nothing is waiting, and a retry is a new purchase."""
        self.pending_proposal = None
        self.pending_proposal_id = None
        self.open_escalation_proposal_id = None
        self.forget_selection()

    def record_turn(self, turn: Turn) -> None:
        self.turns.append(turn)
        if len(self.turns) > MAX_TURNS:
            del self.turns[:-MAX_TURNS]
        self.updated_at = _now()

    def next_turn_index(self) -> int:
        return len(self.turns) + 1

    def transcript(self) -> list[dict[str, object]]:
        return [t.as_dict() for t in self.turns]


class SessionStore:
    """In-process sessions.

    Not thread-safe by design: DC8 fixes one backend worker, and a lock here
    would suggest a concurrency guarantee this layer cannot make. The concurrent
    case that matters -- two proposals racing for the same budget -- is settled
    by C's ``BEGIN IMMEDIATE``, not here.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"sess_{self._counter:04d}"

    def create(self, principal_id: str, session_id: str | None = None) -> AgentSession:
        session = AgentSession(
            session_id=session_id or self.new_id(), principal_id=principal_id
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> AgentSession:
        """Fetch, or raise. A missing session is a client error, not a new one.

        Silently starting a fresh session when the id is unknown would drop the
        user's criteria and look like the agent forgot what it was told.
        """
        from app.errors import AgentError

        try:
            return self._sessions[session_id]
        except KeyError:
            raise AgentError(
                ErrorCode.SESSION_NOT_FOUND,
                f"no session {session_id!r}; start one without a session_id",
                details={"session_id": session_id},
            ) from None

    def get_or_create(self, *, principal_id: str, session_id: str | None) -> AgentSession:
        if session_id is None:
            return self.create(principal_id)
        return self.get(session_id)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)


__all__ = ["AgentSession", "SessionStore", "Turn", "MAX_TURNS"]
