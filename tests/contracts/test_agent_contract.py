"""Contract tests for the natural-language structuring boundary.

These pin the four decisions the agent contracts rest on:

1. one intent vocabulary, shared by the parser and the action dispatcher
2. a parse can never carry authority
3. an untraceable value is treated as invented
4. parsers are interchangeable
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts import (
    AgentActionRequest,
    Ambiguity,
    AmbiguityKind,
    Clarification,
    ConstraintPatch,
    HardConstraints,
    Intent,
    IntentResult,
    LLMRequest,
    MandateDraft,
    ParseSource,
    SessionContext,
    TargetReference,
    UserActionType,
)
from app.contracts.agent import (
    MANDATE_INTENTS,
    SEARCH_INTENTS,
    IntentParser,
    LLMClient,
)
from app.contracts.common import AgentPhase, NextAction


# ---------------------------------------------------------------------------
# One vocabulary
# ---------------------------------------------------------------------------

def test_user_action_type_is_the_same_object_as_intent():
    """A second copy of the list would drift and nothing would catch it."""
    assert UserActionType is Intent


def test_intent_is_importable_from_common_for_backwards_compatibility():
    """The old import path must keep working, resolving to the same enum."""
    from app.contracts.common import UserActionType as FromCommon

    assert FromCommon is Intent


def test_unknown_attribute_on_common_still_raises():
    import app.contracts.common as common

    with pytest.raises(AttributeError):
        common.NotARealThing


def test_every_action_the_dispatcher_accepts_is_a_known_intent():
    """The spec lists each user action; none may be missing from the enum."""
    for name in ("SEARCH", "UPDATE_SEARCH", "ASK_ALTERNATIVES", "CREATE_MANDATE",
                 "UPDATE_MANDATE_DRAFT", "ACTIVATE_MANDATE", "REVOKE_MANDATE",
                 "RUN_DELEGATED_PURCHASE", "APPROVE_ESCALATION",
                 "REJECT_ESCALATION", "SELECT_PRODUCT", "CONFIRM_MANUAL_PAYMENT",
                 "CANCEL_ORDER", "CHECK_STATUS", "UNKNOWN"):
        assert Intent[name].value == name


def test_intent_groups_are_consistent():
    # ASK_ALTERNATIVES belongs to the search family even though it changes no
    # criteria. Requiring its (empty) patch is deliberate: a field that is
    # sometimes required and sometimes not is a field a model gets wrong, and an
    # empty patch states "nothing changed" unambiguously.
    assert SEARCH_INTENTS == {Intent.SEARCH, Intent.UPDATE_SEARCH,
                             Intent.ASK_ALTERNATIVES}
    assert Intent.ACTIVATE_MANDATE in MANDATE_INTENTS
    assert Intent.SEARCH not in MANDATE_INTENTS
    for intent in SEARCH_INTENTS | MANDATE_INTENTS:
        assert intent in Intent
    assert not (SEARCH_INTENTS & MANDATE_INTENTS), "no intent is both"


# ---------------------------------------------------------------------------
# A parse carries no authority
# ---------------------------------------------------------------------------

def test_intent_result_has_no_authority_fields():
    for forbidden in ("cash_total_cents", "policy_hash", "approved", "decision",
                      "payment_status", "capability", "remaining_budget_cents"):
        with pytest.raises(ValidationError):
            IntentResult.model_validate({
                "intent": "CHECK_STATUS", "raw_text": "status please", forbidden: 1,
            })


def test_search_intent_must_carry_criteria():
    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.SEARCH, raw_text="find me headphones")


def test_non_search_intent_must_not_carry_criteria():
    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.CHECK_STATUS, raw_text="status",
                     search=ConstraintPatch(max_price_cents=100))


def test_mandate_draft_only_on_mandate_intents():
    draft = MandateDraft(cap_per_transaction_cents=30000)
    IntentResult(intent=Intent.CREATE_MANDATE, raw_text="authorise this", mandate=draft)

    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.SEARCH, raw_text="find headphones",
                     search=ConstraintPatch(), mandate=draft)


def test_unknown_intent_must_explain_itself():
    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.UNKNOWN, raw_text="mmm")

    result = IntentResult(intent=Intent.UNKNOWN, raw_text="mmm", ambiguities=[
        Ambiguity(kind=AmbiguityKind.MISSING, field="request",
                  detail="no actionable request", question="您想找商品，还是想设置授权？"),
    ])
    assert result.is_actionable() is False


# ---------------------------------------------------------------------------
# Traceability: the anti-fabrication check
# ---------------------------------------------------------------------------

def test_fully_traced_parse_is_actionable():
    result = IntentResult(
        intent=Intent.SEARCH,
        raw_text="300 以内、无线、要降噪",
        search=ConstraintPatch(max_price_cents=30000, connection="wireless",
                               anc_required=True),
        source_spans={"max_price_cents": "300 以内", "connection": "无线",
                      "anc_required": "要降噪"},
    )
    assert result.untraceable_fields() == []
    assert result.is_actionable() is True


def test_invented_value_is_caught_and_blocks_action():
    """The failure this check exists for: a model supplies a budget nobody gave."""
    result = IntentResult(
        intent=Intent.SEARCH, raw_text="帮我找耳机",
        search=ConstraintPatch(max_price_cents=30000),
    )
    assert result.untraceable_fields() == ["max_price_cents"]
    assert result.is_actionable() is False


def test_partially_traced_parse_reports_only_the_missing_ones():
    result = IntentResult(
        intent=Intent.SEARCH, raw_text="找无线耳机，300以内",
        search=ConstraintPatch(max_price_cents=30000, connection="wireless",
                               anc_required=True),
        source_spans={"max_price_cents": "300以内", "connection": "无线"},
    )
    assert result.untraceable_fields() == ["anc_required"]


def test_soft_criteria_do_not_need_to_be_traceable():
    """Only values that decide how much money moves are gated."""
    result = IntentResult(
        intent=Intent.SEARCH, raw_text="找点轻的耳机",
        search=ConstraintPatch(form_factor="in_ear"),
    )
    assert result.untraceable_fields() == []
    assert result.is_actionable() is True


def test_mandate_money_fields_must_be_traced():
    result = IntentResult(
        intent=Intent.CREATE_MANDATE, raw_text="授权你买耳机",
        mandate=MandateDraft(cap_per_transaction_cents=30000,
                             rolling_cap_cents=50000,
                             rolling_window_seconds=86400),
        source_spans={"cap_per_transaction_cents": "每笔不超过 300"},
    )
    untraceable = result.untraceable_fields()
    assert "rolling_cap_cents" in untraceable
    assert "cap_per_transaction_cents" not in untraceable
    assert result.is_actionable() is False


def test_llm_result_with_missing_spans_is_worth_one_repair():
    result = IntentResult(intent=Intent.SEARCH, raw_text="300以内",
                          parse_source=ParseSource.LLM,
                          search=ConstraintPatch(max_price_cents=30000))
    assert result.requires_reparse_attempt() is True

    fallback = result.model_copy(update={"parse_source": ParseSource.FALLBACK})
    assert fallback.requires_reparse_attempt() is False


def test_traced_llm_result_needs_no_repair():
    result = IntentResult(intent=Intent.SEARCH, raw_text="300以内",
                          parse_source=ParseSource.LLM,
                          search=ConstraintPatch(max_price_cents=30000),
                          source_spans={"max_price_cents": "300以内"})
    assert result.requires_reparse_attempt() is False
    assert result.is_actionable() is True


# ---------------------------------------------------------------------------
# Ambiguity and clarification
# ---------------------------------------------------------------------------

def test_no_ambiguity_means_nothing_to_ask():
    assert Clarification.from_ambiguities([]) is None


def test_a_contradiction_is_asked_about_before_a_missing_value():
    """Asking for a budget while two are on the table yields a third."""
    ambiguities = [
        Ambiguity(kind=AmbiguityKind.MISSING, field="valid_for_seconds",
                  detail="no expiry given", question="授权多久有效？"),
        Ambiguity(kind=AmbiguityKind.CONTRADICTORY, field="cap_per_transaction_cents",
                  detail="300 and 500 both given",
                  question="单笔上限是 300 还是 500？"),
    ]
    clarification = Clarification.from_ambiguities(ambiguities)
    assert clarification is not None
    assert "300 还是 500" in clarification.question
    assert clarification.blocking is True
    assert set(clarification.about) == {"valid_for_seconds", "cap_per_transaction_cents"}


def test_an_unsupported_request_does_not_block_but_is_still_reported():
    ambiguities = [
        Ambiguity(kind=AmbiguityKind.UNSUPPORTED, field="audio_quality",
                  detail="no data to rank by", question="音质我无法评估，可以接受吗？"),
    ]
    clarification = Clarification.from_ambiguities(ambiguities)
    assert clarification is not None
    assert clarification.blocking is False


def test_a_blocking_ambiguity_makes_the_turn_inactionable():
    result = IntentResult(intent=Intent.SEARCH, raw_text="找耳机",
                          search=ConstraintPatch(max_price_cents=30000),
                          source_spans={"max_price_cents": "300"},
                          ambiguities=[Ambiguity(
                              kind=AmbiguityKind.CONTRADICTORY,
                              field="connection",
                              detail="wired and wireless both requested",
                              question="您要的是有线还是无线？")])
    assert result.is_actionable() is False
    assert len(result.blocking_ambiguities()) == 1
    assert result.clarification() is not None


def test_a_non_blocking_ambiguity_still_allows_progress():
    result = IntentResult(intent=Intent.SEARCH, raw_text="300以内",
                          search=ConstraintPatch(max_price_cents=30000),
                          source_spans={"max_price_cents": "300以内"},
                          ambiguities=[Ambiguity(
                              kind=AmbiguityKind.UNSUPPORTED, field="audio_quality",
                              detail="cannot rank by sound", question="音质无法评估")])
    assert result.is_actionable() is True


def test_clarification_asks_one_question():
    ambiguities = [
        Ambiguity(kind=AmbiguityKind.MISSING, field="a", detail="d", question="Q-a?"),
        Ambiguity(kind=AmbiguityKind.MISSING, field="b", detail="d", question="Q-b?"),
    ]
    clarification = Clarification.from_ambiguities(ambiguities)
    assert clarification.question in {"Q-a?", "Q-b?"}
    assert len(clarification.about) == 2   # the rest is context, not more questions


# ---------------------------------------------------------------------------
# Constraint patches
# ---------------------------------------------------------------------------

def test_patch_applies_only_what_the_user_mentioned():
    base = HardConstraints(anc_required=True, connection="wireless",
                           max_price_cents=30000)
    merged = ConstraintPatch(max_price_cents=50000).apply_to(base)
    assert merged.max_price_cents == 50000
    assert merged.connection == "wireless"      # untouched
    assert merged.anc_required is True          # untouched


def test_clearing_is_distinct_from_not_mentioning():
    """'do not care about ANC' and 'you have not told me about ANC' differ.

    Collapsing them is how a relaxed requirement silently becomes a required one.
    """
    base = HardConstraints(anc_required=True, connection="wireless")

    untouched = ConstraintPatch(max_price_cents=1000).apply_to(base)
    assert untouched.anc_required is True

    cleared = ConstraintPatch(clear_anc_required=True).apply_to(base)
    assert cleared.anc_required is False
    assert cleared.connection == "wireless"


def test_clearing_an_optional_field_returns_it_to_unset():
    base = HardConstraints(connection="wireless", max_wearing_weight_g=200)
    cleared = ConstraintPatch(clear_connection=True, clear_max_wearing_weight_g=True).apply_to(base)
    assert cleared.connection is None
    assert cleared.max_wearing_weight_g is None


def test_clear_wins_over_a_value_in_the_same_patch():
    base = HardConstraints(connection="wireless")
    patch = ConstraintPatch(connection="wired", clear_connection=True)
    assert patch.apply_to(base).connection is None


def test_patch_merge_is_deterministic():
    base = HardConstraints(anc_required=True)
    patch = ConstraintPatch(max_price_cents=30000, connection="wireless")
    assert patch.apply_to(base).model_dump() == patch.apply_to(base).model_dump()


def test_empty_patch_is_detected():
    assert ConstraintPatch().is_empty() is True
    assert ConstraintPatch(max_price_cents=1).is_empty() is False
    assert ConstraintPatch(clear_connection=True).is_empty() is False


def test_patch_rejects_unknown_fields_and_negative_money():
    with pytest.raises(ValidationError):
        ConstraintPatch(prefer_anc=True)
    with pytest.raises(ValidationError):
        ConstraintPatch(max_price_cents=-1)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def test_reference_needs_exactly_one_handle():
    TargetReference(kind="product", rank=2)
    TargetReference(kind="product", product_id="hp_0001")

    with pytest.raises(ValidationError):
        TargetReference(kind="product")
    with pytest.raises(ValidationError):
        TargetReference(kind="product", rank=2, product_id="hp_0001")


def test_rank_is_one_based():
    with pytest.raises(ValidationError):
        TargetReference(kind="product", rank=0)


# ---------------------------------------------------------------------------
# Explicit actions
# ---------------------------------------------------------------------------

def test_paying_requires_the_order_and_the_amount_that_was_displayed():
    with pytest.raises(ValidationError):
        AgentActionRequest(session_id="s", intent=Intent.CONFIRM_MANUAL_PAYMENT,
                           order_id="ord_1")
    with pytest.raises(ValidationError):
        AgentActionRequest(session_id="s", intent=Intent.CONFIRM_MANUAL_PAYMENT,
                           confirmed_total_cents=100)

    ok = AgentActionRequest(session_id="s", intent=Intent.CONFIRM_MANUAL_PAYMENT,
                            order_id="ord_1", confirmed_total_cents=28900)
    assert ok.confirmed_total_cents == 28900


def test_selecting_requires_a_product():
    with pytest.raises(ValidationError):
        AgentActionRequest(session_id="s", intent=Intent.SELECT_PRODUCT)
    AgentActionRequest(session_id="s", intent=Intent.SELECT_PRODUCT, product_id="hp_0001")


def test_revoking_requires_a_mandate():
    with pytest.raises(ValidationError):
        AgentActionRequest(session_id="s", intent=Intent.REVOKE_MANDATE)
    AgentActionRequest(session_id="s", intent=Intent.REVOKE_MANDATE, mandate_id="man_1")


def test_escalation_resolution_requires_a_proposal():
    for intent in (Intent.APPROVE_ESCALATION, Intent.REJECT_ESCALATION):
        with pytest.raises(ValidationError):
            AgentActionRequest(session_id="s", intent=intent)
        AgentActionRequest(session_id="s", intent=intent, proposal_id="prop_1")


def test_plain_search_action_needs_no_handles():
    AgentActionRequest(session_id="s", intent=Intent.SEARCH)


# ---------------------------------------------------------------------------
# Parser boundary is substitutable
# ---------------------------------------------------------------------------

def test_a_parser_satisfies_the_protocol():
    class StubParser:
        source = ParseSource.FALLBACK

        def parse(self, text: str, context: SessionContext) -> IntentResult:
            return IntentResult(intent=Intent.SEARCH, raw_text=text,
                                search=ConstraintPatch())

    assert isinstance(StubParser(), IntentParser)


def test_an_llm_client_satisfies_the_protocol():
    class StubClient:
        def complete(self, request: LLMRequest):
            raise NotImplementedError

    assert isinstance(StubClient(), LLMClient)


def test_llm_request_is_provider_neutral():
    """No SDK type may appear here, or the model choice becomes architectural."""
    request = LLMRequest(system_prompt="extract", user_text="300以内",
                         response_schema={"type": "object"})
    assert request.temperature == 0.0
    fields = set(LLMRequest.model_fields)
    assert not any("openai" in f or "anthropic" in f or "gemini" in f for f in fields)


def test_session_context_carries_no_money_authority():
    context = SessionContext(principal_id="demo_user", phase=AgentPhase.IDLE,
                             last_shown_product_ids=["hp_0001", "hp_0002"])
    assert context.current_constraints is None
    assert context.has_active_mandate is False
    for forbidden in ("wallet_balance_cents", "remaining_budget_cents", "spend_state"):
        with pytest.raises(ValidationError):
            SessionContext(principal_id="u", **{forbidden: 1})


def test_next_action_covers_the_flow():
    for name in ("ANSWER_QUESTION", "SELECT_PRODUCT", "CONFIRM_MANDATE",
                 "CONFIRM_PAYMENT", "RUN_PURCHASE", "APPROVE_ESCALATION",
                 "CHECK_STATUS", "NONE"):
        assert NextAction[name].value == name.lower()
