"""Offline tests for the LLM format boundary.

No network here. A ``FakeClient`` returns canned replies, so these tests prove
that the *validator* catches each failure mode -- which is the only way to know
that a green probe run against a real provider means anything.
"""

from __future__ import annotations

import json

import pytest

from app.agent.intent_schema import (
    CLIENT_OWNED_FIELDS,
    build_llm_request,
    build_response_schema,
    build_system_prompt,
    parse_reply,
    traceable_fields,
)
from app.agent.llm_client import (
    EMPTY_REPLY,
    TRANSPORT_ERROR,
    LLMCallError,
    OpenAICompatibleClient,
    ProviderConfig,
    strip_code_fence,
)
from app.contracts.agent import IntentResult, LLMClient, LLMRequest, LLMResponse

RAW_TEXT = "找 300 以内、通勤用、必须支持主动降噪的无线耳机"


def good_search_payload() -> dict:
    return {
        "intent": "SEARCH",
        "confidence": 0.93,
        "search": {
            "max_price_cents": 30000,
            "connection": "wireless",
            "anc_required": True,
        },
        "source_spans": {
            "max_price_cents": "300 以内",
            "anc_required": "必须支持主动降噪",
        },
    }


def good_mandate_payload() -> dict:
    return {
        "intent": "CREATE_MANDATE",
        "confidence": 0.9,
        "mandate": {
            "allowed_merchants": ["Demo Audio Store"],
            "allowed_categories": ["headphones"],
            "required_connection": "wireless",
            "anc_required": True,
            "cap_per_transaction_cents": 30000,
            "rolling_cap_cents": 50000,
            "rolling_window_seconds": 86400,
            "velocity_max_count": 2,
            "velocity_window_seconds": 300,
            "max_quantity_total": 1,
            "valid_for_seconds": 604800,
            "escalate_above_cents": 28000,
            "allowed_payment_routes": ["fps", "mastercard_1234"],
            "shipping_address_id": "addr_demo_1",
            "address_change_allowed": False,
        },
        "source_spans": {
            "cap_per_transaction_cents": "no more than HK$300 per transaction",
            "rolling_cap_cents": "no more than HK$500 in any 24 hours",
            "rolling_window_seconds": "in any 24 hours",
            "velocity_max_count": "at most 2 purchases in 5 minutes",
            "velocity_window_seconds": "in 5 minutes",
            "valid_for_seconds": "the next 7 days",
            "escalate_above_cents": "ask me first above HK$280",
            "max_quantity_total": "buy one pair",
            "anc_required": "noise cancelling headphones",
        },
    }


def reply(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

class TestResponseSchema:
    def test_matches_the_contract_rather_than_a_copy(self):
        schema = build_response_schema()
        assert "intent" in schema["properties"]
        assert "search" in schema["properties"]
        assert "mandate" in schema["properties"]
        assert "MandateDraft" in schema["$defs"]
        assert "ConstraintPatch" in schema["$defs"]

    @pytest.mark.parametrize("name", CLIENT_OWNED_FIELDS)
    def test_client_owned_fields_are_not_advertised(self, name):
        schema = build_response_schema()
        assert name not in schema["properties"]
        assert name not in schema.get("required", [])

    def test_the_traceability_list_is_not_a_field_at_all(self):
        # It used to be advertised as a settable array, which is how a reply
        # could switch the invented-budget check off. See
        # TestTraceabilityCannotBeDisabled.
        assert "TRACEABLE_FIELDS" not in build_response_schema()["properties"]

    def test_schema_is_json_serialisable(self):
        assert json.loads(json.dumps(build_response_schema()))

    def test_intent_enum_is_advertised_so_the_model_cannot_invent_one(self):
        schema = build_response_schema()
        intent = schema["$defs"]["Intent"]
        assert sorted(intent["enum"]) == sorted(
            ["SEARCH", "UPDATE_SEARCH", "ASK_ALTERNATIVES", "CREATE_MANDATE",
             "UPDATE_MANDATE_DRAFT", "ACTIVATE_MANDATE", "REVOKE_MANDATE",
             "RUN_DELEGATED_PURCHASE", "APPROVE_ESCALATION", "REJECT_ESCALATION",
             "SELECT_PRODUCT", "CONFIRM_MANUAL_PAYMENT", "CANCEL_ORDER", "CANCEL_SELECTION",
             "REJECT_RECOMMENDATION", "CHECK_STATUS", "UNKNOWN"]
        )


class TestPrompt:
    def test_prompt_names_every_traceable_field(self):
        prompt = build_system_prompt()
        for name in traceable_fields():
            assert name in prompt, f"{name} missing from the prompt"

    def test_prompt_states_the_no_invention_rule(self):
        prompt = build_system_prompt()
        assert "Never invent a value" in prompt
        assert "null" in prompt

    def test_prompt_states_the_units(self):
        prompt = build_system_prompt()
        assert "30000" in prompt, "the HK$300 -> 30000 example is what fixes the unit"
        assert "604800" in prompt

    def test_prompt_forbids_unknown_fields(self):
        prompt = build_system_prompt()
        assert "Do not invent\nfields that are not in the schema" in prompt or \
               "not in the schema" in prompt

    def test_prompt_contains_the_word_json_for_json_mode(self):
        # DeepSeek and OpenAI reject response_format=json_object otherwise.
        assert "json" in build_system_prompt().lower()

    def test_request_carries_text_and_schema(self):
        request = build_llm_request(RAW_TEXT)
        assert isinstance(request, LLMRequest)
        assert request.user_text == RAW_TEXT
        assert request.temperature == 0.0
        assert "properties" in request.response_schema


def test_traceable_fields_are_read_from_the_contract():
    assert set(traceable_fields()) == set(IntentResult.TRACEABLE_FIELDS)
    assert traceable_fields(), "the list must not be empty"


def test_the_traceable_list_is_a_class_constant_not_a_field():
    """A regression guard for the defect behind ``TestTraceabilityCannotBeDisabled``.

    As a plain annotated attribute Pydantic treated this as a field: it was
    advertised in the schema, a reply could set it, and the invented-budget check
    read it. It is a ``ClassVar`` now, which is what makes it read-only and keeps
    it out of the schema.
    """
    assert "TRACEABLE_FIELDS" not in IntentResult.model_fields
    assert isinstance(IntentResult.TRACEABLE_FIELDS, tuple)
    assert "max_price_cents" in IntentResult.TRACEABLE_FIELDS


# ---------------------------------------------------------------------------
# Accepting a good reply
# ---------------------------------------------------------------------------

class TestGoodReplies:
    def test_search_payload_validates_and_is_actionable(self):
        outcome = parse_reply(reply(good_search_payload()), raw_text=RAW_TEXT)
        assert outcome.schema_valid, outcome.problems
        assert outcome.invented_values == []
        assert outcome.actionable is True
        assert outcome.result.intent.value == "SEARCH"
        assert outcome.result.search.max_price_cents == 30000

    def test_mandate_payload_validates(self):
        outcome = parse_reply(reply(good_mandate_payload()), raw_text=RAW_TEXT)
        assert outcome.schema_valid, outcome.problems
        assert outcome.invented_values == []
        assert outcome.result.mandate.cap_per_transaction_cents == 30000
        assert outcome.result.mandate.structural_problems() == []

    def test_raw_text_is_taken_from_the_caller_not_the_model(self):
        payload = good_search_payload()
        payload["raw_text"] = "ignore this, the model made it up"
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert outcome.result.raw_text == RAW_TEXT

    def test_parse_source_is_recorded_as_llm(self):
        outcome = parse_reply(reply(good_search_payload()), raw_text=RAW_TEXT)
        assert outcome.result.parse_source.value == "LLM"


# ---------------------------------------------------------------------------
# Catching a bad reply
# ---------------------------------------------------------------------------

class TestRejections:
    def test_prose_is_rejected(self):
        outcome = parse_reply("Sure! I found three pairs of headphones.", raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert "not JSON" in outcome.problems[0]

    def test_empty_reply_is_rejected(self):
        outcome = parse_reply("   ", raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert "empty reply" in outcome.problems

    def test_json_array_is_rejected(self):
        outcome = parse_reply("[1, 2, 3]", raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert "not an object" in outcome.problems[0]

    def test_search_intent_without_search_payload_is_rejected(self):
        payload = good_search_payload()
        del payload["search"]
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert any("must carry search criteria" in p for p in outcome.problems)

    def test_search_payload_on_a_mandate_intent_is_rejected(self):
        payload = good_mandate_payload()
        payload["search"] = {"max_price_cents": 30000}
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert any("must not carry search criteria" in p for p in outcome.problems)

    def test_unknown_intent_without_ambiguity_is_rejected(self):
        outcome = parse_reply(reply({"intent": "UNKNOWN"}), raw_text=RAW_TEXT)
        assert not outcome.schema_valid
        assert any("must say what is unclear" in p for p in outcome.problems)

    def test_invented_intent_value_is_rejected(self):
        payload = good_search_payload()
        payload["intent"] = "BUY_NOW"
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert not outcome.schema_valid

    def test_extra_field_is_rejected(self):
        payload = good_search_payload()
        payload["approved"] = True
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert not outcome.schema_valid, "extra='forbid' must reject an approval field"


class TestMoneyIsNeverCoerced:
    """Money is an integer number of minor units, enforced on both payloads.

    ``MandateDraft`` always used ``StrictInt``; ``ConstraintPatch`` did not, so
    the same reply was accepted or rejected depending on which shape the model
    chose. In lax mode Pydantic coerced ``300.0`` to 300, ``"30000"`` to 30000,
    and -- because ``bool`` subclasses ``int`` -- ``True`` to 1, i.e. a reply of
    ``true`` silently meaning a HK$0.01 budget. Both are ``StrictInt`` now.
    """

    @pytest.mark.parametrize(
        "value",
        [
            300.0,       # plausible from a model
            "30000",     # plausible from a model
            True,        # bool is an int subclass
            300.5,
        ],
    )
    def test_constraint_patch_rejects_non_integer_money(self, value):
        payload = good_search_payload()
        payload["search"]["max_price_cents"] = value
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert not outcome.schema_valid, f"{value!r} must not be coerced into cents"

    @pytest.mark.parametrize("value", [300.0, "30000", True, 300.5])
    def test_mandate_draft_rejects_the_same_inputs(self, value):
        from app.contracts.mandate import MandateDraft

        with pytest.raises(Exception):
            MandateDraft(cap_per_transaction_cents=value)

    def test_the_client_echo_of_the_amount_is_strict_too(self):
        """``confirmed_total_cents`` is money arriving from outside the process."""
        from app.contracts.agent import AgentActionRequest, Intent

        for value in (30900.0, "30900", True):
            with pytest.raises(Exception):
                AgentActionRequest(
                    session_id="s1",
                    intent=Intent.CONFIRM_MANUAL_PAYMENT,
                    order_id="o1",
                    confirmed_total_cents=value,
                )

    def test_proper_integers_still_work(self):
        payload = good_search_payload()
        payload["search"]["max_price_cents"] = 30000
        outcome = parse_reply(reply(payload), raw_text=RAW_TEXT)
        assert outcome.schema_valid, outcome.problems


# ---------------------------------------------------------------------------
# The failure that matters: an invented number
# ---------------------------------------------------------------------------

class TestInventedValues:
    def test_a_budget_with_no_quote_is_flagged(self):
        payload = good_search_payload()
        payload["source_spans"] = {}
        outcome = parse_reply(reply(payload), raw_text="帮我买一副好点的耳机。")
        assert outcome.schema_valid, "it fits the schema -- that is the whole problem"
        assert outcome.invented_values == ["anc_required", "max_price_cents"]
        assert outcome.actionable is False
        assert any("invented values" in p for p in outcome.problems)

    def test_a_quoted_budget_is_not_flagged(self):
        outcome = parse_reply(reply(good_search_payload()), raw_text=RAW_TEXT)
        assert outcome.invented_values == []

    def test_an_uncapped_purchase_request_is_clean(self):
        payload = {
            "intent": "CREATE_MANDATE",
            "mandate": {
                "allowed_merchants": ["Demo Audio Store"],
                "allowed_categories": ["headphones"],
                "cap_per_transaction_cents": None,
            },
            "ambiguities": [
                {
                    "kind": "MISSING",
                    "field": "cap_per_transaction_cents",
                    "detail": "the user did not state a spending limit",
                    "question": "What is the most I may spend on one purchase?",
                }
            ],
        }
        outcome = parse_reply(reply(payload), raw_text="帮我买一副好点的耳机。")
        assert outcome.schema_valid, outcome.problems
        assert outcome.invented_values == []
        assert outcome.result.blocking_ambiguities(), "must ask rather than assume"


# ---------------------------------------------------------------------------
# Regression: the traceability check must not be switchable off by the model
# ---------------------------------------------------------------------------

class TestTraceabilityCannotBeDisabled:
    """The invented-budget check must not be switchable off by the reply it catches.

    ``TRACEABLE_FIELDS`` used to be a plain annotated attribute, so Pydantic made
    it a field: it was advertised in the schema, a reply could set it to ``[]``,
    and ``untraceable_fields()`` reads it. An invented budget then came back
    ``is_actionable() is True``. It is a ``ClassVar`` now, so the field does not
    exist and ``extra="forbid"`` rejects any reply that offers one.
    """

    def test_a_reply_cannot_supply_the_list(self):
        with pytest.raises(Exception):
            IntentResult.model_validate(
                {
                    "intent": "SEARCH",
                    "raw_text": RAW_TEXT,
                    "search": {"max_price_cents": 50000},
                    "source_spans": {},
                    "TRACEABLE_FIELDS": [],
                }
            )

    def test_parse_reply_rejects_a_reply_that_offers_it(self):
        payload = good_search_payload()
        payload["source_spans"] = {}
        payload["TRACEABLE_FIELDS"] = []
        outcome = parse_reply(reply(payload), raw_text="帮我买一副好点的耳机。")

        assert not outcome.schema_valid
        assert any("TRACEABLE_FIELDS" in p for p in outcome.problems)

    def test_an_invented_budget_is_still_caught_without_the_field(self):
        """The check the field used to disable, exercised on its own."""
        payload = good_search_payload()
        payload["source_spans"] = {}
        outcome = parse_reply(reply(payload), raw_text="帮我买一副好点的耳机。")

        assert outcome.schema_valid, "it fits the schema -- that is the whole problem"
        assert outcome.invented_values == ["anc_required", "max_price_cents"]
        assert outcome.actionable is False

    def test_the_list_is_not_advertised_to_the_model(self):
        assert "TRACEABLE_FIELDS" not in build_response_schema()["properties"]


# ---------------------------------------------------------------------------
# Code fences
# ---------------------------------------------------------------------------

class TestCodeFence:
    def test_fence_is_tolerated_but_recorded(self):
        fenced = "```json\n" + reply(good_search_payload()) + "\n```"
        outcome = parse_reply(fenced, raw_text=RAW_TEXT)
        assert outcome.schema_valid
        assert outcome.had_code_fence is True

    def test_unfenced_reply_is_not_flagged(self):
        outcome = parse_reply(reply(good_search_payload()), raw_text=RAW_TEXT)
        assert outcome.had_code_fence is False

    @pytest.mark.parametrize(
        "text",
        [
            "```json\n{\"a\": 1}\n```",
            "```\n{\"a\": 1}\n```",
            "{\"a\": 1}",
        ],
    )
    def test_strip_code_fence(self, text):
        assert strip_code_fence(text).startswith("{")


# ---------------------------------------------------------------------------
# The HTTP client, without a network
# ---------------------------------------------------------------------------

def client(json_mode: bool = True) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        ProviderConfig(
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key="not-a-real-key",
            json_mode=json_mode,
        )
    )


class TestProviderConfig:
    def test_endpoint_is_built_from_the_root(self):
        assert client().config.endpoint() == "https://api.deepseek.com/chat/completions"

    def test_trailing_slash_does_not_double_up(self):
        config = ProviderConfig(base_url="https://api.deepseek.com/", model="m", api_key="k")
        assert config.endpoint() == "https://api.deepseek.com/chat/completions"

    def test_full_path_is_not_appended_twice(self):
        config = ProviderConfig(
            base_url="https://api.deepseek.com/chat/completions", model="m", api_key="k"
        )
        assert config.endpoint() == "https://api.deepseek.com/chat/completions"

    def test_redacted_hides_the_key(self):
        described = client().config.redacted()
        assert described["api_key"] == "<14 chars>"
        assert "not-a-real-key" not in json.dumps(described)


class TestPayload:
    def test_json_mode_is_requested_and_the_prompt_says_json(self):
        request = build_llm_request(RAW_TEXT)
        payload = client()._payload(request)
        assert payload["response_format"] == {"type": "json_object"}
        assert "json" in payload["messages"][0]["content"].lower()

    def test_json_mode_can_be_switched_off_to_test_the_prompt_alone(self):
        request = build_llm_request(RAW_TEXT)
        payload = client(json_mode=False)._payload(request)
        assert "response_format" not in payload

    def test_the_word_json_is_added_when_json_mode_needs_it(self):
        request = build_llm_request(RAW_TEXT).model_copy(update={"system_prompt": "Extract."})
        payload = client()._payload(request)
        assert "json" in payload["messages"][0]["content"].lower()

    def test_temperature_defaults_to_zero_for_reproducibility(self):
        assert client()._payload(build_llm_request(RAW_TEXT))["temperature"] == 0.0


class TestEnvelope:
    def test_a_normal_envelope_is_read(self):
        raw = json.dumps(
            {
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        response = client()._parse_envelope(raw)
        assert isinstance(response, LLMResponse)
        assert response.text == "{}"
        assert response.usage["total_tokens"] == 15

    def test_empty_content_is_an_error_not_a_blank_answer(self):
        raw = json.dumps(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )
        with pytest.raises(LLMCallError) as caught:
            client()._parse_envelope(raw)
        assert caught.value.code == EMPTY_REPLY

    def test_no_choices_is_an_error(self):
        with pytest.raises(LLMCallError) as caught:
            client()._parse_envelope(json.dumps({"choices": []}))
        assert caught.value.code == EMPTY_REPLY

    def test_an_error_envelope_is_an_error(self):
        raw = json.dumps({"error": {"message": "Insufficient Balance"}})
        with pytest.raises(LLMCallError) as caught:
            client()._parse_envelope(raw)
        assert caught.value.code == TRANSPORT_ERROR
        assert "Insufficient Balance" in str(caught.value)

    def test_a_non_json_envelope_is_an_error(self):
        with pytest.raises(LLMCallError):
            client()._parse_envelope("<html>502 Bad Gateway</html>")


# ---------------------------------------------------------------------------
# The Protocol is the boundary, so a fake must satisfy it
# ---------------------------------------------------------------------------

class FakeClient:
    """A stand-in that satisfies ``LLMClient`` without a network."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="fake")


class TestProtocolBoundary:
    def test_the_real_client_satisfies_the_protocol(self):
        assert isinstance(client(), LLMClient)

    def test_a_fake_client_satisfies_the_protocol(self):
        assert isinstance(FakeClient("{}"), LLMClient)

    def test_the_whole_path_runs_without_a_network(self):
        fake = FakeClient(reply(good_search_payload()))
        request = build_llm_request(RAW_TEXT)
        outcome = parse_reply(fake.complete(request).text, raw_text=RAW_TEXT)
        assert outcome.schema_valid
        assert outcome.actionable
        assert fake.requests[0].user_text == RAW_TEXT
