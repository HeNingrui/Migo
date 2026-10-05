"""The profile chain: what A understands about the user, and how it says so.

Two properties matter more than the rest and are tested first, because they are
the ones a plausible-looking implementation gets wrong:

* **a soft preference never becomes a hard constraint** -- the user saying "我经常
  通勤" describes their life, and a system that turns it into a weight limit has
  set a bound nobody agreed to;
* **explicit and inferred stay distinguishable** -- a fact the user stated is
  quotable back to them, and a fact A worked out is labelled as A's.

The parser tests live here too rather than in ``test_fallback_parser`` because
they are about the profile reading, which is a separate mechanism that happens to
be driven by the same deterministic parser.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.agent import profile_flow as profile  # noqa: E402
from app.agent.fallback_parser import FallbackIntentParser  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.contracts.agent import SessionContext  # noqa: E402
from app.contracts.product import HardConstraints  # noqa: E402
from app.contracts.profile import (  # noqa: E402
    FactSource,
    ProfileFact,
    ProfileGap,
    UserProfile,
    derive_soft_preferences,
    preference_profile_from,
)


# ---------------------------------------------------------------------------
# The type-level guarantee
# ---------------------------------------------------------------------------

class TestASoftPreferenceCannotBecomeAHardConstraint:
    """The property the whole design rests on, asserted where it could break."""

    def test_a_profile_has_no_way_to_express_a_bound(self):
        """No field of a profile fact is a number, a bound or a filter.

        Checked structurally rather than by reading the code: a fact holds a
        field name from a closed vocabulary and a value from a closed vocabulary.
        A later change that let a rule land here would fail on the value domain.
        """
        with pytest.raises(ValueError):
            ProfileFact(field="weight", value="250", source=FactSource.EXPLICIT,
                        evidence_quote="250 克")
        with pytest.raises(ValueError):
            ProfileFact(field="max_wearing_weight_g", value="preferred",
                        source=FactSource.EXPLICIT, evidence_quote="轻")

    def test_deriving_preferences_returns_criteria_and_never_constraints(self):
        described = profile.extract_profile("我每天坐地铁上班，喜欢轻一点的耳机")
        derived = derive_soft_preferences(described)
        assert derived, "a stated weight preference must produce a criterion"
        for criterion in derived:
            assert criterion.attribute in (
                "wearing_weight_g", "battery_hours", "anc", "price_cents",
            )
        # The only object a caller can build from these is a PreferenceProfile,
        # whose whole vocabulary is ordering.
        built = preference_profile_from(described)
        assert built.criteria == derived
        assert isinstance(HardConstraints(), HardConstraints)

    def test_the_profile_module_never_imports_hard_constraints(self):
        """A structural guard, because the tempting change is a convenience import."""
        source = (Path(profile.__file__)).read_text(encoding="utf-8")
        assert "HardConstraints" not in source, (
            "the profile module must not be able to produce a hard constraint; "
            "importing the type is how that starts"
        )

    def test_a_commute_does_not_become_a_weight_limit(self):
        """The exact example the requirement names."""
        described = profile.extract_profile("我经常通勤")
        facts = {f.field for f in described.facts}
        assert "use_case" in facts
        assert "weight" not in facts
        assert not any("wearing_weight_g" in f.field for f in described.facts)


# ---------------------------------------------------------------------------
# Explicit and inferred
# ---------------------------------------------------------------------------

class TestExplicitAndInferredAreDifferentClaims:
    def test_a_stated_fact_keeps_the_users_words(self):
        described = profile.extract_profile("我平时主要用 iPhone 听歌")
        devices = [f for f in described.facts if f.field == "device"]
        assert devices, "a named device is a fact about the user"
        assert devices[0].source is FactSource.EXPLICIT
        assert devices[0].evidence_quote in "我平时主要用 iPhone 听歌"

    def test_an_explicit_fact_must_be_quotable(self):
        with pytest.raises(ValueError, match="evidence_quote"):
            ProfileFact(field="use_case", value="commute",
                        source=FactSource.EXPLICIT)

    def test_an_inference_is_not_dressed_up_as_the_users_words(self):
        described = profile.extract_profile("我每天坐地铁上班")
        combined = described.merge(profile.infer_from(described, "我每天坐地铁上班"))
        inferred = [f for f in combined.facts if f.source is FactSource.INFERRED]
        assert inferred, "a commute is the inference this rule exists for"
        assert inferred[0].field == "anc"
        # The quote on an inference is the evidence it came from, and the summary
        # has to say so rather than presenting it as a statement.
        assert "推断" in profile.describe_profile(combined)

    def test_the_summary_labels_inferences(self):
        stated = UserProfile(facts=[ProfileFact(
            field="use_case", value="commute", source=FactSource.EXPLICIT,
            evidence_quote="通勤")])
        guess = UserProfile(facts=[ProfileFact(
            field="anc", value="medium", source=FactSource.INFERRED,
            evidence_quote="通勤")])
        text = profile.describe_profile(stated.merge(guess))
        assert "我按你说的记下" in text
        assert "我自己推断的" in text

    def test_a_profile_of_nothing_but_guesses_cannot_rank(self):
        guessed = UserProfile(facts=[ProfileFact(
            field="anc", value="medium", source=FactSource.INFERRED,
            evidence_quote="通勤")])
        assert guessed.gap() is ProfileGap.ONLY_INFERRED
        plan = profile.plan(guessed)
        assert plan.wants_an_answer, (
            "ranking on nothing but guesses and presenting it as the user's "
            "preference is the failure this branch prevents"
        )

    def test_an_inference_does_not_close_its_own_question(self):
        """A guess is not an answer, so the turn that would confirm it is offered.

        Ordered after the dimensions the turn never came near -- asking about a
        dimension the user just spoke about reads as not listening -- but not
        dropped, which is how A would otherwise never ask about the one thing it
        guessed wrong.
        """
        inferred = UserProfile(facts=[
            ProfileFact(field="use_case", value="commute",
                        source=FactSource.EXPLICIT, evidence_quote="通勤"),
            ProfileFact(field="anc", value="medium",
                        source=FactSource.INFERRED, evidence_quote="通勤"),
        ])
        # Nothing else has been mentioned, so the real gaps come first.
        assert profile.next_question(inferred)[0] == "weight"

        everything_else_stated = inferred.merge(UserProfile(facts=[
            ProfileFact(field="weight", value="preferred",
                        source=FactSource.EXPLICIT, evidence_quote="轻一点"),
            ProfileFact(field="battery", value="preferred",
                        source=FactSource.EXPLICIT, evidence_quote="续航"),
        ]))
        assert profile.next_question(everything_else_stated)[0] == "anc", (
            "with every other dimension answered, the guess is what is left to ask"
        )

        stated = everything_else_stated.merge(UserProfile(facts=[ProfileFact(
            field="anc", value="preferred", source=FactSource.EXPLICIT,
            evidence_quote="比较重视降噪")]))
        assert profile.next_question(stated) is None, (
            "a stated preference does close its question"
        )

    def test_an_explicit_preference_outranks_an_inferred_one(self):
        mixed = UserProfile(facts=[
            ProfileFact(field="anc", value="preferred", source=FactSource.INFERRED,
                        evidence_quote="通勤"),
            ProfileFact(field="battery", value="preferred", source=FactSource.EXPLICIT,
                        evidence_quote="续航要久"),
            ProfileFact(field="weight", value="preferred", source=FactSource.EXPLICIT,
                        evidence_quote="喜欢轻的"),
        ])
        derived = derive_soft_preferences(mixed)
        sources = [c.source for c in derived]
        assert sources == sorted(sources, key=lambda s: 0 if s == "explicit" else 1)
        assert all(c.priority == "high" for c in derived if c.source == "explicit")
        assert all(c.priority == "medium" for c in derived if c.source == "inferred")


# ---------------------------------------------------------------------------
# Wishes, demands and self-descriptions
# ---------------------------------------------------------------------------

class TestWishesAndDemandsStayApart:
    """The profile must not contradict the search parser about the same word."""

    @pytest.mark.parametrize("text", [
        "必须有降噪",
        "预算 500，必须有主动降噪的无线耳机",
    ])
    def test_a_demand_does_not_become_a_profile_preference(self, text):
        described = profile.extract_profile(text)
        assert described is None or all(f.field != "anc" for f in described.facts), (
            "a demand is a hard constraint; recording it as a soft preference "
            "would make one sentence mean two different things"
        )

    @pytest.mark.parametrize("text", [
        "最好有降噪",
        "希望有主动降噪，300 以内",
        "比较重视降噪",
        "我比较在意续航",
    ])
    def test_a_wish_or_a_stated_concern_does_become_a_preference(self, text):
        described = profile.extract_profile(text)
        assert described is not None, f"{text!r} states something about the user"
        assert described.facts

    def test_a_use_case_is_never_a_device_requirement(self):
        """"打游戏" is what they do; "必须支持游戏主机" is what they own."""
        playing = profile.extract_profile("平时打游戏比较多")
        assert playing is not None
        assert any(f.field == "use_case" and f.value == "gaming" for f in playing.facts)
        assert all(f.field != "device" for f in playing.facts)


class TestTheParserCarriesTheProfile:
    def setup_method(self):
        self.parser = FallbackIntentParser(route_ids=["fps_demo"])
        self.cold = SessionContext(principal_id="u")
        self.warm = SessionContext(principal_id="u",
                                   last_shown_product_ids=["hp_0001", "hp_0008"])

    def test_a_self_description_is_read_without_becoming_a_search(self):
        result = self.parser.parse("我主要在通勤时用，平时会连接手机和电脑", self.cold)
        assert result.profile is not None
        fields = {f.field for f in result.profile.facts}
        assert "use_case" in fields and "device" in fields
        # Nothing was demanded of a product, so nothing may be filtered on.
        assert result.search is not None and result.search.is_empty()

    def test_a_device_is_only_a_requirement_when_it_is_demanded(self):
        demanded = self.parser.parse("必须支持游戏主机", self.cold)
        assert demanded.mandate is not None
        assert demanded.mandate.required_device == "game_console"
        assert demanded.source_spans.get("required_device")

        mentioned = self.parser.parse("我平时连手机和电脑", self.cold)
        assert mentioned.mandate is None, (
            "describing what you own is not authorising a filter"
        )
        assert mentioned.profile is not None

    def test_a_device_demand_is_not_silently_read_as_a_product_filter(self):
        """The search patch has nowhere to put a device, so it must not swallow it."""
        demanded = self.parser.parse("必须支持游戏主机的耳机", self.cold)
        mandate_device = demanded.mandate.required_device if demanded.mandate else None
        assert mandate_device in (None, "game_console")
        if demanded.search is not None:
            assert "required_device" not in demanded.search.model_dump()

    def test_a_rejection_is_not_a_refinement(self):
        result = self.parser.parse("太重了", self.warm)
        assert result.intent.value == "REJECT_RECOMMENDATION"
        assert result.search is None

    def test_a_rejection_with_no_subject_to_reject_is_not_read_as_one(self):
        """On a cold turn "太重" has nothing to be heavy relative to."""
        result = self.parser.parse("太重了", self.cold)
        assert result.intent.value != "REJECT_RECOMMENDATION"

    def test_a_number_wins_over_a_complaint_word(self):
        """"太贵了，200 以内" states a budget; reading it as a complaint loses it."""
        result = self.parser.parse("太贵了，200 以内", self.warm)
        assert result.intent.value == "UPDATE_SEARCH"
        assert result.search.max_price_cents == 20000

    def test_a_description_with_a_direction_word_is_not_a_complaint(self):
        """"比较喜欢轻一点的耳机" describes the user; "太重了" complains."""
        assert not profile.looks_like_rejection("比较喜欢轻一点的耳机")
        assert profile.looks_like_rejection("太重了")
        result = self.parser.parse("比较喜欢轻一点的耳机", self.warm)
        assert result.intent.value != "REJECT_RECOMMENDATION"


class TestRejectionReading:
    def test_a_named_dimension_is_reported_as_a_direction(self):
        read = profile.read_rejection("太重了")
        assert read.direction == "max_wearing_weight_g"
        assert read.understood
        assert any(f.field == "weight" for f in read.profile.facts)

    def test_a_dimension_only_mentioned_as_a_priority_is_still_a_direction(self):
        read = profile.read_rejection("我更在意续航")
        assert read.direction == "min_battery_hours"

    def test_plain_dissatisfaction_names_no_dimension(self):
        read = profile.read_rejection("都不喜欢")
        assert read.direction is None
        assert read.asks_nothing_specific
        assert read.understood

    def test_a_rejection_never_carries_a_number(self):
        for text in ("太重了", "太贵了", "我更在意续航", "都不喜欢"):
            read = profile.read_rejection(text)
            for fact in read.profile.facts:
                assert not any(ch.isdigit() for ch in fact.value)

    def test_an_unrecognised_turn_is_not_a_rejection(self):
        assert not profile.read_rejection("今天天气不错").understood


# ---------------------------------------------------------------------------
# The session holds the profile and derives preferences from it
# ---------------------------------------------------------------------------

class TestTheSessionDerivesPreferences:
    def test_an_empty_update_does_not_erase_what_was_known(self):
        session = AgentSession(session_id="s1", principal_id="u")
        session.remember_profile(profile.extract_profile("我每天通勤"))
        before = list(session.profile.facts)
        session.remember_profile(None)
        session.remember_profile(UserProfile())
        assert session.profile.facts == before

    def test_a_correction_replaces_a_preference_instead_of_duplicating_it(self):
        session = AgentSession(session_id="s1", principal_id="u")
        session.remember_profile(profile.extract_profile("我比较重视降噪"))
        session.remember_profile(profile.extract_profile("其实我更需要长续航"))
        values = {f.field: f.value for f in session.profile.facts}
        assert values.get("battery") == "preferred"
        assert len([f for f in session.profile.facts if f.field == "battery"]) == 1

    def test_soft_preferences_are_derived_on_every_read(self):
        session = AgentSession(session_id="s1", principal_id="u")
        assert session.soft_preferences().is_empty()
        session.remember_profile(profile.extract_profile("我比较在意续航"))
        assert session.soft_preferences().attributes() == ["battery_hours"]
        session.remember_profile(profile.extract_profile("还是更在意重量"))
        assert "wearing_weight_g" in session.soft_preferences().attributes()

    def test_use_cases_travel_to_b_only_when_stated(self):
        session = AgentSession(session_id="s1", principal_id="u")
        session.remember_profile(profile.extract_profile("我上下班坐地铁"))
        assert session.soft_preferences().use_cases == ["commute"]

        guessed = UserProfile(facts=[ProfileFact(
            field="use_case", value="gaming", source=FactSource.INFERRED,
            evidence_quote="游戏")])
        other = AgentSession(session_id="s2", principal_id="u")
        other.remember_profile(guessed)
        assert other.soft_preferences().use_cases == [], (
            "an inferred use case handed to B is a guess used as a filter"
        )


class TestTheTwoSoftnessRulesAgree:
    """One rule, implemented twice, pinned against itself.

    ``profile_flow.softness_of`` and ``fallback_parser._is_soft`` answer the same
    question -- does a wish word or a demand word govern this position? -- and
    they cannot share code, because the profile module must not import the parser
    (the parser imports the profile module). The comment at the profile copy
    claims they agree; without this test that claim is an aspiration.

    They agree on the sentences that matter. They are *not* identical: the profile
    also treats a stated concern ("比较重视降噪") as a preference, which the parser
    deliberately does not, because for the parser that sentence would have to
    become a hard constraint and for the profile it must not. That asymmetry is
    asserted separately, so it stays deliberate rather than becoming drift.
    """

    @pytest.mark.parametrize("text,expected", [
        ("最好有降噪", True),
        ("希望有主动降噪，300 以内", True),
        ("必须有降噪", False),
        ("必须降噪，最好轻一点", False),
        ("预算 500，要头戴式，最好有降噪", True),
    ])
    def test_both_rules_report_the_same_governing_word(self, text, expected):
        from app.agent.fallback_parser import ANC_WORDS, _any, _is_soft

        # The position must be measured on the same string both rules see, which
        # is why the search runs on the lowercased text: ``_is_soft`` lowercases
        # internally, and an offset taken from the original can land on the wrong
        # character in a sentence that mixes cases.
        lowered = text.lower()
        span = _any(lowered, ANC_WORDS)
        assert span is not None, f"{text!r} does not mention ANC at all"
        at = lowered.find(span)
        assert _is_soft(text, at) is expected
        assert profile.softness_of(text, at) is expected, (
            "the two implementations of the softness rule have drifted"
        )

    def test_the_profile_is_wider_only_where_it_is_meant_to_be(self):
        """The one deliberate difference, stated as a test rather than a comment."""
        from app.agent.fallback_parser import ANC_WORDS, _any, _is_soft

        text = "比较重视降噪"
        lowered = text.lower()
        at = lowered.find(_any(lowered, ANC_WORDS))
        assert _is_soft(text, at) is False, (
            "the parser must not read a stated concern as a wish: for the parser "
            "the alternative is a hard constraint, and 重视 is not a demand"
        )
        assert profile.softness_of(text, at) is True, (
            "the profile must read it as a preference: 'I care about ANC' is "
            "exactly the sentence a profile exists to hold"
        )

    def test_the_two_vocabularies_overlap_enough_to_be_the_same_rule(self):
        from app.agent.fallback_parser import FallbackIntentParser

        vocabulary = FallbackIntentParser.vocabulary()["soft_words"]
        parser_soft = {w.lower() for w in vocabulary}
        profile_soft = {w.lower() for w in profile.SOFT_CUES}
        shared = parser_soft & profile_soft
        assert len(shared) >= 5, (
            "the two lists have drifted apart; they are meant to describe the "
            f"same set of wishes. shared={sorted(shared)}"
        )


__all__ = [
    "TestASoftPreferenceCannotBecomeAHardConstraint",
    "TestExplicitAndInferredAreDifferentClaims",
    "TestRejectionReading",
    "TestTheParserCarriesTheProfile",
    "TestTheSessionDerivesPreferences",
    "TestTheTwoSoftnessRulesAgree",
    "TestWishesAndDemandsStayApart",
]
