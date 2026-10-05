"""The model-backed parser, tested without a network.

A model's output can only be sampled, so the parts worth pinning are the ones
that are *ours*: what happens to a reply that is wrong, and what the prompt tells
the model before it answers. Both are tested here against a fake client.
"""

from __future__ import annotations

import json

import pytest

from app.agent.intent_schema import build_llm_request, build_system_prompt
from app.agent.llm_parser import MAX_ATTEMPTS, LLMIntentParser
from app.contracts.agent import (
    IntentResult,
    LLMRequest,
    LLMResponse,
    ParseSource,
    SessionContext,
)
from app.contracts.product import HardConstraints
from app.errors import AgentError
from app.contracts.common import ErrorCode

RAW = "找 300 以内、通勤用、必须支持主动降噪的无线耳机"


def good_reply() -> str:
    return json.dumps({
        "intent": "SEARCH",
        "confidence": 0.9,
        "search": {"max_price_cents": 30000, "connection": "wireless",
                   "anc_required": True},
        "source_spans": {"max_price_cents": "300 以内",
                         "connection": "无线",
                         "anc_required": "必须支持主动降噪"},
    }, ensure_ascii=False)


class FakeClient:
    """Returns canned replies in order and records every request."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("the parser called the model more times than expected")
        return LLMResponse(text=self._replies.pop(0), model="fake")


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise self.error


def parser_with(*replies: str):
    client = FakeClient(*replies)
    return LLMIntentParser(client), client


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

class TestGoodReply:
    def test_a_valid_reply_is_returned(self):
        parser, client = parser_with(good_reply())
        result = parser.parse(RAW, SessionContext(principal_id="u"))
        assert isinstance(result, IntentResult)
        assert result.intent.value == "SEARCH"
        assert result.search.max_price_cents == 30000
        assert result.untraceable_fields() == []
        assert len(client.requests) == 1, "a valid reply must not be retried"

    def test_the_caller_owns_raw_text_and_source(self):
        parser, _ = parser_with(json.dumps({
            "intent": "SEARCH", "raw_text": "model wrote this",
            "parse_source": "REPAIRED",
            "search": {"max_price_cents": 100},
            "source_spans": {"max_price_cents": "100"},
        }))
        result = parser.parse(RAW, SessionContext(principal_id="u"))
        assert result.raw_text == RAW
        assert result.parse_source == ParseSource.LLM


# ---------------------------------------------------------------------------
# The repair attempt
# ---------------------------------------------------------------------------

class TestRepair:
    def test_a_bad_reply_is_retried_once_and_can_succeed(self):
        """The mechanism that makes a model usable at all.

        A first reply the contract rejects is resent with the violations named.
        """
        bad = json.dumps({"intent": "SEARCH"})  # SEARCH with no criteria
        parser, client = parser_with(bad, good_reply())

        result = parser.parse(RAW, SessionContext(principal_id="u"))

        assert len(client.requests) == 2
        assert result.parse_source == ParseSource.REPAIRED
        assert "second attempt" in (result.parser_note or "")

    def test_the_retry_names_the_rule_that_was_broken(self):
        """"invalid" is not actionable; the broken rule is."""
        parser, client = parser_with(json.dumps({"intent": "SEARCH"}), good_reply())
        parser.parse(RAW, SessionContext(principal_id="u"))

        repair_prompt = client.requests[1].system_prompt
        assert "previous reply was rejected" in repair_prompt
        assert "must carry search criteria" in repair_prompt

    def test_two_failures_give_up(self):
        bad = json.dumps({"intent": "SEARCH"})
        parser, client = parser_with(bad, bad)
        with pytest.raises(AgentError) as caught:
            parser.parse(RAW, SessionContext(principal_id="u"))
        assert caught.value.code == ErrorCode.LLM_PARSE_FAILED
        assert len(client.requests) == MAX_ATTEMPTS

    def test_the_failure_carries_every_problem_not_a_summary(self):
        parser, _ = parser_with(json.dumps({"intent": "SEARCH"}),
                                json.dumps({"intent": "SEARCH"}))
        with pytest.raises(AgentError) as caught:
            parser.parse(RAW, SessionContext(principal_id="u"))
        problems = caught.value.details["problems"]
        assert problems, "the point of reporting a parse failure is which rule broke"
        assert "reply_excerpt" in caught.value.details

    def test_an_invented_value_is_also_treated_as_a_failure(self):
        """A well-formed reply carrying an unquoted number is not acceptable."""
        invented = json.dumps({
            "intent": "SEARCH",
            "search": {"max_price_cents": 50000},
            "source_spans": {},
        })
        parser, client = parser_with(invented, good_reply())
        result = parser.parse("帮我买副好点的耳机", SessionContext(principal_id="u"))
        assert len(client.requests) == 2
        assert result.source_spans, "the repaired reply quotes the user"


# ---------------------------------------------------------------------------
# Transport failures are not parse failures
# ---------------------------------------------------------------------------

class TestTransport:
    def test_unreachable_is_unavailable_not_parse_failed(self):
        """Only a *wrong* reply is worth repairing.

        A network error retried as if it were a formatting mistake wastes the
        one attempt and reports the wrong reason.
        """
        from app.agent.llm_client import TRANSPORT_ERROR, LLMCallError

        client = FailingClient(LLMCallError(TRANSPORT_ERROR, "connection refused"))
        parser = LLMIntentParser(client)
        with pytest.raises(AgentError) as caught:
            parser.parse(RAW, SessionContext(principal_id="u"))
        assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
        assert caught.value.retryable is True
        assert client.calls == 1, "a transport failure must not be repaired"


# ---------------------------------------------------------------------------
# What the prompt tells the model
# ---------------------------------------------------------------------------

class TestPromptContract:
    def test_the_target_rule_is_stated(self):
        """A model filled in both rank and product_id before this existed.

        Filling in the id it worked out is helpful and rejected: a reference
        must carry exactly one handle.
        """
        prompt = build_system_prompt()
        assert "exactly ONE handle" in prompt
        assert "rank" in prompt and "product_id" in prompt

    def test_underspecified_is_distinguished_from_missing(self):
        """Otherwise the same sentence behaves differently per parser.

        MISSING blocks and makes the agent ask; UNDERSPECIFIED lets the caller
        derive a value from the results already on screen.
        """
        prompt = build_system_prompt()
        assert "UNDERSPECIFIED, not MISSING" in prompt

    def test_the_prompt_renders_with_braces_in_it(self):
        """Regression: a JSON example in the template broke str.format()."""
        assert '"kind"' in build_system_prompt()
        assert "{" in build_system_prompt()


class TestSessionContextReachesTheModel:
    """A second turn is unparseable without it -- "cheaper" has no referent."""

    def test_current_filters_are_included(self):
        context = SessionContext(
            principal_id="u",
            current_constraints=HardConstraints(max_price_cents=30000,
                                                connection="wireless"),
        )
        prompt = build_system_prompt(context)
        assert "max_price_cents=30000" in prompt
        assert "connection='wireless'" in prompt

    def test_shown_products_are_listed_in_order(self):
        context = SessionContext(
            principal_id="u",
            last_shown_product_ids=["hp_0018", "hp_0001", "hp_0008"],
        )
        prompt = build_system_prompt(context)
        assert "1. hp_0018" in prompt
        assert "2. hp_0001" in prompt
        assert "ordinals resolve to them" in prompt

    def test_no_money_is_ever_disclosed(self):
        """The parser resolves references; it never needs a balance.

        A model that is told a budget will eventually reason about it.
        """
        context = SessionContext(
            principal_id="u",
            current_constraints=HardConstraints(max_price_cents=30000),
        )
        prompt = build_system_prompt(context).lower()
        for forbidden in ("balance", "wallet", "remaining_budget", "spent"):
            assert forbidden not in prompt

    def test_an_empty_session_says_what_is_missing(self):
        """A session with nothing in it is described, not omitted.

        "none yet" is better than a silence the model has to interpret: silence
        reads as "no information", while "no products have been shown" tells it
        that an ordinal in this turn cannot be resolved.
        """
        prompt = build_system_prompt(SessionContext(principal_id="u"))
        assert "current filters: none" in prompt
        assert "no products have been shown yet" in prompt

    def test_a_missing_context_says_it_is_the_first_turn(self):
        assert "first turn" in build_system_prompt(None)

    def test_the_request_carries_the_context(self):
        context = SessionContext(principal_id="u", last_shown_product_ids=["hp_0018"])
        request = build_llm_request(RAW, context=context)
        assert "hp_0018" in request.system_prompt
        assert request.user_text == RAW
