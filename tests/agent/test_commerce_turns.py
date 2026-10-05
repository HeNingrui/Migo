"""The conversation, wired to C: authorise, choose, buy, be stopped, explain.

This is the A + C integration suite. It drives the real orchestrator against the
real :class:`~app.commerce.CommerceService` on a temporary database, so every
number the user sees has travelled the whole path: D's catalog, C's quote, C's
decision, C's receipt. Nothing here is stubbed except B, which is still a
stand-in and is marked as one.

What it is really checking is that A never becomes C. Four claims:

* **the summary is signed, not assumed.** Activating without a complete draft
  asks; activating with one hands C the draft and shows back C's version and
  hash.
* **the price is C's.** Selecting calls ``create_quote``; the amount in the
  reply is the quote's field, and the reply carries the quote object itself.
* **the outcome is C's.** Approved, denied and escalated are three different
  replies built from three different C objects, and a payment that failed is a
  fourth that is not a policy denial.
* **nothing A holds is authority.** The session keeps an id and a version; the
  version is what makes a revocation between two turns land on the purchase.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.agent.local_search import LocalSearchClient  # noqa: E402
from app.agent.orchestrator import AgentOrchestrator  # noqa: E402
from app.agent.response_renderer import money  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.commerce.database import read_connection  # noqa: E402
from app.commerce.repositories import WalletRepository  # noqa: E402
from app.commerce.schema import (  # noqa: E402
    create_commerce_schema,
    initialize_wallets,
)
from app.commerce.service import CommerceService  # noqa: E402
from app.contracts.agent import AgentActionRequest, ChatRequest, Intent  # noqa: E402
from app.contracts.common import AgentPhase, NextAction  # noqa: E402
from app.db import initialize_database  # noqa: E402

#: What the user types to authorise. Every money clause the draft requires is in
#: it, because the parser will not invent one and neither will A.
AUTHORISATION = (
    "授权你在这段时间里替我买耳机：每笔不超过 320，24 小时内总共不超过 600，"
    "5 分钟最多 2 笔，一共买 3 件，超过 300 先问我，只用 FPS，"
    "送到 addr_demo_01，有效期 7 天"
)

#: Wireless, ANC, in stock, and priced inside the mandate above. hp_0018 lands at
#: HK$289 (below the ask-me line), hp_0007 at HK$310 (above it, under the cap).
SEARCH = "找 350 以内、无线、必须支持主动降噪的耳机"

#: A wider search, used to reach the product that exceeds the per-transaction cap.
WIDE_SEARCH = "找 600 以内、无线、必须支持主动降噪的耳机"

OVER_CAP = "hp_0003"
AT_THE_THRESHOLD = "hp_0007"
UNDER_THE_THRESHOLD = "hp_0018"


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "ac.sqlite3"
    initialize_database(
        path, commerce_schema=create_commerce_schema,
        wallet_initializer=initialize_wallets,
    )
    return path


@pytest.fixture
def orchestrator(db_path) -> AgentOrchestrator:
    return AgentOrchestrator(
        sessions=SessionStore(),
        search=LocalSearchClient(),
        commerce=CommerceService(db_path=db_path),
    )


@pytest.fixture
def chat(orchestrator) -> "Chat":
    return Chat(orchestrator)


class Chat:
    """One conversation.

    A helper rather than a bare function on purpose: the second turn is the whole
    point of this system, and a helper that quietly started a new session per
    call would make every multi-turn test pass for the wrong reason.
    """

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator
        self.session_id: str | None = None

    def say(self, *turns):
        responses = []
        for text in turns:
            response = self.orchestrator.handle(
                ChatRequest(session_id=self.session_id, message=text))
            self.session_id = response.session_id
            responses.append(response)
        return responses

    def click(self, intent, **handles):
        return self.orchestrator.handle_action(AgentActionRequest(
            session_id=self.session_id, intent=intent, **handles))

    def authorise(self):
        """Draft, summarise and sign. Returns the two responses."""
        draft, signed = self.say(AUTHORISATION, "确认授权")
        assert signed.mandate is not None, "the mandate must come back from C"
        return draft, signed

    def choose(self, product_id: str):
        """Pick a specific product the way a frontend would: by id.

        The conversation resolves a rank against what was shown, which
        ``test_orchestrator`` covers. These tests need a named product, so they
        use the path a control bound to an id would use.
        """
        self.say(WIDE_SEARCH)
        return self.click(Intent.SELECT_PRODUCT, product_id=product_id)


class TestTheMandateIsSigned:
    def test_an_authorisation_becomes_a_summary_before_it_becomes_a_mandate(self, chat):
        """Nothing is activated by being asked for.

        The draft is A's; the mandate is C's; and the step between them is a
        confirmation, which is the whole reason the user is shown a summary.
        """
        (draft,) = chat.say(AUTHORISATION)
        assert draft.intent is Intent.CREATE_MANDATE
        assert draft.mandate is None, "a draft is not an active authorisation"
        assert draft.mandate_draft is not None
        assert draft.next_action is NextAction.CONFIRM_MANDATE
        assert draft.phase is AgentPhase.AWAITING_MANDATE_CONFIRMATION
        assert draft.requires_user_action() is True, "there is a signature to give"
        assert "单笔上限" in draft.message

    def test_the_summary_marks_what_a_proposed(self, chat):
        """The clauses A filled in are shown as proposals, not as the user's words."""
        (draft,) = chat.say(AUTHORISATION)
        assert "（我建议的）" in draft.message

    def test_every_amount_on_the_summary_traces_to_the_user(self, chat):
        (draft,) = chat.say(AUTHORISATION)
        assert draft.untraceable_fields == []
        assert draft.mandate_draft.cap_per_transaction_cents == 32000
        assert draft.mandate_draft.rolling_cap_cents == 60000
        assert draft.mandate_draft.escalate_above_cents == 30000
        assert draft.mandate_draft.rolling_window_seconds == 86400
        assert draft.mandate_draft.velocity_max_count == 2

    def test_an_incomplete_draft_is_a_question_rather_than_a_mandate(self, chat):
        (draft,) = chat.say("授权你以后帮我买耳机，超过 200 先问我")
        assert draft.mandate is None
        assert draft.mandate_draft.cap_per_transaction_cents is None
        assert draft.clarification is not None
        assert draft.next_action is NextAction.CONFIRM_MANDATE

    def test_signing_hands_the_draft_to_c_and_shows_c_s_version(self, chat):
        _, signed = chat.authorise()
        assert signed.mandate.version == 1
        assert signed.mandate.status == "ACTIVE"
        assert signed.mandate.policy_hash.startswith("sha256:")
        assert signed.mandate.policy_hash in signed.message, (
            "the hash is only an answer if the user was told which value to quote"
        )
        assert signed.phase is AgentPhase.MANDATE_ACTIVE

    def test_a_confirmation_with_no_draft_does_not_activate_anything(self, chat):
        """The click is a fact, but it has to be a click about something."""
        (response,) = chat.say("确认")
        assert response.mandate is None
        assert response.intent is not Intent.ACTIVATE_MANDATE

    def test_a_revocation_is_a_new_version_and_says_what_it_cannot_undo(self, chat):
        _, signed = chat.authorise()
        (revoked,) = chat.say("撤销授权")
        assert revoked.mandate.version == 2
        assert revoked.mandate.status == "REVOKED"
        assert revoked.mandate.policy_hash == signed.mandate.policy_hash
        assert "不会退款" in revoked.message, (
            "a revocation must not be presented as a refund"
        )


class TestThePriceComesFromC:
    def test_selecting_prices_the_product_and_shows_c_s_quote(self, chat):
        chat.authorise()
        found, chosen = chat.say(SEARCH, "第二款")
        assert chosen.selected_product_id == found.results.candidates[1].product_id
        assert chosen.quote is not None
        assert chosen.quote.product_id == chosen.selected_product_id
        assert chosen.quote.merchant_total_cents == 29900 + 1000
        assert chosen.phase is AgentPhase.SHOWING_PRODUCTS
        assert chosen.next_action is NextAction.RUN_PURCHASE

    def test_the_amount_in_the_reply_is_the_quote_s_own_field(self, chat):
        """The message rule: every number comes from a field on the response."""
        chat.authorise()
        _, chosen = chat.say(SEARCH, "第一款")
        assert money(chosen.quote.merchant_total_cents) in chosen.message
        assert money(chosen.quote.shipping_cents) in chosen.message

    def test_the_shipping_rate_is_labelled_as_a_sandbox_parameter(self, chat):
        chat.authorise()
        _, chosen = chat.say(SEARCH, "第一款")
        assert "SANDBOX" in chosen.message

    def test_the_price_is_the_catalog_price_plus_the_sandbox_rate(self, chat):
        """A never states a price, and never attaches a budget of its own."""
        chat.authorise()
        _, chosen = chat.say(SEARCH, "第一款")
        assert chosen.quote.unit_price_cents == 27900
        assert chosen.quote.merchant_total_cents == 28900


class TestTheOutcomeComesFromC:
    def test_an_approved_purchase_settles_and_shows_the_receipt(self, chat):
        chat.authorise()
        chosen = chat.choose(UNDER_THE_THRESHOLD)
        assert chosen.quote.merchant_total_cents == 28900

        (bought,) = chat.say("买吧")
        assert bought.decision.outcome == "APPROVE"
        assert bought.receipt is not None
        assert bought.receipt.cash_total_cents == 28900
        assert bought.receipt.merchant_total_cents == 28900, "shipping is part of the merchant total"
        assert bought.reservation.status == "SETTLED"
        assert bought.phase is AgentPhase.COMPLETED
        assert bought.next_action is NextAction.NONE
        assert "SANDBOX" in bought.message, (
            "a sandbox settlement must not read as a real one"
        )

    def test_the_receipt_says_what_left_the_account(self, chat):
        chat.authorise()
        chat.choose(UNDER_THE_THRESHOLD)
        (bought,) = chat.say("买吧")
        assert bought.receipt.balance_after_cents == 500000 - 28900
        assert "4,711.00" in bought.message

    def test_a_denied_purchase_carries_the_denial_and_no_receipt(self, chat):
        """The plan's "show it being stopped", from the conversation's side."""
        chat.authorise()
        chosen = chat.choose(OVER_CAP)
        assert chosen.quote.merchant_total_cents == 50900

        (denied,) = chat.say("买吧")
        assert denied.decision.outcome == "DENY"
        assert denied.denial is not None
        assert denied.denial.primary_reason.value == "CAP_PER_TRANSACTION_EXCEEDED"
        assert denied.receipt is None
        assert denied.reservation is None
        assert denied.phase is AgentPhase.DENIED
        assert "HK$509.00" in denied.message
        assert "HK$320.00" in denied.message

    def test_an_escalated_purchase_asks_and_holds_nothing(self, chat):
        chat.authorise()
        chosen = chat.choose(AT_THE_THRESHOLD)
        assert chosen.quote.merchant_total_cents == 31000

        (asked,) = chat.say("买吧")
        assert asked.decision.outcome == "ESCALATE"
        assert asked.escalation is not None
        assert asked.escalation.cash_total_cents == 31000
        assert asked.escalation.escalate_above_cents == 30000
        assert asked.receipt is None
        assert asked.phase is AgentPhase.AWAITING_ESCALATION
        assert asked.next_action is NextAction.APPROVE_ESCALATION
        assert asked.requires_user_action() is True

    def test_answering_yes_runs_the_purchase_once(self, chat):
        """Approval and execution are two C calls, not one.

        C records the answer as a single-use grant, and the purchase is a
        separate submission that C re-evaluates against it. Folding them together
        would be A deciding that an approval implies a purchase.
        """
        chat.authorise()
        chat.choose(AT_THE_THRESHOLD)
        chat.say("买吧")

        (settled,) = chat.say("同意")
        assert settled.receipt is not None
        assert settled.receipt.cash_total_cents == 31000
        assert settled.phase is AgentPhase.COMPLETED
        assert settled.receipt.merchant_total_cents == 31000

    def test_answering_no_leaves_the_money_alone(self, chat, db_path):
        chat.authorise()
        chat.choose(AT_THE_THRESHOLD)
        chat.say("买吧")

        (refused,) = chat.say("算了")
        assert refused.phase is AgentPhase.DENIED
        assert refused.receipt is None
        assert refused.escalation.resolution == "REJECTED"
        with read_connection(db_path) as conn:
            assert WalletRepository().balance(conn, "demo_user") == 500000


class TestTheButtonPath:
    """A click is a click: explicit actions skip the parser entirely."""

    def test_a_button_selects_by_product_id_rather_than_by_position(self, chat):
        """The frontend binds a control to an id, which is why the id is carried."""
        chat.authorise()
        (found,) = chat.say(SEARCH)
        product_id = found.results.candidates[0].product_id

        response = chat.click(Intent.SELECT_PRODUCT, product_id=product_id)
        assert response.selected_product_id == product_id
        assert response.quote is not None
        assert response.parse_source is None
        assert response.trace == []

    def test_a_button_activates_the_mandate(self, chat):
        """The consent gate has to be expressible as a click.

        ``requires_user_action()`` promises a control for ``CONFIRM_MANDATE``,
        and at that moment there is no mandate to name -- which is why
        ``ACTIVATE_MANDATE`` does not require ``mandate_id``.
        """
        chat.say(AUTHORISATION)
        response = chat.click(Intent.ACTIVATE_MANDATE)
        assert response.mandate is not None
        assert response.phase is AgentPhase.MANDATE_ACTIVE

    def test_a_button_naming_another_sessions_mandate_is_refused(self, chat):
        chat.say(AUTHORISATION)
        with pytest.raises(Exception) as caught:
            chat.click(Intent.ACTIVATE_MANDATE, mandate_id="man_somewhere_else")
        assert "does not hold that mandate" in str(caught.value)

    def test_a_button_runs_the_purchase(self, chat):
        chat.authorise()
        (found,) = chat.say(SEARCH)
        chat.click(Intent.SELECT_PRODUCT,
                   product_id=found.results.candidates[0].product_id)
        response = chat.click(Intent.RUN_DELEGATED_PURCHASE)
        assert response.receipt is not None

    def test_a_button_approves_the_escalation_it_named(self, chat):
        chat.authorise()
        chat.choose(AT_THE_THRESHOLD)
        asked = chat.say("买吧")[0]

        response = chat.click(Intent.APPROVE_ESCALATION,
                              proposal_id=asked.escalation.proposal_id)
        assert response.receipt is not None
        assert response.receipt.cash_total_cents == 31000


class TestTheSessionHoldsNoAuthority:
    def test_a_revocation_between_two_turns_lands_on_the_purchase(self, chat):
        """The stale-version case, from the conversation's side.

        A holds a version number, sends it back, and C refuses because it is no
        longer current. A never re-checks the mandate itself -- it could not do
        so correctly, and a cache that is right most of the time is a cache that
        is wrong once.
        """
        chat.authorise()
        chat.choose(UNDER_THE_THRESHOLD)
        chat.say("撤销授权")

        (refused,) = chat.say("买吧")
        assert refused.decision.outcome == "DENY"
        assert refused.receipt is None
        assert refused.denial.primary_reason.value in (
            "MANDATE_REVOKED", "MANDATE_VERSION_STALE",
        )

    def test_status_is_read_from_c_not_from_the_session(self, chat):
        chat.authorise()
        chat.choose(UNDER_THE_THRESHOLD)
        chat.say("买吧")

        (status,) = chat.say("还有多少额度")
        assert status.spend_state is not None
        assert status.spend_state.exposure_cents == 28900
        assert status.spend_state.exposure_count == 1
        assert status.mandate.version == 1
        assert "HK$289.00" in status.message

    def test_a_second_purchase_is_checked_against_the_first(self, chat):
        """Exposure accumulates, so the second purchase is measured against it."""
        chat.authorise()
        chat.choose(UNDER_THE_THRESHOLD)
        chat.say("买吧")
        chat.choose(UNDER_THE_THRESHOLD)
        chat.say("买吧")

        (status,) = chat.say("还有多少额度")
        assert status.spend_state.exposure_cents == 57800
        assert status.spend_state.exposure_quantity == 2

    def test_the_transcript_records_the_intent_not_the_prose(self, orchestrator, chat):
        chat.authorise()
        session = orchestrator._sessions.get(chat.session_id)  # noqa: SLF001
        assert [t.intent for t in session.turns] == [
            Intent.CREATE_MANDATE, Intent.ACTIVATE_MANDATE,
        ]
        assert session.mandate_id is not None
        assert session.mandate_version == 1
        assert session.mandate_draft is not None, (
            "the draft the user signed stays on file as the record of what was agreed"
        )

    def test_the_session_holds_no_money(self, orchestrator, chat):
        """The rule the session's docstring states, asserted rather than trusted."""
        chat.authorise()
        chat.choose(UNDER_THE_THRESHOLD)
        chat.say("买吧")
        session = orchestrator._sessions.get(chat.session_id)  # noqa: SLF001

        forbidden = ("approved", "paid", "payment_success", "remaining_budget",
                     "policy_hash", "balance")
        assert not [name for name in vars(session) if name in forbidden], (
            "commerce truth comes from C; a cached copy is how a stale number "
            "reaches a user"
        )
        assert session.submissions == 1, "the idempotency counter is only a counter"


class TestTwoConversationsShareOneDatabase:
    """Regression: the idempotency key was built from a counter that restarts.

    Session ids are minted from a counter per ``SessionStore`` and the
    submission count starts at zero, so two runs of the application both hand
    their first conversation ``sess_0001`` and its first submission
    ``sess_0001-1``. Sharing one database -- which is the normal case, and the
    only case in a demo -- meant the second run's first purchase was refused with
    ``IDEMPOTENCY_CONFLICT``: a correct refusal of a mistake A made, and a demo
    that breaks on its second start.

    Two orchestrators over one database is what a restart looks like from C's
    side, so that is what this reproduces.
    """

    def test_a_second_run_can_still_buy(self, db_path):
        from app.commerce.service import CommerceService as Service

        first = Chat(AgentOrchestrator(
            sessions=SessionStore(), search=LocalSearchClient(),
            commerce=Service(db_path=db_path)))
        first.authorise()
        first.choose(UNDER_THE_THRESHOLD)
        assert first.say("买吧")[0].receipt is not None

        # A new process: a fresh session store, whose first session is again
        # sess_0001, against the same durable database.
        second = Chat(AgentOrchestrator(
            sessions=SessionStore(), search=LocalSearchClient(),
            commerce=Service(db_path=db_path)))
        second.authorise()
        second.choose(UNDER_THE_THRESHOLD)
        bought = second.say("买吧")[0]

        assert bought.receipt is not None, (
            "the second run's first purchase must not collide with the first "
            "run's idempotency key"
        )
        assert bought.receipt.cash_total_cents == 28900
        assert second.session_id == first.session_id, (
            "both runs mint the same session id, which is exactly the collision"
        )

    def test_the_keys_are_unique_even_when_the_counters_are_not(self, db_path):
        from app.commerce.service import CommerceService as Service

        keys = []
        for _ in range(3):
            chat = Chat(AgentOrchestrator(
                sessions=SessionStore(), search=LocalSearchClient(),
                commerce=Service(db_path=db_path)))
            chat.authorise()
            chat.choose(UNDER_THE_THRESHOLD)
            session = chat.orchestrator._sessions.get(chat.session_id)  # noqa: SLF001
            before = session.submissions
            chat.say("买吧")
            assert session.submissions == before + 1
            keys.append(session.session_id)
        assert len(set(keys)) == 1, "the premise: every run reuses sess_0001"


class TestFailureIsNotSilent:
    def test_buying_with_no_mandate_asks_instead_of_submitting(self, chat):
        chat.say(SEARCH, "第一款")
        (response,) = chat.say("买吧")
        assert response.receipt is None
        assert response.decision is None
        assert response.clarification is not None
        assert response.next_action is NextAction.CONFIRM_MANDATE

    def test_buying_with_nothing_chosen_asks_which_product(self, chat):
        """With a mandate but nothing on screen, the purchase asks for a product.

        The turn is recognised as a purchase rather than falling through to
        "what are you looking for?": there is an authorisation on file and the
        user said "buy", and the useful answer names the missing step.
        """
        chat.authorise()
        (response,) = chat.say("买吧")
        assert response.intent is Intent.RUN_DELEGATED_PURCHASE
        assert response.decision is None
        assert response.next_action is NextAction.ANSWER_QUESTION
        assert response.clarification is not None

    def test_buying_with_a_shortlist_asks_which_one(self, chat):
        chat.authorise()
        chat.say(SEARCH)
        (response,) = chat.say("买吧")
        assert response.next_action is NextAction.SELECT_PRODUCT
        assert response.clarification is not None
