"""The deterministic parser, tested exhaustively.

This is the file that justifies having a fallback at all: a model's output can
only be sampled, but this parser can be pinned. Every case below is either a
documented demo sentence or a bug that was actually found and fixed.
"""

from __future__ import annotations

import pytest

from app.agent.fallback_parser import FallbackIntentParser
from app.contracts.agent import AmbiguityKind, Intent, ParseSource, SessionContext


@pytest.fixture
def parser() -> FallbackIntentParser:
    return FallbackIntentParser()


@pytest.fixture
def context() -> SessionContext:
    return SessionContext(principal_id="demo_user")


def parsed(parser, context, text):
    return parser.parse(text, context)


def values(result) -> dict:
    return result.search.model_dump(exclude_none=True) if result.search else {}


# ---------------------------------------------------------------------------
# The demo sentence, in the languages the team actually types
# ---------------------------------------------------------------------------

class TestDemoSentence:
    @pytest.mark.parametrize(
        "text",
        [
            "找 300 以内、通勤用、必须支持主动降噪的无线耳机",
            "wireless headphones under HK$300 with active noise cancelling",
            "300 蚊以内嘅无线降噪耳机",
            "預算 300，要無線，必須有主動降噪",
        ],
    )
    def test_price_wireless_and_anc_are_extracted(self, parser, context, text):
        result = parsed(parser, context, text)
        payload = values(result)
        assert payload["max_price_cents"] == 30000
        assert payload["connection"] == "wireless"
        assert payload["anc_required"] is True

    def test_every_extracted_value_quotes_the_user(self, parser, context):
        """The point of the fallback: it cannot invent a number."""
        result = parsed(parser, context, "找 300 以内、通勤用、必须支持主动降噪的无线耳机")
        assert result.untraceable_fields() == []
        assert set(result.source_spans) >= {"max_price_cents", "connection", "anc_required"}


class TestWordOrder:
    """A regulation bug: cues were only searched backwards from the number.

    English and Chinese put the budget cue on opposite sides -- "under HK$300"
    vs "300 以内" -- and Chinese also leads with it, as in "预算 500".
    """

    def test_cue_after_the_number(self, parser, context):
        assert values(parsed(parser, context, "300 以内"))["max_price_cents"] == 30000

    def test_cue_before_the_number(self, parser, context):
        assert values(parsed(parser, context, "under HK$300"))["max_price_cents"] == 30000

    def test_leading_chinese_cue(self, parser, context):
        assert values(parsed(parser, context, "预算 500"))["max_price_cents"] == 50000

    def test_currency_between_number_and_cue(self, parser, context):
        assert values(parsed(parser, context, "300 蚊以内"))["max_price_cents"] == 30000

    def test_no_more_than_phrasing(self, parser, context):
        payload = values(parsed(parser, context, "no more than 250 please"))
        assert payload["max_price_cents"] == 25000


class TestMeasurementsAreNotMoney:
    """A regulation bug: "30 克以内" was read as a HK$30 budget.

    The cue "以内" is generic, so a number carrying a unit had to stop being
    treated as currency.
    """

    def test_grams_are_not_a_budget(self, parser, context):
        payload = values(parsed(parser, context, "30 克以内的入耳式"))
        assert payload["max_wearing_weight_g"] == 30.0
        assert "max_price_cents" not in payload

    def test_battery_hours_are_not_a_budget(self, parser, context):
        payload = values(parsed(parser, context, "续航 20 小时以上"))
        assert payload["min_battery_hours"] == 20.0
        assert "max_price_cents" not in payload

    def test_a_real_budget_survives_beside_a_measurement(self, parser, context):
        payload = values(parsed(parser, context, "30 克以内、500 以内的无线耳机"))
        assert payload["max_price_cents"] == 50000
        assert payload["max_wearing_weight_g"] == 30.0


# ---------------------------------------------------------------------------
# Unknown is not satisfied
# ---------------------------------------------------------------------------

class TestWishesAreNotRequirements:
    """``anc_required`` may only be set by a demand, never by a preference.

    The system treats an unknown specification as *unsatisfied*, so reading a
    wish as a requirement silently eliminates products the user would have
    accepted.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "预算 500，要头戴式，最好有降噪",
            "over-ear under HK$500, ideally with noise cancelling",
            "希望有主动降噪，300 以内",
        ],
    )
    def test_a_soft_word_leaves_anc_unset(self, parser, context, text):
        assert "anc_required" not in values(parsed(parser, context, text))

    def test_a_demand_still_sets_it(self, parser, context):
        assert values(parsed(parser, context, "必须有降噪"))["anc_required"] is True

    def test_a_wish_does_not_cancel_an_earlier_demand(self, parser, context):
        """Proximity matters: the wish is about weight, not about ANC."""
        payload = values(parsed(parser, context, "必须降噪，最好轻一点"))
        assert payload["anc_required"] is True


# ---------------------------------------------------------------------------
# References to what was shown
# ---------------------------------------------------------------------------

class TestReferences:
    @pytest.mark.parametrize(
        "text,rank",
        [("第二款", 2), ("第二个", 2), ("the second one", 2), ("第一款", 1), ("第三款", 3)],
    )
    def test_rank_resolves_to_a_shown_position(self, parser, context, text, rank):
        result = parsed(parser, context, text)
        assert result.intent == Intent.SELECT_PRODUCT
        assert result.target is not None and result.target.rank == rank


class TestDirectionOnlyRefinement:
    """A direction with no number is not a blocking problem -- but not nothing.

    "cheaper" is answerable from the results already on screen, so the turn
    proceeds; the ambiguity records what A still has to turn into a value.
    """

    @pytest.mark.parametrize(
        "text,field",
        [("便宜点", "max_price_cents"), ("轻一点", "max_wearing_weight_g"),
         ("cheaper", "max_price_cents"), ("电池再久一点", "min_battery_hours")],
    )
    def test_a_direction_carries_an_underspecified_ambiguity(
        self, parser, context, text, field
    ):
        result = parsed(parser, context, text)
        assert result.intent == Intent.UPDATE_SEARCH
        assert result.search is not None and result.search.is_empty()
        kinds = {a.kind for a in result.ambiguities}
        assert AmbiguityKind.UNDERSPECIFIED in kinds
        assert field in {a.field for a in result.ambiguities}
        # Underspecified does not block: blocking would ask a question the
        # results can already answer.
        assert result.is_actionable() is True

    def test_a_number_beats_a_direction_word(self, parser, context):
        """"续航 20 小时以上" is a requirement, not a wish for more battery."""
        payload = values(parsed(parser, context, "续航 20 小时以上"))
        assert payload["min_battery_hours"] == 20.0


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------

class TestRefusesToGuess:
    @pytest.mark.parametrize("text", ["随便看看", "嗯", "hello", "???"])
    def test_nothing_recognisable_asks_a_question(self, parser, context, text):
        result = parsed(parser, context, text)
        assert result.intent == Intent.UNKNOWN
        assert result.ambiguities, "an unrecognised turn must say what is unclear"
        assert result.is_actionable() is False
        assert result.clarification() is not None

    def test_no_invented_values_anywhere(self, parser, context):
        for text in ("随便看看", "买点东西", "something nice"):
            result = parsed(parser, context, text)
            assert result.untraceable_fields() == []
            assert values(result) == {}


class TestResultShape:
    def test_parse_source_is_recorded(self, parser, context):
        assert parsed(parser, context, "300 以内").parse_source == ParseSource.FALLBACK

    def test_a_search_intent_always_carries_criteria(self, parser, context):
        """The contract rejects a SEARCH with no payload; the parser must comply."""
        result = parsed(parser, context, "找 300 以内的无线耳机")
        assert result.intent in (Intent.SEARCH, Intent.UPDATE_SEARCH)
        assert result.search is not None

    def test_the_parser_satisfies_the_protocol(self, parser):
        from app.contracts.agent import IntentParser

        assert isinstance(parser, IntentParser)
