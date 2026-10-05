"""The half of a turn that touches C: authorising, pricing, and buying.

Same shape as :mod:`app.agent.browsing`: every method returns a
:class:`~app.agent.turn.TurnOutcome`, or ``None`` to mean "this turn is not mine
to answer". That keeps the decision about what to ask in one place instead of
duplicated into each handler.

What this module is careful about, and why each of them is a decision:

**It asks C for the number, and shows C's number.** Selecting a product calls
``create_quote`` and renders the ``Quote`` verbatim. A never adds a shipping
estimate, never rounds, and never says "about". The price on screen is the price
C will enforce, and the difference between those two is the difference between a
shop and a guess.

**It submits and then reads back.** ``submit_proposal`` returns a decision, and
that is all the frozen interface promises. Everything after the decision --
the reservation, the capability, the payment, the receipt -- happens inside C,
and A learns about it by asking, not by being told. That is deliberate: A has no
call that can trigger a payment, so the only way for a purchase to happen is for
C to decide it may.

**It never converts a refusal into a different refusal.** A denial, an
unanswered question and a failed payment are three different things with three
different next steps, and they are rendered from three different C objects. A
turn that says "there was a problem" is a turn that has thrown away the only
part the user can act on.

**It re-reads the mandate rather than trusting its memory.** The session holds a
version number, and it is sent back as ``expected_mandate_version`` so that a
revocation between two turns lands on the purchase instead of being missed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.agent import mandate_flow as mandate
from app.agent import response_renderer as render
from app.agent.clients import CommerceClient
from app.agent.session import AgentSession
from app.agent.turn import TurnOutcome
from app.contracts.agent import Clarification, IntentResult
from app.contracts.commerce import PurchaseProposal
from app.contracts.common import AgentPhase, ErrorCode, NextAction, SourceType, new_request_id
from app.contracts.mandate import Mandate, MandateDraft
from app.contracts.policy import ProposalOutcome


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_id() -> str:
    """A fresh identifier for one purchase attempt.

    Random rather than counted, and for the same reason C's ids are: a counter
    restarts with the process, and a proposal is a durable record.
    """
    return f"prop_{uuid.uuid4().hex[:12]}"


class CommerceTurns:
    """Authorisation, pricing and the purchase itself, through C's interface."""

    def __init__(
        self,
        commerce: CommerceClient,
        *,
        principal_id: str,
        agent_id: str,
        shipping_address_id: str,
        address_change_allowed: bool = False,
    ) -> None:
        self._commerce = commerce
        self._principal_id = principal_id
        self._agent_id = agent_id
        self._shipping_address_id = shipping_address_id
        self._address_change_allowed = address_change_allowed

    # -- the mandate --------------------------------------------------------

    def draft_mandate(self, session: AgentSession, result: IntentResult,
                      patch: MandateDraft | None) -> TurnOutcome:
        """Fold this turn's reading into the draft, then either ask or summarise.

        The merge is deterministic and lives in ``mandate_flow``. What happens
        here is the two-way branch: an incomplete draft gets exactly one
        question, and a complete one gets the summary it will be signed from.
        """
        draft = mandate.merge_draft(session.mandate_draft, patch or MandateDraft())
        draft = mandate.propose_clauses(
            draft,
            merchant_of_record=self._commerce.merchant_of_record(),
            payment_routes=self._commerce.payment_routes(),
            shipping_address_id=self._shipping_address_id,
            address_change_allowed=self._address_change_allowed,
        )
        session.mandate_draft = draft
        if patch is not None and patch.allowed_payment_routes:
            # Keep what the user actually asked for, so the proposal that
            # follows can express it rather than only what the mandate permits.
            session.preferred_route_ids = mandate.canonical_payment_routes(
                patch.allowed_payment_routes, self._commerce.payment_routes())

        route_problem = self._unavailable_routes(draft)
        if route_problem is not None:
            return route_problem

        clarification = mandate.clarification_for(draft)
        if clarification is not None:
            return TurnOutcome(
                message="\n".join([
                    render.describe_constraints(session.constraints),
                    mandate.describe_gaps(draft),
                    clarification.question,
                ]),
                next_action=NextAction.CONFIRM_MANDATE,
                phase=AgentPhase.BUILDING_MANDATE,
                produced=["mandate_draft"],
                mandate_draft=draft,
                notes=render.describe_ambiguities(mandate.ambiguities_for(draft)),
                clarification=clarification,
            )

        return TurnOutcome(
            message="\n".join([
                mandate.describe_summary(draft),
                "确认的话我就去激活这份授权；要改哪一条直接说。",
            ]),
            next_action=NextAction.CONFIRM_MANDATE,
            phase=AgentPhase.AWAITING_MANDATE_CONFIRMATION,
            produced=["mandate_draft"],
            mandate_draft=draft,
            notes=["mandate draft complete; waiting for the user to sign it"],
            clarification=Clarification(
                question="要按这份授权激活吗？", about=[], blocking=False),
        )

    def activate_mandate(self, session: AgentSession) -> TurnOutcome:
        """Hand the signed draft to C. A produces the draft; C produces the mandate."""
        if session.mandate_draft is None:
            return self._nothing_to_activate()

        route_problem = self._unavailable_routes(session.mandate_draft)
        if route_problem is not None:
            return route_problem

        found = mandate.gaps(session.mandate_draft)
        if not found.is_complete:
            # Belt and braces: the summary is only shown for a complete draft, but
            # a mandate that activated with a clause missing would authorise
            # spending under terms nobody agreed to.
            question = mandate.clarification_for(session.mandate_draft)
            return TurnOutcome(
                message="\n".join([mandate.describe_gaps(session.mandate_draft),
                                   question.question if question else ""]),
                next_action=NextAction.CONFIRM_MANDATE,
                phase=AgentPhase.AWAITING_MANDATE_CONFIRMATION,
                clarification=question,
            )

        activated: Mandate = self._commerce.activate_mandate(
            session.mandate_draft,
            principal_id=self._principal_id,
            agent_id=self._agent_id,
        )
        session.mandate_id = activated.mandate_id
        session.mandate_version = activated.version
        session.last_mandate = activated
        return TurnOutcome(
            message="\n".join([
                render.describe_mandate(activated),
                "之后就按这份授权执行；想撤销随时说「撤销授权」。",
            ]),
            next_action=NextAction.NONE,
            phase=AgentPhase.MANDATE_ACTIVE,
            produced=["mandate"],
            mandate=activated,
            notes=[f"activated by C as {activated.mandate_id} v{activated.version}"],
        )

    def _unavailable_routes(self, draft: MandateDraft) -> TurnOutcome | None:
        available = self._commerce.payment_routes()
        unknown = [r for r in (draft.allowed_payment_routes or []) if r not in available]
        if not unknown:
            return None
        question = ("这份授权里的付款方式暂不可用：" + "、".join(unknown)
                    + "。请改用已配置的模拟付款方式：" + "、".join(available) + "。")
        return TurnOutcome(
            message=question, next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.BUILDING_MANDATE, mandate_draft=draft,
            produced=["mandate_draft"],
            clarification=Clarification(question=question,
                                      about=["allowed_payment_routes"], blocking=True),
        )

    def _nothing_to_activate(self) -> TurnOutcome:
        return TurnOutcome(
            message="还没有要激活的授权。先说清楚要授权的范围（比如「每笔不超过 300」），我整理出来给你确认。",
            next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.AWAITING_REQUIREMENTS,
            clarification=Clarification(
                question="要授权我花多少钱、买什么？", about=[], blocking=False),
        )

    def revoke_mandate(self, session: AgentSession) -> TurnOutcome:
        """Withdraw the authorisation. A new version, and C decides what that means."""
        if session.mandate_id is None:
            return TurnOutcome(
                message="现在没有生效中的授权。",
                next_action=NextAction.NONE,
                phase=session.phase,
            )
        revoked = self._commerce.revoke_mandate(session.mandate_id)
        session.mandate_version = revoked.version
        session.last_mandate = revoked
        return TurnOutcome(
            message="\n".join([
                f"授权已撤销（版本 {revoked.version}）。",
                "已经付款成功的交易不会被取消，也不会退款——撤销的是之后的授权。",
            ]),
            next_action=NextAction.NONE,
            phase=AgentPhase.CANCELLED,
            produced=["mandate"],
            mandate=revoked,
            notes=["revocation adds a version; settled payments are unaffected"],
        )

    # -- one product ---------------------------------------------------------

    def price(self, session: AgentSession, product_id: str,
              quantity: int = 1) -> TurnOutcome:
        """Ask C to price the chosen product, and show exactly what came back.

        The quote comes back before anything is submitted, so the user sees the
        landed total -- shipping included -- and the decision to continue is made
        against a number rather than against a catalog price.
        """
        from app.errors import AgentError

        product = session.product(product_id)
        headline = (
            f"{product.name}（{product.brand}）" if product else product_id
        )
        try:
            quote = self._commerce.create_quote(product_id=product_id, quantity=quantity)
        except AgentError as exc:
            if exc.code is ErrorCode.OUT_OF_STOCK:
                return TurnOutcome(
                    message=f"{headline} 现在缺货，换一款吧。",
                    next_action=NextAction.ANSWER_QUESTION,
                    phase=AgentPhase.SHOWING_PRODUCTS,
                    notes=[f"{exc.code.value}: {exc.message}"],
                    clarification=Clarification(
                        question="要我按同样的条件再找几款吗？", about=[], blocking=False),
                )
            raise

        session.select(product_id, quote)
        return TurnOutcome(
            message="\n".join([
                f"选中 {headline}。",
                render.describe_quote(quote),
                "要我按这份授权把这一笔交出去吗？",
            ]),
            next_action=NextAction.RUN_PURCHASE,
            phase=AgentPhase.SHOWING_PRODUCTS,
            produced=["quote"],
            quote=quote,
            notes=["priced by C; the amount shown is the one C will enforce"],
            selected_product_id=product_id,
            clarification=Clarification(
                question="要执行这一笔吗？", about=[], blocking=False),
        )

    # -- the purchase --------------------------------------------------------

    def run_purchase(self, session: AgentSession) -> TurnOutcome:
        """Build a proposal, submit it, and explain C's answer."""
        if session.mandate_id is None or session.mandate_version is None:
            return TurnOutcome(
                message="还没有生效的授权，所以不能下单。先授权，再选商品。",
                next_action=NextAction.CONFIRM_MANDATE,
                phase=AgentPhase.AWAITING_REQUIREMENTS,
                clarification=Clarification(
                    question="要现在整理一份授权吗？", about=[], blocking=False),
            )
        if session.selected_product_id is None or session.pending_quote is None:
            if session.has_shown_products:
                return TurnOutcome(
                    message="还没选具体哪一款。说「第几款」就行。",
                    next_action=NextAction.SELECT_PRODUCT,
                    phase=AgentPhase.SHOWING_PRODUCTS,
                    clarification=Clarification(
                        question="要买哪一款？", about=["product_id"], blocking=False),
                )
            return TurnOutcome(
                message="先挑一款再说下单吧。",
                next_action=NextAction.ANSWER_QUESTION,
                phase=AgentPhase.AWAITING_REQUIREMENTS,
                clarification=Clarification(
                    question="想找什么样的耳机？", about=[], blocking=False),
            )

        quote = session.pending_quote
        # One key per submission, drawn once. A deliberate second submission of
        # the same purchase needs a fresh key -- C's idempotency rule answers a
        # replay with what it recorded, and this time the answer is meant to be
        # different, because the principal has said yes.
        idempotency_key = session.next_idempotency_key()
        resubmission = self._resubmission_of(session, quote)
        proposal = (resubmission.model_copy(update={"idempotency_key": idempotency_key})
                    if resubmission is not None
                    else PurchaseProposal(
                        proposal_id=_proposal_id(),
                        mandate_id=session.mandate_id,
                        expected_mandate_version=session.mandate_version,
                        principal_id=self._principal_id,
                        agent_id=self._agent_id,
                        product_id=session.selected_product_id,
                        quantity=quote.quantity,
                        # Transcribed from C's own quote, not chosen by A: the
                        # merchant of record is C's fact, and C re-checks it
                        # against the mandate.
                        merchant_id=quote.merchant_id,
                        quote_id=quote.quote_id,
                        preferred_payment_route_ids=list(session.preferred_route_ids),
                        # Transcribed from the activated mandate, so the address
                        # the evaluator compares is the one the user signed.
                        shipping_address_id=(
                            session.last_mandate.shipping_address_id
                            if session.last_mandate else self._shipping_address_id
                        ),
                        request_id=new_request_id(),
                        idempotency_key=idempotency_key,
                        created_at=_utc(),
                    ))

        decision = self._commerce.submit_proposal(proposal)
        session.pending_proposal = proposal
        session.pending_proposal_id = proposal.proposal_id
        outcome = self._commerce.get_proposal_outcome(proposal.proposal_id)
        return self.explain(session, proposal, decision.outcome, outcome)

    @staticmethod
    def _resubmission_of(session: AgentSession, quote) -> PurchaseProposal | None:
        """The proposal still in flight for this quote, if there is one.

        An ``ApprovalGrant`` names one ``proposal_id``, so a purchase the
        principal has just approved has to be submitted *again* rather than
        replaced: a fresh proposal is a different purchase, the grant does not
        cover it, and C would -- correctly -- ask for approval a second time.
        Cleared as soon as the attempt is over, so an ordinary retry after a
        refusal does build a new one.
        """
        pending = session.pending_proposal
        if pending is None or pending.quote_id != quote.quote_id:
            return None
        return pending

    def explain(self, session: AgentSession, proposal: PurchaseProposal,
                outcome_word: str, outcome: ProposalOutcome | None) -> TurnOutcome:
        """Turn one of C's three answers into the reply and the next control.

        Kept in one place so that the four cases cannot drift apart: a denial
        that rendered like a success, or an escalation that offered a "pay"
        button, would each be a way for the interface to describe a system that
        does not exist.
        """
        decision = outcome.decision if outcome else None
        produced = ["decision"]

        if outcome_word == "DENY":
            session.forget_purchase()
            denial = outcome.denial if outcome else None
            lines = ["这笔没有通过。"]
            if denial is not None:
                lines.append(render.describe_denial(denial))
            elif decision is not None:
                lines.append(render.describe_decision(decision))
            lines.append("可以改条件、或者换个商品再试。")
            return TurnOutcome(
                message="\n".join(lines),
                next_action=NextAction.NONE,
                phase=AgentPhase.DENIED,
                produced=[*produced, "denial"],
                decision=decision, denial=denial,
                notes=["C denied the purchase; nothing was reserved"],
            )

        if outcome_word == "ESCALATE":
            escalation = outcome.escalation if outcome else None
            session.open_escalation_proposal_id = proposal.proposal_id
            return TurnOutcome(
                message="\n".join([
                    "这笔金额超过了你设定的门槛，需要你确认。",
                    render.describe_escalation(escalation) if escalation
                    else render.describe_decision(decision),
                ]),
                next_action=NextAction.APPROVE_ESCALATION,
                phase=AgentPhase.AWAITING_ESCALATION,
                produced=[*produced, "escalation"],
                decision=decision, escalation=escalation,
                notes=["C is asking the principal; nothing is reserved yet"],
            )

        # APPROVE. C has already reserved and settled inside submit_proposal.
        session.forget_purchase()

        if outcome is not None and outcome.receipt is not None:
            lines = [
                "已按授权完成这一笔。",
                render.describe_receipt(outcome.receipt),
                render.describe_sandbox_settlement()
                if outcome.settlement_source_type is SourceType.SANDBOX else "",
            ]
            return TurnOutcome(
                message="\n".join(line for line in lines if line),
                next_action=NextAction.NONE,
                phase=AgentPhase.COMPLETED,
                produced=[*produced, "receipt"],
                decision=decision, reservation=outcome.reservation,
                receipt=outcome.receipt,
                notes=["reserved and settled by C; A triggered no payment"],
            )

        if outcome is not None and outcome.payment_failure is not None:
            failure = outcome.payment_failure
            unknown = failure.code is ErrorCode.PAYMENT_STATUS_UNKNOWN
            return TurnOutcome(
                message="\n".join([
                    "这笔通过了授权，但付款没有完成。",
                    render.describe_payment_failure(failure),
                ]),
                next_action=NextAction.NONE,
                phase=AgentPhase.PAYMENT_UNKNOWN if unknown else AgentPhase.DENIED,
                produced=[*produced, "payment_failure"],
                decision=decision, reservation=outcome.reservation,
                payment_failure=failure,
                notes=[f"settlement refusal: {failure.code.value}"],
            )

        # Approved with no receipt and no failure: the reservation exists and the
        # settlement has not been read back yet.
        return TurnOutcome(
            message="\n".join([
                render.describe_decision(decision) if decision else "已通过。",
                render.describe_reservation(outcome.reservation) if outcome
                and outcome.reservation else "",
            ]),
            next_action=NextAction.CHECK_STATUS,
            phase=AgentPhase.PAYMENT_RESERVED,
            produced=[*produced, "reservation"],
            decision=decision,
            reservation=outcome.reservation if outcome else None,
            notes=["reserved by C; settlement outcome not yet recorded"],
        )

    # -- the escalation ------------------------------------------------------

    def answer_escalation(self, session: AgentSession, *, approve: bool) -> TurnOutcome:
        """Relay the principal's yes or no. C records it and acts on it."""
        proposal_id = session.open_escalation_proposal_id
        if proposal_id is None:
            return TurnOutcome(
                message="现在没有等你确认的交易。",
                next_action=NextAction.NONE,
                phase=session.phase,
            )

        if approve:
            self._commerce.approve_escalation(
                proposal_id, principal_id=self._principal_id)
            session.open_escalation_proposal_id = None
            return TurnOutcome(
                message="已记下你的同意。这笔还需要重新提交一次，我这就去执行。",
                next_action=NextAction.RUN_PURCHASE,
                phase=AgentPhase.RUNNING_DELEGATED_PURCHASE,
                produced=["grant"],
                notes=["C issued a single-use grant bound to this exact purchase"],
            )

        escalation = self._commerce.reject_escalation(
            proposal_id, principal_id=self._principal_id)
        session.open_escalation_proposal_id = None
        session.forget_purchase()
        return TurnOutcome(
            message="已记下你的拒绝，这笔不会执行。",
            next_action=NextAction.NONE,
            phase=AgentPhase.DENIED,
            produced=["escalation"],
            escalation=escalation,
            notes=[f"C recorded the answer as {escalation.resolution}"],
        )

    # -- status --------------------------------------------------------------

    def status(self, session: AgentSession) -> TurnOutcome:
        """What C currently holds for this principal: the mandate, the spending.

        Read from C on every call rather than from the session. A cached
        remaining budget is exactly the number that is wrong when it matters.
        """
        if session.mandate_id is None:
            return TurnOutcome(
                message="现在没有生效的授权，也没有已经发生的交易。",
                next_action=NextAction.CONFIRM_MANDATE,
                phase=AgentPhase.AWAITING_REQUIREMENTS,
                clarification=Clarification(
                    question="要现在整理一份授权吗？", about=[], blocking=False),
            )

        current = self._commerce.get_mandate(session.mandate_id)
        if current is None:  # pragma: no cover - C keeps every version
            return TurnOutcome(
                message="这份授权在交易层已经查不到了。",
                next_action=NextAction.NONE,
                phase=AgentPhase.CANCELLED,
            )

        session.mandate_version = current.version
        session.last_mandate = current
        state = self._commerce.spend_state(current)
        lines = [
            f"授权状态：{current.status}，版本 {current.version}，"
            f"规则指纹 {current.policy_hash}",
            f"额度使用：{render.describe_spend_state(state)}"
            f"（单笔上限 {render.money(current.cap_per_transaction_cents)}，"
            f"累计上限 {render.money(current.rolling_cap_cents)}）",
        ]
        if session.pending_proposal_id:
            outcome = self._commerce.get_proposal_outcome(session.pending_proposal_id)
            if outcome is not None:
                lines.append(f"最近一笔：{outcome.decision.outcome}")
        return TurnOutcome(
            message="\n".join(lines),
            next_action=NextAction.NONE,
            phase=AgentPhase.MANDATE_ACTIVE,
            produced=["spend_state"],
            mandate=current, spend_state=state,
            notes=["read from C, not from the session"],
        )


__all__ = ["CommerceTurns"]
