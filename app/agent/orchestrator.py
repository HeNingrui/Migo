"""One turn, from a sentence to an answer.

    say something -> understood -> looked up -> answered -> say something else

The hard part is not any single step, it is the *second* turn. "便宜点" and
"第二款" carry no requirements of their own; they only mean something against
what the conversation already established.

This class is the turn loop and nothing else. It reads the catalog through
:class:`~app.agent.browsing.BrowsingTurns` and reaches C through
:class:`~app.agent.purchase_flow.CommerceTurns`, and it never touches either
subsystem itself. That is what keeps the loop short enough to follow and the two
halves independently testable.

Six rules, each of them a decision rather than a default:

**1. The parser proposes; the orchestrator disposes.** A parse carrying an
untraceable value is refused here, before anything is looked up, even though the
deterministic parser cannot produce one. The check exists for the model path.

**2. A direction with no number is resolved from what is on screen, and the
resolution is stated.** "便宜点" becomes a concrete cap derived from the
candidates already shown, and the reply says which number was chosen and why.
Silently picking a number is how an agent acquires a budget nobody agreed to.

**3. Nothing is bought here.** This class decides *which* handler answers a
turn. The handlers propose, C decides, and the only thing this layer does with a
decision is render it.

**4. Degradation is visible.** Which parser ran, and whether a value was derived
rather than stated, both appear in the reply.

**5. A turn that asks always says it is asking.** The parse may carry no
ambiguity -- an intent can be recognised while still being unanswerable -- so the
check is repeated at the end rather than trusted once.

**6. Commerce is optional, and its absence is stated.** An orchestrator built
without a commerce client still answers searches; it says so rather than
pretending a purchase was attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.agent import response_renderer as render
from app.agent import profile_flow
from app.agent.browsing import BrowsingTurns
from app.agent.clients import CommerceClient, SearchClient
from app.agent.purchase_flow import CommerceTurns
from app.agent.session import AgentSession, SessionStore, Turn
from app.agent.turn import TurnOutcome
from app.contracts.agent import (
    AgentActionRequest,
    AgentResponse,
    Ambiguity,
    AmbiguityKind,
    ChatRequest,
    Clarification,
    ConstraintPatch,
    Intent,
    IntentParser,
    IntentResult,
    ParseSource,
    ParserAttempt,
)
from app.contracts.common import AgentPhase, ErrorCode, NextAction, new_request_id

#: Intents whose answer comes from C. Grouped so the dispatch below reads as
#: "these are the money turns", which is the distinction that matters.
_MONEY_INTENTS: frozenset[Intent] = frozenset({
    Intent.CREATE_MANDATE,
    Intent.UPDATE_MANDATE_DRAFT,
    Intent.ACTIVATE_MANDATE,
    Intent.REVOKE_MANDATE,
    Intent.RUN_DELEGATED_PURCHASE,
    Intent.APPROVE_ESCALATION,
    Intent.REJECT_ESCALATION,
    Intent.CHECK_STATUS,
})


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class AgentOrchestrator:
    """The turn loop. Holds no money and makes no decision that matters."""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        search: SearchClient,
        commerce: CommerceClient | None = None,
        parser: IntentParser | None = None,
        llm_parser: IntentParser | None = None,
        explainer=None,
        principal_id: str = "demo_user",
        agent_id: str = "demo_agent",
        shipping_address_id: str = "addr_demo_01",
        address_change_allowed: bool = False,
    ) -> None:
        from app.agent.fallback_parser import FallbackIntentParser

        self._sessions = sessions
        self._browsing = BrowsingTurns(search)
        self._principal_id = principal_id
        self._agent_id = agent_id
        self._commerce = commerce
        #: The commerce conversation, or ``None`` when no client was supplied.
        #: Built once, so a caller cannot end up with two of them disagreeing
        #: about which address the user signed for.
        self._money = (
            CommerceTurns(
                commerce,
                principal_id=principal_id,
                agent_id=agent_id,
                shipping_address_id=shipping_address_id,
                address_change_allowed=address_change_allowed,
            )
            if commerce is not None else None
        )
        self._deterministic = parser or FallbackIntentParser(
            route_ids=commerce.payment_routes() if commerce is not None else (),
        )
        self._llm = llm_parser
        self._explainer = explainer

    # -- the turn -----------------------------------------------------------

    def handle(self, request: ChatRequest) -> AgentResponse:
        session = self._sessions.get_or_create(
            principal_id=self._principal_id, session_id=request.session_id
        )
        request_id = new_request_id()
        from app.agent.catalog_commands import navigation, price_order
        command = navigation(request.message)
        if command:
            result = IntentResult(raw_text=request.message, intent=Intent.UPDATE_SEARCH,
                parse_source=ParseSource.FALLBACK, search=ConstraintPatch())
            outcome = self._browsing.more(session, show_all=command == "all")
            return self._respond(session, request_id, _Understood.of(result), outcome,
                [ParserAttempt(source=ParseSource.FALLBACK, ok=True, detail="catalog_navigation")])
        result, trace = self._understand(request.message, session)
        if result.intent in {Intent.SEARCH, Intent.UPDATE_SEARCH, Intent.ASK_ALTERNATIVES, Intent.UNKNOWN} and not result.untraceable_fields():
            ordering = price_order(request.message)
            if result.search_is_new and result.intent == Intent.SEARCH:
                session.stated_criteria.clear()
            if ordering:
                session.stated_criteria = [ordering, *[c for c in session.stated_criteria if c.attribute != ordering.attribute]]
                result = result.model_copy(update={"intent": Intent.UPDATE_SEARCH,
                    "search": result.search or ConstraintPatch(), "ambiguities": []})
        outcome = self._route(result, session)
        return self._respond(session, request_id, _Understood.of(result), outcome, trace)

    def handle_action(self, request: AgentActionRequest) -> AgentResponse:
        """A button, not a sentence.

        This path never reaches a parser, and that is the point of having it:
        the two consent gates -- signing a mandate and running a purchase -- are
        the places where "the user agreed" has to be a fact rather than an
        interpretation, and a click is a fact. What the click *means* is still
        C's answer: the action says which product, which mandate or which
        proposal, and everything after that is the same code path a sentence
        takes.
        """
        from app.errors import AgentError

        session_id = request.session_id
        if self._sessions.get(session_id) is None:  # pragma: no cover - get raises
            raise AgentError(
                ErrorCode.SESSION_NOT_FOUND, f"no session {session_id!r}",
                details={"session_id": session_id},
            )
        session = self._sessions.get(session_id)
        understood = _Understood.of_action(request)
        outcome = self._route_action(request, session)
        return self._respond(session, request.request_id or new_request_id(),
                             understood, outcome, trace=[])

    def _route_action(self, request: AgentActionRequest,
                      session: AgentSession) -> TurnOutcome:
        """Dispatch an explicit action. Same handlers, no parsing."""
        intent = request.intent

        if intent == Intent.CANCEL_SELECTION:
            return self._cancel_selection(session)

        if self._money is None:
            return self._no_commerce()

        if intent == Intent.SELECT_PRODUCT:
            return self._money.price(session, request.product_id,
                                     quantity=request.quantity or 1)
        if intent in (Intent.CREATE_MANDATE, Intent.UPDATE_MANDATE_DRAFT):
            return self._money.draft_mandate(session, result=None, patch=None)
        if intent == Intent.ACTIVATE_MANDATE:
            self._reject_foreign_mandate(request, session)
            return self._money.activate_mandate(session)
        if intent == Intent.REVOKE_MANDATE:
            self._reject_foreign_mandate(request, session)
            return self._money.revoke_mandate(session)
        if intent == Intent.RUN_DELEGATED_PURCHASE:
            return self._money.run_purchase(session)
        if intent in (Intent.APPROVE_ESCALATION, Intent.REJECT_ESCALATION):
            # The client names the proposal, so a stale button cannot answer a
            # different question than the one it was rendered for.
            if request.proposal_id is not None:
                session.open_escalation_proposal_id = request.proposal_id
            outcome = self._money.answer_escalation(
                session, approve=intent == Intent.APPROVE_ESCALATION)
            if outcome.next_action == NextAction.RUN_PURCHASE:
                # Same continuation the sentence path takes: the approval is
                # recorded, and the purchase is a separate submission.
                return self._continue_after_approval(session, outcome)
            return outcome
        if intent == Intent.CHECK_STATUS:
            return self._money.status(session)

        return TurnOutcome(
            message="这个动作我现在还不会执行。",
            next_action=NextAction.NONE,
            phase=session.phase,
            notes=[f"unsupported action: {intent.value}"],
        )

    @staticmethod
    def _reject_foreign_mandate(request: AgentActionRequest,
                                session: AgentSession) -> None:
        """A client may name a mandate, and it has to be this session's.

        Ignoring a supplied identifier would be worse than refusing it: a stale
        button from another conversation would then act on whatever this session
        happens to hold, which is exactly the mistake naming it was meant to
        prevent.
        """
        from app.errors import AgentError

        if request.mandate_id is not None and request.mandate_id != session.mandate_id:
            raise AgentError(
                ErrorCode.CONFLICT,
                "this session does not hold that mandate",
                details={"mandate_id": request.mandate_id,
                         "session_id": session.session_id},
            )

    def _route(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Decide what this turn does. The only place intents are dispatched."""
        # What the turn said about the user is recorded before anything is
        # decided, because every branch below can be improved by it and none of
        # them may override it. It cannot reach C: a profile is not authority.
        session.remember_profile(result.profile)

        invented = result.untraceable_fields()
        if invented:
            # Rule 1. The last gate before anything is looked up.
            #
            # No clarification object is attached, and that is the contract's
            # rule rather than a preference: a turn carrying an untraceable
            # value must not also ask a question, because the invented value has
            # to be resolved before anything else is asked. The message tells the
            # user to say it again; the response carries the field names so the
            # client can point at them.
            return TurnOutcome(
                message="这次理解里有无法追溯到你的原话的数值，我没有按它执行。请再说一次。",
                next_action=NextAction.NONE,
                phase=AgentPhase.AWAITING_REQUIREMENTS,
                notes=[f"untraceable: {', '.join(invented)}"],
            )

        if result.intent == Intent.SELECT_PRODUCT:
            return self._select(result, session)

        if result.intent == Intent.CANCEL_SELECTION:
            return self._cancel_selection(session)

        if result.intent == Intent.REJECT_RECOMMENDATION:
            return self._reject(result, session)

        if result.intent == Intent.ASK_ALTERNATIVES:
            if self._explainer is not None and session.last_results:
                # A question about evidence preserves the displayed candidates
                # and ranking. It must not trigger the old colour-variant search.
                return TurnOutcome(message="按当前筛选条件和候选商品回答。",
                    phase=AgentPhase.SHOWING_PRODUCTS, produced=["results"],
                    next_action=NextAction.SELECT_PRODUCT if session.last_results.candidates else NextAction.NONE)
            # Answer if there is a subject; ask if there is not.
            return self._browsing.answer_question(session) or self._clarify(result, session)

        if result.intent in (Intent.SEARCH, Intent.UPDATE_SEARCH):
            return self._search(result, session)

        if result.intent in _MONEY_INTENTS:
            return self._money_turn(result, session)

        return self._clarify(result, session)

    def _cancel_selection(self, session: AgentSession) -> TurnOutcome:
        if session.open_escalation_proposal_id and self._money:
            self._money.answer_escalation(session, approve=False)
        session.forget_purchase()
        return TurnOutcome(message="已取消当前选择和待执行的报价。可以重新挑选商品；已完成的付款记录仍保留。",
                           next_action=NextAction.NONE, phase=AgentPhase.CANCELLED)

    def _money_turn(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Hand a money intent to C's half of the conversation.

        Every branch here either asks C a question or submits something to it.
        None of them decides anything: the outcomes -- approved, denied,
        escalated, paid, refused -- all come back from C and are rendered, not
        computed.
        """
        intent = result.intent

        if intent == Intent.CHECK_STATUS:
            if self._money is None:
                return self._no_commerce()
            return self._money.status(session)

        if self._money is None:
            return self._no_commerce()

        if intent in (Intent.CREATE_MANDATE, Intent.UPDATE_MANDATE_DRAFT):
            return self._money.draft_mandate(session, result, result.mandate)

        if intent == Intent.ACTIVATE_MANDATE:
            # A summary signed from the session's own draft: the parser may echo
            # the draft it was shown, and the thing that gets submitted has to be
            # the thing that was summarised, not the echo.
            return self._money.activate_mandate(session)

        if intent == Intent.REVOKE_MANDATE:
            return self._money.revoke_mandate(session)

        if intent == Intent.RUN_DELEGATED_PURCHASE:
            return self._money.run_purchase(session)

        if intent == Intent.APPROVE_ESCALATION:
            outcome = self._money.answer_escalation(session, approve=True)
            if outcome.next_action == NextAction.RUN_PURCHASE:
                return self._continue_after_approval(session, outcome)
            return outcome

        if intent == Intent.REJECT_ESCALATION:
            return self._money.answer_escalation(session, approve=False)

        return self._clarify(result, session)  # pragma: no cover - the set is closed

    def _continue_after_approval(self, session: AgentSession,
                                 approval: TurnOutcome) -> TurnOutcome:
        """Run the purchase the principal just authorised.

        Two C calls rather than one, and deliberately so: the approval is a
        recorded answer, and the purchase is a separate submission that C
        re-evaluates against it. Folding them together would mean A deciding
        that an approval implies a purchase.
        """
        purchase = self._money.run_purchase(session)
        return TurnOutcome(
            message="\n".join([approval.message, purchase.message]),
            next_action=purchase.next_action,
            phase=purchase.phase,
            produced=[*approval.produced, *purchase.produced],
            notes=[*approval.notes, *purchase.notes],
            clarification=purchase.clarification,
            selected_product_id=purchase.selected_product_id,
            decision=purchase.decision,
            denial=purchase.denial,
            escalation=purchase.escalation,
            reservation=purchase.reservation,
            receipt=purchase.receipt,
            payment_failure=purchase.payment_failure,
        )

    def _no_commerce(self) -> TurnOutcome:
        return TurnOutcome(
            message=(
                "这个进程里没有接上交易层，所以我现在只能看商品，不能报价或下单。"
            ),
            next_action=NextAction.NONE,
            phase=AgentPhase.AWAITING_REQUIREMENTS,
            notes=["no commerce client configured"],
        )

    def _search(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Search, or open the conversation if there is nothing to search on."""
        patch = result.search
        if patch is None:  # pragma: no cover - the contract enforces this
            return self._clarify(result, session)

        # Two shapes of "nothing to search on", and they are answered differently.
        #
        # A *self-description* ("我平时通勤，喜欢轻一点的") says nothing about a
        # product and everything about the user. It is not a failed search: it
        # re-orders the shelf the user is already looking at, and if they have not
        # asked for one yet, the reply acknowledges and asks the one question that
        # would make the next search meaningful.
        #
        # A *cold browse* ("我想买个耳机") asks for a shelf. That is the opening's
        # job, and this branch must not swallow it.
        describes_the_user = result.profile is not None and not result.profile.is_empty()
        if patch.is_empty() and describes_the_user and not session.stated_criteria:
            if session.constraints is None:
                return self._profile_turn(result, session)
            outcome = self._browsing.search(
                patch, is_new=False, ambiguities=list(result.ambiguities),
                session=session, preferences_changed=True,
            )
            if outcome is not None:
                return outcome
            return self._profile_turn(result, session)

        outcome = self._browsing.search(
            patch,
            is_new=result.search_is_new,
            ambiguities=list(result.ambiguities),
            session=session,
            preferences_changed=bool(session.stated_criteria),
        )
        # The browsing layer declines when the turn carries nothing and there is
        # nothing on file -- the cold "我想买个耳机". That is the opening's job.
        return outcome or self._browsing.opening(session) or self._clarify(result, session)

    # -- the user, rather than the catalogue ---------------------------------

    def _profile_turn(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Acknowledge what was said about the user, and ask the next question.

        No products are shown. That is the decision this method exists to make:
        a self-description with no requirement is not a search request, and
        answering it with a listing would dress A's default ordering up as
        "here is what suits you" -- from a profile that has just this moment
        stopped being empty.
        """
        described = profile_flow.describe_profile(session.profile)
        question, about = self._next_profile_question(session)
        lines = [line for line in (described, question) if line]
        return TurnOutcome(
            message="\n".join(lines),
            next_action=NextAction.DESCRIBE_PREFERENCES,
            phase=AgentPhase.AWAITING_REQUIREMENTS,
            produced=["profile"],
            notes=["a self-description: nothing was searched for"],
            clarification=Clarification(question=question, about=[about],
                                        blocking=False),
        )

    def _reject(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Re-open the requirement instead of re-ranking what was shown.

        A rejection is evidence about the user, so the profile is updated first
        and the answer is built from the updated profile. Where the user named a
        dimension ("太重"), the reply is the one the refinement path already
        gives: a bound derived from what was actually shown, with the derived
        number stated so it can be disagreed with. Where they named nothing
        ("都不喜欢"), guessing a dimension would be inventing the complaint --
        so the reply asks which of their own conditions to give up.

        What never happens here: B is not asked to re-rank, and no hard
        constraint is created from a complaint.
        """
        rejection = profile_flow.read_rejection(result.raw_text)
        session.remember_profile(rejection.profile)
        described = profile_flow.describe_profile(session.profile)

        if rejection.direction is None or not session.last_shown_product_ids:
            question = (
                "那是哪一点不合适？说一个方向就行 —— 更轻、更便宜、续航更长，"
                "或者换个用途。"
            )
            return TurnOutcome(
                message="\n".join(line for line in (described, question) if line),
                next_action=NextAction.ANSWER_QUESTION,
                phase=AgentPhase.SHOWING_PRODUCTS,
                produced=["profile"] if described else [],
                notes=["rejection with no dimension named; asking rather than guessing"],
                clarification=Clarification(question=question, about=["message"],
                                            blocking=False),
            )

        # A named dimension goes down the same road "便宜点" takes: a bound
        # derived from the shelf the user was actually looking at, stated aloud.
        outcome = self._browsing.search(
            ConstraintPatch(),
            is_new=False,
            ambiguities=[Ambiguity(
                kind=AmbiguityKind.UNDERSPECIFIED,
                field=rejection.direction,
                detail="the user rejected a recommended product on this dimension",
                question="想收到什么程度？",
            )],
            session=session,
        )
        if outcome is None:  # pragma: no cover - there were products on screen
            return self._clarify(result, session)
        notes = list(outcome.notes)
        notes.append(f"re-analysis after a rejection on {rejection.direction}")
        return TurnOutcome(
            message="\n".join(line for line in (described, outcome.message) if line),
            next_action=outcome.next_action,
            phase=outcome.phase,
            produced=[*outcome.produced, "profile"],
            notes=notes,
            clarification=outcome.clarification,
        )

    def _next_profile_question(self, session: AgentSession) -> tuple[str, str]:
        """The one question worth asking about the user next."""
        found = profile_flow.next_question(session.profile)
        if found is None:
            return ("还想补充什么偏好吗？没有的话直接说预算和款式就行。", "message")
        field_name, question = found
        return question, field_name

    # -- parsing ------------------------------------------------------------

    def _understand(
        self, text: str, session: AgentSession
    ) -> tuple[IntentResult, list[ParserAttempt]]:
        """Model first when configured, deterministic parser always available.

        The order is deliberate: the deterministic parser is what runs offline
        and in tests, so it is the one whose behaviour is known exactly. The
        model is an improvement on it, not a prerequisite for it.
        """
        context = session.context()
        trace: list[ParserAttempt] = []

        if self._llm is not None:
            try:
                result = self._llm.parse(text, context)
            except Exception as exc:  # noqa: BLE001 - a model failure is data
                trace.append(ParserAttempt(
                    source=ParseSource.LLM, ok=False,
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                ))
            else:
                trace.append(ParserAttempt(source=ParseSource.LLM, ok=True))
                if result.is_actionable() or result.intent != Intent.UNKNOWN:
                    return result, trace
                trace.append(ParserAttempt(
                    source=ParseSource.LLM, ok=False,
                    detail="the model returned UNKNOWN; trying the local rules",
                ))

        fallback = self._deterministic.parse(text, context)
        trace.append(ParserAttempt(source=ParseSource.FALLBACK, ok=True))
        return fallback, trace

    # -- asking -------------------------------------------------------------

    def _clarify(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Ask, without asking for anything already on file.

        The question is grounded in the session: with criteria recorded, a vague
        turn offers to continue with them rather than starting over. Re-asking
        for a budget the user gave two turns ago is the single most common way
        an assistant looks like it is not listening.
        """
        clarification = result.clarification() or Clarification(
            question="能再说得具体一点吗？", about=[], blocking=False,
        )
        return TurnOutcome(
            message=render.describe_clarification(
                clarification, constraints=session.constraints
            ),
            next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.AWAITING_REQUIREMENTS,
            notes=render.describe_ambiguities(result.ambiguities),
            clarification=clarification,
        )

    # -- selecting something already shown ----------------------------------

    def _select(self, result: IntentResult, session: AgentSession) -> TurnOutcome:
        """Resolve a rank against what was shown, then ask C to price it.

        Two steps, and the second is C's: resolving "the second one" is A's job
        because only A knows what was on screen, and pricing it is not, because
        only C knows what it costs.
        """
        target = result.target
        if target is None or target.rank is None:
            return self._clarify(result, session)

        product_id = session.product_at_rank(target.rank)
        if product_id is None:
            return self._unresolvable_rank(target.rank, session)

        if self._money is None:
            product = session.product(product_id)
            if product is None:  # pragma: no cover - ids come from the same response
                return self._clarify(result, session)
            return TurnOutcome(
                message=(
                    f"选中第 {target.rank} 款：{product.name}（{product.brand}）— "
                    f"{render.money(product.price_cents)}"
                ),
                next_action=NextAction.NONE,
                phase=AgentPhase.SHOWING_PRODUCTS,
                produced=["selected_product"],
                selected_product_id=product_id,
                notes=["no commerce client configured; nothing was priced"],
            )

        return self._money.price(session, product_id)

    @staticmethod
    def _unresolvable_rank(rank: int, session: AgentSession) -> TurnOutcome:
        shown = len(session.last_shown_product_ids)
        question = (
            f"你指的是第 {rank} 款，但上一次只显示了 {shown} 款。要我先搜一次吗？"
            if shown else
            "还没有给你看过商品列表。要我先搜一次吗？"
        )
        return TurnOutcome(
            message=question,
            next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.AWAITING_REQUIREMENTS,
            notes=[f"rank {rank} out of range ({shown} shown)"],
            clarification=Clarification(
                question=question, about=["product_id"], blocking=False,
            ),
        )

    # -- recording ----------------------------------------------------------

    def _respond(
        self,
        session: AgentSession,
        request_id: str,
        understood: "_Understood",
        outcome: TurnOutcome,
        trace: list[ParserAttempt],
    ) -> AgentResponse:
        """Record the turn and hand back the response.

        The only place that knows how an ``AgentResponse`` is assembled, so no
        handler has to. Every payload is copied straight across from the
        :class:`~app.agent.turn.TurnOutcome` that produced it, so what the user
        is shown is what that turn received rather than a later lookup.
        """
        notes = list(outcome.notes)
        parse_note = understood.parse_note()
        if parse_note:
            notes.append(parse_note)

        session.phase = outcome.phase
        clarification = outcome.clarification
        if clarification is None and outcome.next_action == NextAction.ANSWER_QUESTION:
            clarification = understood.default_clarification()

        # Rule 5: never claim to be asking something without a question to ask.
        next_action = outcome.next_action
        if next_action == NextAction.ANSWER_QUESTION and clarification is None:
            next_action = NextAction.NONE

        response = AgentResponse(
            session_id=session.session_id,
            request_id=request_id,
            message=outcome.message,
            phase=outcome.phase,
            next_action=next_action,
            intent=understood.intent,
            confidence=understood.confidence,
            parse_source=understood.parse_source,
            source_spans=dict(understood.source_spans),
            untraceable_fields=list(understood.untraceable),
            clarification=clarification,
            ambiguities=list(understood.ambiguities),
            constraints=session.constraints,
            profile=(session.profile if not session.profile.is_empty() else None),
            results=session.last_results if "results" in outcome.produced else None,
            selected_product_id=(
                outcome.selected_product_id
                if outcome.selected_product_id is not None
                else session.selected_product_id
            ),
            mandate_draft=(outcome.mandate_draft
                           or (session.mandate_draft
                               if "mandate_draft" in outcome.produced else None)),
            quote=outcome.quote,
            mandate=outcome.mandate,
            spend_state=outcome.spend_state,
            reservation=outcome.reservation,
            decision=outcome.decision,
            denial=outcome.denial,
            escalation=outcome.escalation,
            receipt=outcome.receipt,
            payment_failure=outcome.payment_failure,
            notes=notes,
            trace=trace,
        )
        if (self._explainer is not None and understood.parse_source is not None
                and "catalog_navigation" not in outcome.notes
                and not understood.untraceable
                and understood.intent in {Intent.SEARCH, Intent.UPDATE_SEARCH,
                    Intent.ASK_ALTERNATIVES, Intent.REJECT_RECOMMENDATION, Intent.UNKNOWN}
                and not any((response.quote, response.mandate, response.mandate_draft,
                             response.receipt, response.decision, response.escalation))):
            try:
                message = self._explainer.explain(response=response, session=session,
                                                  user_text=understood.spoken_as)
                response = response.model_copy(update={"message": message,
                    "trace": [*response.trace, ParserAttempt(source=ParseSource.LLM, ok=True,
                                               detail="grounded_explanation")]})
            except Exception as exc:
                # Never echo provider content or credentials into the UI.
                response = response.model_copy(update={
                    "message": (response.message + "\n模型这轮未能完成评论解释，可以在“评论与水军分析”页查看原文和已计算的分析依据。"
                                if understood.intent == Intent.ASK_ALTERNATIVES else response.message),
                    "notes": [*response.notes, "模型解释本轮不可用，已保留数据库核验的回答。"],
                    "trace": [*response.trace, ParserAttempt(source=ParseSource.LLM, ok=False,
                        detail="grounded_explanation_failed: " + type(exc).__name__)]})
        session.record_turn(Turn(index=session.next_turn_index(),
            user_text=understood.spoken_as, assistant_message=response.message,
            at=_utc(), intent=understood.intent, parse_source=understood.parse_source,
            produced=list(outcome.produced)))
        return response


@dataclass(frozen=True)
class _Understood:
    """What the turn was read as, from either a parse or a button.

    The two paths produce the same fields, so the response assembly does not
    have to branch on which one ran -- and a bug in one cannot make the other
    report something different about the same turn.
    """

    spoken_as: str
    intent: Intent
    confidence: float | None = None
    parse_source: ParseSource | None = None
    source_spans: dict[str, str] = field(default_factory=dict)
    untraceable: list[str] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    clarification: Clarification | None = None

    @classmethod
    def of(cls, result: IntentResult) -> "_Understood":
        return cls(
            spoken_as=result.raw_text,
            intent=result.intent,
            confidence=result.confidence,
            parse_source=result.parse_source,
            source_spans=dict(result.source_spans),
            untraceable=result.untraceable_fields(),
            ambiguities=list(result.ambiguities),
            clarification=result.clarification(),
        )

    @classmethod
    def of_action(cls, request: AgentActionRequest) -> "_Understood":
        # A click carries no prose. Recorded as the intent the button stood for,
        # so the transcript reads as a conversation rather than as a gap.
        return cls(spoken_as=f"[{request.intent.value}]", intent=request.intent)

    def parse_note(self) -> str | None:
        return render.describe_parse_source(self.parse_source)

    def default_clarification(self) -> Clarification | None:
        return self.clarification


__all__ = ["AgentOrchestrator"]
