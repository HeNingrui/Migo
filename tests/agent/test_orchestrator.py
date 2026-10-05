"""The turn loop, tested offline with a fake model.

The orchestrator is where a multi-turn conversation either works or does not:
the parser sees one sentence, and only this layer knows what was said before.
Most of what follows is therefore about the *second* turn.

The fake model lets a test choose exactly what the parser returned -- including
returning something unhelpful -- so the orchestrator's own behaviour is pinned
independently of any model's mood.
"""

from __future__ import annotations

import json

import pytest

from app.agent.local_search import LocalSearchClient
from app.agent.orchestrator import AgentOrchestrator
from app.agent.session import SessionStore
from app.contracts.agent import (
    ChatRequest,
    LLMRequest,
    LLMResponse,
    SessionContext,
)
from app.contracts.common import NextAction

DEMO = "找 300 以内、通勤用、必须支持主动降噪的无线耳机"


class SilentModel:
    """A model that always answers UNKNOWN, like a cautious one does.

    Used to prove the orchestrator stays useful when the model contributes
    nothing, which is the whole reason the deterministic parser exists.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.user_text)
        return LLMResponse(text=json.dumps({"intent": "UNKNOWN"}), model="fake")


class ScriptedModel:
    """Returns a prepared reply per user message, so a test can force a path."""

    def __init__(self, replies: dict[str, dict | str]) -> None:
        self.replies = replies

    def complete(self, request: LLMRequest) -> LLMResponse:
        reply = self.replies.get(request.user_text)
        if reply is None:
            return LLMResponse(text=json.dumps({"intent": "UNKNOWN"}), model="fake")
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return LLMResponse(text=text, model="fake")


def build(*, model=None, commerce=None):
    from app.agent.llm_parser import LLMIntentParser

    return AgentOrchestrator(
        sessions=SessionStore(),
        search=LocalSearchClient(),
        commerce=commerce,
        llm_parser=LLMIntentParser(model) if model is not None else None,
    )


def talk(orchestrator, *turns):
    """Send turns in one session and return every response."""
    session_id = None
    responses = []
    for text in turns:
        response = orchestrator.handle(ChatRequest(session_id=session_id, message=text))
        session_id = response.session_id
        responses.append(response)
    return responses


# ---------------------------------------------------------------------------
# Three ways a vague-looking turn is handled, and why they differ
# ---------------------------------------------------------------------------

class TestColdStartShowsBeforeAsking:
    """A want with no requirements gets a starting point, not a question.

    "我想买个耳机" gives nothing to filter on. Replying "what are you looking
    for?" asks the user to compose a specification from nothing -- the least
    useful answer available, and the one the agent used to give.
    """

    def test_it_shows_products_and_then_asks(self):
        orchestrator = build()
        (response,) = talk(orchestrator, "我想买个耳机")

        assert response.results is not None, "show something to react to"
        assert response.results.candidates, "a cold start must list something"
        assert response.next_action == NextAction.ANSWER_QUESTION
        assert response.clarification is not None
        assert "款式" in response.message or "预算" in response.message

    def test_it_says_which_order_it_used(self):
        """With no stated preference there is no best match, only a default.

        Saying which one keeps an arbitrary order from looking like a
        recommendation.
        """
        orchestrator = build()
        (response,) = talk(orchestrator, "我想买个耳机")
        assert "价格从低到高" in response.message

    def test_what_it_showed_can_be_referenced_next(self):
        """Showing products must register them, or "第一款" means nothing."""
        orchestrator = build()
        first, second = talk(orchestrator, "我想买个耳机", "第一款")
        assert second.selected_product_id == first.results.candidates[0].product_id

    @pytest.mark.parametrize("text", ["你好", "嗯", "hello"])
    def test_a_non_committal_turn_still_gets_a_question(self, text):
        """Not every unrecognised turn is a shopping request.

        Only a stated want earns a product list; a greeting is answered with a
        question, which is what the user asked for by saying nothing.
        """
        orchestrator = build()
        (response,) = talk(orchestrator, text)
        assert response.results is None
        assert response.next_action == NextAction.ANSWER_QUESTION


class TestAQuestionIsAnsweredNotRepeated:
    """A question with a subject is answered from what the agent already has.

    "有别的颜色吗" after a search is askable in one line: the products are on
    screen and the criteria are known. Replying "what are you looking for?" is
    the same defect as re-asking for a budget.
    """

    def test_the_answer_points_at_variants_already_on_screen(self):
        """The answer is often already visible.

        Asked "are there other colours?" while both colours of one model are on
        screen, the useful reply is to point at them. Hunting only for variants
        *not yet shown* produced "there are no other versions" -- true of the
        unseen ones and useless as an answer.
        """
        orchestrator = build()
        first, second = talk(orchestrator, DEMO, "有别的颜色吗")
        models = {}
        for candidate in first.results.candidates:
            models.setdefault(candidate.product.model, []).append(candidate)
        varied = {m: cs for m, cs in models.items() if len(cs) > 1}
        assert varied, "the fixture must contain two variants of one model"
        assert "你现在看到的就有" in second.message
        for model in varied:
            assert model in second.message

    def test_a_variant_question_is_answered_with_the_siblings(self):
        orchestrator = build()
        # hp_0001 and hp_0008 are the same model in different variants.
        first, second = talk(orchestrator, DEMO, "有别的颜色吗")
        assert second.intent.value == "ASK_ALTERNATIVES"
        assert second.results is not None
        assert "型号" in second.message or "别的" in second.message

    def test_the_answer_is_grounded_in_real_products(self):
        orchestrator = build()
        _, second = talk(orchestrator, DEMO, "有别的颜色吗")
        assert second.results is not None
        assert second.results.candidates

    def test_the_new_list_replaces_what_ranks_resolve_against(self):
        """After answering, "second one" must mean the second one now on screen."""
        orchestrator = build()
        _, _, third = talk(orchestrator, DEMO, "有别的颜色吗", "第二款")
        assert third.selected_product_id is not None

    @pytest.mark.parametrize(
        "text", ["有别的颜色吗", "有其他款式吗", "还有别的吗", "any other colours?"],
    )
    def test_it_is_recognised_as_a_question_not_a_refinement(self, text):
        from app.agent.fallback_parser import FallbackIntentParser
        from app.contracts.agent import Intent

        result = FallbackIntentParser().parse(text, SessionContext(principal_id="u"))
        assert result.intent == Intent.ASK_ALTERNATIVES

    def test_asking_before_anything_was_shown_still_asks_back(self):
        """With no subject the question genuinely cannot be answered."""
        orchestrator = build()
        (response,) = talk(orchestrator, "有别的颜色吗")
        assert response.next_action == NextAction.ANSWER_QUESTION
        assert response.clarification is not None


# ---------------------------------------------------------------------------
# The reported defect: asking for something already on file
# ---------------------------------------------------------------------------

class TestNeverAsksWhatItAlreadyKnows:
    """A vague turn after criteria exist must not restart the conversation.

    Reported from a live run: with "≤HK$200, wireless, ANC" on file, "随便看看"
    was answered with "what are you looking for, and roughly what price?" --
    asking for a budget the user had given two turns earlier. Users read that as
    the agent not listening, and they are right.
    """

    def test_a_vague_turn_offers_to_continue_instead_of_re_asking(self):
        orchestrator = build()
        first, second = talk(orchestrator, "找 200 以内的无线耳机，必须支持主动降噪", "随便看看")

        assert second.intent.value == "UNKNOWN"
        assert "HK$200.00" in second.message, "the known budget must be shown back"
        assert "再看一遍" in second.message or "改点什么" in second.message
        assert "大概什么价位" not in second.message, "it already knows the price"

    def test_a_vague_turn_actually_re_searches_with_the_known_criteria(self):
        """Continuing is better than asking -- the criteria are right there.

        "随便看看" means "show me", and something is already on file, so the
        useful answer is the results rather than a question.
        """
        model = ScriptedModel({
            DEMO: {
                "intent": "SEARCH",
                "search": {"max_price_cents": 30000, "connection": "wireless",
                           "anc_required": True},
                "source_spans": {"max_price_cents": "300 以内",
                                 "connection": "无线",
                                 "anc_required": "必须支持主动降噪"},
            },
            "随便看看": {"intent": "UPDATE_SEARCH", "search": {}, "source_spans": {}},
        })
        first, second = talk(build(model=model), DEMO, "随便看看")
        assert first.results is not None
        assert second.results is not None, "an empty refinement with criteria re-searches"
        assert second.results.total_matches > 0
        assert any("再看一遍" in n for n in second.notes)

    def test_an_empty_refinement_without_criteria_still_asks(self):
        """The same turn with nothing on file is genuinely unanswerable."""
        orchestrator = build()
        (response,) = talk(orchestrator, "随便看看")
        assert response.next_action == NextAction.ANSWER_QUESTION
        assert response.clarification is not None

    def test_a_known_field_is_not_asked_for_again(self):
        orchestrator = build()
        first, second = talk(orchestrator, "300 以内的无线耳机", "嗯")
        assert second.message.count("预算") == 0 or "HK$300.00" in second.message


# ---------------------------------------------------------------------------
# The model contributes nothing
# ---------------------------------------------------------------------------

class TestTheFallbackCarriesTheConversation:
    """Every turn goes to the model first; the local rules catch the failures.

    The trace is what makes that visible. A fallback that took over silently
    would make a model outage indistinguishable from a model that agreed.
    """

    def test_the_loop_still_works_when_the_model_says_nothing(self):
        orchestrator = build(model=SilentModel())
        first, second = talk(orchestrator, DEMO, "第二款")

        assert first.results is not None and first.results.total_matches == 4
        assert second.intent.value == "SELECT_PRODUCT"
        assert second.selected_product_id == first.results.candidates[1].product_id

    def test_the_fallback_is_recorded_in_the_trace(self):
        orchestrator = build(model=SilentModel())
        (response,) = talk(orchestrator, DEMO)
        sources = [attempt.source.value for attempt in response.trace]
        assert "LLM" in sources and "FALLBACK" in sources
        assert any(not a.ok for a in response.trace), "the failure must be visible"

    def test_the_reply_says_the_local_rules_were_used(self):
        orchestrator = build(model=SilentModel())
        (response,) = talk(orchestrator, DEMO)
        assert any("本地规则" in note for note in response.notes)


# ---------------------------------------------------------------------------
# References and refinement
# ---------------------------------------------------------------------------

class TestReferences:
    def test_the_second_one_resolves_to_what_was_shown(self):
        """The chosen id is carried, not a position: the next search replaces
        the list, and a control bound to "item 2" would then buy something else.

        With no commerce client there is nothing to price the choice with, so no
        purchase control is offered -- a "confirm payment" button that leads
        nowhere is worse than no button. The wired behaviour, including the
        price and the control, is asserted in ``test_commerce_turns.py``.
        """
        orchestrator = build()
        first, second = talk(orchestrator, DEMO, "第二款")
        shown = [c.product_id for c in first.results.candidates]
        assert second.selected_product_id == shown[1]
        assert second.next_action == NextAction.NONE
        assert second.quote is None, "nothing may be priced without a commerce layer"
        assert any("no commerce client" in note for note in second.notes)

    def test_a_rank_beyond_what_was_shown_is_refused_not_guessed(self):
        orchestrator = build()
        _, second = talk(orchestrator, DEMO, "第九款")
        assert second.next_action == NextAction.ANSWER_QUESTION
        assert second.clarification is not None

    def test_ranks_resolve_against_the_last_showing_not_a_new_search(self):
        """What the user saw is what "the second one" means."""
        orchestrator = build()
        (first,) = talk(orchestrator, DEMO)
        session = orchestrator._sessions.get(first.session_id)
        assert session.last_shown_product_ids == [
            c.product_id for c in first.results.candidates
        ]


class TestRefinement:
    def test_cheaper_narrows_and_says_which_number_it_chose(self):
        orchestrator = build()
        first, second = talk(orchestrator, DEMO, "便宜点")
        assert second.constraints.max_price_cents is not None
        assert second.constraints.max_price_cents < 30000
        # The derived number must be stated: a budget the user never saw is a
        # budget the user never agreed to.
        assert "HK$" in second.message

    def test_a_stated_number_replaces_the_derived_one(self):
        orchestrator = build()
        _, _, third = talk(orchestrator, DEMO, "便宜点", "250 以内")
        assert third.constraints.max_price_cents == 25000


# ---------------------------------------------------------------------------
# Nothing matched
# ---------------------------------------------------------------------------

class TestEmptyResults:
    def test_it_reports_what_relaxing_would_buy(self):
        """The contract refuses an empty result with no explanation, and the
        user needs one: which of their own conditions was impossible."""
        orchestrator = build()
        (response,) = talk(orchestrator, "300 以内的无线降噪耳机，重量不超过 5 克")
        assert response.results is not None
        assert response.results.total_matches == 0, "5 g must exclude everything"
        assert response.results.relax_hints is not None
        assert response.results.relax_hints.hints, "say what relaxing would buy"
        assert all(h.gained_count > 0 for h in response.results.relax_hints.hints)
        assert "放宽" in response.message

    def test_the_relaxation_options_are_offered_as_a_question(self):
        orchestrator = build()
        (response,) = talk(orchestrator, "50 以内的无线降噪耳机")
        assert response.results.total_matches == 0
        assert response.next_action == NextAction.ANSWER_QUESTION
        assert response.clarification is not None

    def test_a_near_miss_says_which_constraints_it_breaks(self):
        """Without this A cannot tell the user what is missing."""
        orchestrator = build()
        (response,) = talk(orchestrator, "50 以内的无线降噪耳机")
        closest = response.results.relax_hints.closest_candidates
        assert closest, "a near miss is more useful than a bare 'nothing found'"
        assert all(c.violated_fields for c in closest)


# ---------------------------------------------------------------------------
# The money boundary
# ---------------------------------------------------------------------------

class TestMoneyIntentsAreNotFaked:
    """With no commerce client, the honest answer is that the step is not built.

    The orchestrator is constructible without C on purpose -- a search-only
    process is a legitimate configuration -- so what is asserted here is that
    the absence is *stated* rather than papered over. ``commerce=None`` is the
    configuration under test; the wired paths are covered in
    ``tests/agent/test_commerce_turns.py`` against the real commerce service.
    """

    @pytest.mark.parametrize("text", ["确认付款", "激活这个授权", "撤销授权"])
    def test_a_paying_intent_says_it_is_not_connected(self, text):
        orchestrator = build()
        (response,) = talk(orchestrator, text)
        # Nothing may claim a payment happened or a decision was taken.
        assert response.receipt is None
        assert response.decision is None
        assert response.next_action != NextAction.CONFIRM_PAYMENT

    def test_choosing_before_anything_was_shown_asks_rather_than_guessing(self):
        """A rank with nothing to rank against is a question, not a purchase."""
        orchestrator = build()
        (response,) = talk(orchestrator, "帮我买第二款")
        assert response.selected_product_id is None
        assert response.next_action == NextAction.ANSWER_QUESTION
        assert response.clarification is not None, "it asks, so it must say it asks"


class TestSession:
    def test_a_turn_without_a_session_id_starts_one(self):
        orchestrator = build()
        (response,) = talk(orchestrator, DEMO)
        assert response.session_id.startswith("sess_")

    def test_an_unknown_session_id_is_an_error_not_a_new_session(self):
        """Silently starting over would drop the user's criteria and look like
        the agent forgot what it was told."""
        from app.errors import AgentError

        orchestrator = build()
        with pytest.raises(AgentError):
            orchestrator.handle(ChatRequest(session_id="sess_nope", message=DEMO))

    def test_criteria_survive_across_turns(self):
        orchestrator = build()
        first, second = talk(orchestrator, DEMO, "便宜点")
        assert first.constraints is not None and second.constraints is not None
        assert second.constraints.max_price_cents <= first.constraints.max_price_cents
