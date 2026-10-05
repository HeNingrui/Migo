"""Contract tests for the A -> B search boundary.

These pin the split the design rests on: A supplies the judgement (what matters,
in which direction, which dimensions to compare), B supplies the facts. Almost
every test here is about a boundary that would otherwise let B guess, or let A
discover a mistake only in the UI.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts import (
    CONSTRAINT_TO_ATTRIBUTE,
    Candidate,
    ComparisonCell,
    ComparisonFrame,
    ComparisonRow,
    Criterion,
    DimensionStats,
    GapReport,
    HardConstraints,
    MatchReason,
    OverBudgetAlternative,
    PreferenceProfile,
    Product,
    RelaxHints,
    RelaxationOption,
    SearchOutcome,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SummaryResponse,
    classify_outcome,
)
from app.contracts.search import NATURAL_DIRECTION, RELAXABLE_FIELDS


def make_product(**overrides) -> Product:
    payload = dict(
        product_id="hp_0001", category="headphones", name="Demo Headphones",
        brand="Demo", model="D1", variant="Black", connection="wireless",
        form_factor="in_ear", price_cents=29900, currency="HKD", stock=10,
        anc=True, battery_hours=8.0, wearing_weight_g=9.0, use_cases=["commute"],
        source_type="demo", source_url=None, data_note="demo",
        seller_description="A short demo description.",
        shipping_origin="Hong Kong",
    )
    payload.update(overrides)
    return Product.model_validate(payload)


def make_candidate(product_id="hp_0001", **overrides) -> Candidate:
    payload = dict(product=make_product(product_id=product_id), match_score=10,
                   reasons=[MatchReason(field="anc", observed=True, text="supports ANC")],
                   preference_misses=[], violated_fields=[])
    payload.update(overrides)
    return Candidate.model_validate(payload)


def make_response(**overrides) -> SearchResponse:
    payload = dict(total_matches=1, returned=1,
                   applied_constraints=HardConstraints(max_price_cents=30000),
                   applied_criteria=["battery_hours"],
                   candidates=[make_candidate()],
                   comparison=[], gaps=GapReport(), relax_hints=None, suggestions=[])
    payload.update(overrides)
    return SearchResponse.model_validate(payload)


# ---------------------------------------------------------------------------
# Criterion
# ---------------------------------------------------------------------------

def test_criterion_keeps_the_users_own_words():
    criterion = Criterion(attribute="battery_hours", direction="higher",
                          priority="high", source="explicit",
                          evidence_quote="续航要长一点")
    assert criterion.evidence_quote == "续航要长一点"


def test_unknown_criterion_can_be_reported_without_becoming_a_fact():
    """B reports unsupported dimensions in gaps; it never fabricates their values."""
    criterion = Criterion(attribute="audio_quality")
    assert criterion.attribute == "audio_quality"
    with pytest.raises(ValidationError):
        Criterion(attribute="")


def test_closer_to_requires_a_target_and_others_forbid_it():
    Criterion(attribute="wearing_weight_g", direction="closer_to", target=200.0)
    with pytest.raises(ValidationError):
        Criterion(attribute="wearing_weight_g", direction="closer_to")
    with pytest.raises(ValidationError):
        Criterion(attribute="wearing_weight_g", direction="lower", target=200.0)


def test_criterion_defaults_are_conservative():
    criterion = Criterion(attribute="anc")
    assert criterion.priority == "medium"
    assert criterion.source == "explicit"
    assert criterion.target is None


# ---------------------------------------------------------------------------
# PreferenceProfile
# ---------------------------------------------------------------------------

def test_prefer_anc_defaults_to_false():
    """A silent default of true would add five points to everyone and mean nothing."""
    assert PreferenceProfile().prefer_anc is False


def test_criteria_order_is_preserved():
    profile = PreferenceProfile(criteria=[
        Criterion(attribute="battery_hours", direction="higher", priority="high"),
        Criterion(attribute="wearing_weight_g", direction="lower", priority="low"),
    ])
    assert profile.attributes() == ["battery_hours", "wearing_weight_g"]


def test_a_dimension_may_not_be_given_two_directions():
    with pytest.raises(ValidationError):
        PreferenceProfile(criteria=[
            Criterion(attribute="anc", direction="higher"),
            Criterion(attribute="anc", direction="lower"),
        ])


def test_duplicate_use_cases_are_rejected():
    with pytest.raises(ValidationError):
        PreferenceProfile(use_cases=["commute", "commute"])


def test_unknown_preference_fields_are_rejected():
    with pytest.raises(ValidationError):
        PreferenceProfile(min_battery_hours=20)


def test_empty_profile_is_detected():
    assert PreferenceProfile().is_empty() is True
    assert PreferenceProfile(use_cases=["music"]).is_empty() is False


# ---------------------------------------------------------------------------
# ComparisonFrame
# ---------------------------------------------------------------------------

def test_comparison_frame_is_bounded():
    assert ComparisonFrame().max_columns == 3
    ComparisonFrame(max_columns=2)
    ComparisonFrame(max_columns=5)
    with pytest.raises(ValidationError):
        ComparisonFrame(max_columns=1)
    with pytest.raises(ValidationError):
        ComparisonFrame(max_columns=6)


# ---------------------------------------------------------------------------
# SearchRequest: comparison dimensions must be justified
# ---------------------------------------------------------------------------

def test_dimension_from_a_constraint_is_allowed():
    request = SearchRequest(
        constraints=HardConstraints(max_price_cents=30000),
        comparison=ComparisonFrame(dimensions=["price_cents"]),
    )
    assert request.comparison.dimensions == ["price_cents"]


def test_constraint_names_map_to_product_field_names():
    """A caller bounds max_price_cents; the comparable field is price_cents."""
    assert CONSTRAINT_TO_ATTRIBUTE["max_price_cents"] == "price_cents"
    assert CONSTRAINT_TO_ATTRIBUTE["min_price_cents"] == "price_cents"
    assert CONSTRAINT_TO_ATTRIBUTE["max_wearing_weight_g"] == "wearing_weight_g"
    assert CONSTRAINT_TO_ATTRIBUTE["min_battery_hours"] == "battery_hours"
    assert CONSTRAINT_TO_ATTRIBUTE["anc_required"] == "anc"


def test_dimension_from_a_criterion_is_allowed():
    request = SearchRequest(
        preferences=PreferenceProfile(criteria=[
            Criterion(attribute="battery_hours", direction="higher")]),
        comparison=ComparisonFrame(dimensions=["battery_hours"]),
    )
    assert request.comparison.dimensions == ["battery_hours"]


def test_dimension_with_no_justification_is_refused():
    """Inventing an axis is indistinguishable from having misunderstood one."""
    with pytest.raises(ValidationError) as exc:
        SearchRequest(
            constraints=HardConstraints(max_price_cents=30000),
            comparison=ComparisonFrame(dimensions=["form_factor"]),
        )
    assert "not justified" in str(exc.value)


def test_default_constraints_justify_nothing():
    with pytest.raises(ValidationError):
        SearchRequest(comparison=ComparisonFrame(dimensions=["price_cents"]))


def test_anc_is_justified_when_required():
    request = SearchRequest(
        constraints=HardConstraints(anc_required=True),
        comparison=ComparisonFrame(dimensions=["anc"]),
    )
    assert request.comparison.dimensions == ["anc"]


def test_anc_is_not_justified_by_its_default():
    with pytest.raises(ValidationError):
        SearchRequest(comparison=ComparisonFrame(dimensions=["anc"]))


def test_defaults_are_usable_without_arguments():
    request = SearchRequest()
    assert request.limit == 3
    assert request.comparison.dimensions == []


def test_there_is_no_summary_flag():
    """Distribution statistics come from a separate endpoint.

    A flag plus two possible response shapes would leave the caller inferring
    which one it received from its own request history.
    """
    assert "summary_only" not in SearchRequest.model_fields


def test_the_two_answers_are_told_apart_by_a_field():
    response = make_response()
    assert response.kind == "candidates"

    summary = SummaryResponse(total_matches=0)
    assert summary.kind == "summary"

    from pydantic import TypeAdapter

    adapter = TypeAdapter(SearchResult)
    assert adapter.validate_python(response.model_dump()).kind == "candidates"
    assert adapter.validate_python(summary.model_dump()).kind == "summary"


def test_a_duplicated_dimension_is_refused():
    """The same column twice is a mistake, and a table that silently dedupes it
    hides the mistake."""
    with pytest.raises(ValidationError) as exc:
        SearchRequest(
            constraints=HardConstraints(max_price_cents=30000),
            comparison=ComparisonFrame(dimensions=["price_cents", "price_cents"]),
        )
    assert "repeats" in str(exc.value)


def test_limit_is_bounded():
    SearchRequest(limit=1)
    SearchRequest(limit=20)
    with pytest.raises(ValidationError):
        SearchRequest(limit=0)
    with pytest.raises(ValidationError):
        SearchRequest(limit=21)


# ---------------------------------------------------------------------------
# ComparisonCell and ComparisonRow
# ---------------------------------------------------------------------------

def test_unknown_is_reported_as_unknown_not_zero():
    unknown = ComparisonCell(value=None, known=False)
    assert unknown.known is False

    with pytest.raises(ValidationError):
        ComparisonCell(value=None, known=True)
    with pytest.raises(ValidationError):
        ComparisonCell(value=0, known=False)


def test_row_spread_needs_every_value_known():
    known = ComparisonRow(attribute="battery_hours", direction="higher", spread=10.0,
                          values=[ComparisonCell(value=35, known=True),
                                  ComparisonCell(value=25, known=True)])
    assert known.spread == 10.0

    with pytest.raises(ValidationError):
        ComparisonRow(attribute="battery_hours", direction="higher", spread=10.0,
                      values=[ComparisonCell(value=35, known=True),
                              ComparisonCell(value=None, known=False)])


def test_row_with_an_unknown_value_may_still_report_no_spread():
    row = ComparisonRow(attribute="battery_hours", direction="higher", spread=None,
                        values=[ComparisonCell(value=35, known=True),
                                ComparisonCell(value=None, known=False)])
    assert row.spread is None
    assert row.is_distinguishing is False


# ---------------------------------------------------------------------------
# SearchResponse: internal consistency
# ---------------------------------------------------------------------------

def test_returned_must_match_the_candidate_list():
    with pytest.raises(ValidationError):
        make_response(returned=2)


def test_returned_may_not_exceed_total_matches():
    """One candidate cannot come out of a set of zero."""
    with pytest.raises(ValidationError) as exc:
        make_response(total_matches=0, returned=1, candidates=[make_candidate()],
                      comparison=[], applied_criteria=[], relax_hints=RelaxHints())
    assert "total_matches" in str(exc.value) or "empty result" in str(exc.value)


def test_total_matches_may_exceed_the_returned_candidates():
    """Truncation is expected: 40 eligible, 3 shown."""
    response = make_response(total_matches=40, returned=1)
    assert response.truncated is True
    assert response.is_empty is False


def test_an_empty_result_must_carry_candidates_free_and_explain_itself():
    with pytest.raises(ValidationError):
        make_response(total_matches=0, returned=0, candidates=[],
                      applied_criteria=[], relax_hints=None)

    with pytest.raises(ValidationError):
        make_response(total_matches=0, returned=0, candidates=[make_candidate()],
                      relax_hints=RelaxHints())

    ok = make_response(total_matches=0, returned=0, candidates=[], comparison=[],
                       applied_criteria=[],
                       relax_hints=RelaxHints(hints=[RelaxationOption(
                           field="max_price_cents", from_value=30000, to_value=31000,
                           gained_count=1, example_product_id="hp_0007")]))
    assert ok.is_empty is True


def test_comparison_rows_must_align_with_candidates():
    with pytest.raises(ValidationError):
        make_response(comparison=[ComparisonRow(
            attribute="price_cents", direction="lower",
            values=[ComparisonCell(value=1, known=True),
                    ComparisonCell(value=2, known=True)])])

    ok = make_response(comparison=[ComparisonRow(
        attribute="price_cents", direction="lower",
        values=[ComparisonCell(value=29900, known=True)],
        spread=0.0, is_distinguishing=False, is_criterion=False)])
    assert ok.returned == 1


def test_response_is_immutable_and_rejects_unknown_fields():
    response = make_response()
    with pytest.raises(ValidationError):
        response.total_matches = 99
    with pytest.raises(ValidationError):
        SearchResponse(total_matches=0, returned=0, applied_constraints=HardConstraints(),
                       surprise=1)


def test_response_defaults_to_the_settlement_currency():
    assert make_response().currency == "HKD"


# ---------------------------------------------------------------------------
# Decision support on the response
# ---------------------------------------------------------------------------

def test_distinguishing_rows_are_the_ones_worth_showing():
    response = make_response(comparison=[
        ComparisonRow(attribute="price_cents", direction="lower",
                      values=[ComparisonCell(value=29900, known=True)],
                      spread=0.0, is_distinguishing=False, is_criterion=False),
        ComparisonRow(attribute="battery_hours", direction="higher",
                      values=[ComparisonCell(value=8, known=True)],
                      spread=0.0, is_distinguishing=True, is_criterion=True),
    ])
    assert [r.attribute for r in response.distinguishing_rows()] == ["battery_hours"]


def test_decision_dimensions_rank_user_relevant_first():
    """A dimension the user never mentioned but where candidates differ is often
    the real decision, so it outranks one where they are identical."""
    response = make_response(comparison=[
        ComparisonRow(attribute="price_cents", direction="lower",
                      values=[ComparisonCell(value=1, known=True)],
                      is_distinguishing=True, is_criterion=False),
        ComparisonRow(attribute="battery_hours", direction="higher",
                      values=[ComparisonCell(value=1, known=True)],
                      is_distinguishing=True, is_criterion=True),
        ComparisonRow(attribute="wearing_weight_g", direction="lower",
                      values=[ComparisonCell(value=1, known=True)],
                      is_distinguishing=False, is_criterion=True),
    ])
    ordered = [r.attribute for r in response.decision_dimensions()]
    assert ordered == ["battery_hours", "price_cents"]   # non-differentiating dropped


# ---------------------------------------------------------------------------
# GapReport and RelaxHints
# ---------------------------------------------------------------------------

def test_gap_report_knows_whether_it_has_anything_to_say():
    assert GapReport().has_gaps() is False
    assert GapReport(unsupported_criteria=["audio_quality"]).has_gaps() is True
    assert GapReport(dropped_criteria=["anc"]).has_gaps() is True
    assert GapReport(dropped_dimensions=["audio_quality"]).has_gaps() is True
    assert GapReport(missing_data_attributes=["battery_hours"]).has_gaps() is True
    assert GapReport(over_budget_alternatives=[
        OverBudgetAlternative(product_id="hp_0011", price_cents=31900, delta_cents=1900)
    ]).has_gaps() is True


def test_a_relaxation_option_must_actually_gain_something():
    """A hint that buys nothing is worse than no hint."""
    with pytest.raises(ValidationError):
        RelaxationOption(field="max_price_cents", from_value=30000, to_value=30000,
                         gained_count=0, example_product_id="hp_0007")


def test_every_near_miss_must_name_what_it_fails():
    """Without this, A cannot tell the user what is missing, and the near miss
    is indistinguishable from a clean match."""
    with pytest.raises(ValidationError):
        RelaxHints(closest_candidates=[make_candidate(violated_fields=[])])

    hints = RelaxHints(closest_candidates=[
        make_candidate("hp_0011", violated_fields=["max_price_cents"])])
    assert hints.closest_candidates[0].violated_fields == ["max_price_cents"]


def test_a_clean_candidate_reports_no_violations():
    assert make_candidate().is_clean is True
    assert make_candidate(violated_fields=["anc_required"]).is_clean is False


def test_relaxable_fields_exclude_changing_the_request():
    """Widening the category or dropping in-stock is a different request, not a
    concession, so it may never be offered as a relaxation."""
    assert set(RELAXABLE_FIELDS) == {
        "max_price_cents", "min_price_cents", "max_wearing_weight_g",
        "min_battery_hours", "anc_required",
    }
    for forbidden in ("category", "connection", "form_factor", "brand_allowlist",
                      "in_stock_only"):
        assert forbidden not in RELAXABLE_FIELDS


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

def test_matches_mean_satisfied():
    assert classify_outcome(make_response(total_matches=5, returned=1)) is SearchOutcome.SATISFIED


def test_no_matches_with_hints_means_the_user_gets_a_choice():
    response = make_response(total_matches=0, returned=0, candidates=[], comparison=[],
                             applied_criteria=[],
                             relax_hints=RelaxHints(hints=[RelaxationOption(
                                 field="max_price_cents", from_value=30000,
                                 to_value=31000, gained_count=1,
                                 example_product_id="hp_0007")]))
    assert classify_outcome(response) is SearchOutcome.NEEDS_RELAXATION


def test_no_matches_and_nothing_to_relax_means_no():
    response = make_response(total_matches=0, returned=0, candidates=[], comparison=[],
                             applied_criteria=[], relax_hints=RelaxHints())
    assert classify_outcome(response) is SearchOutcome.IMPOSSIBLE


# ---------------------------------------------------------------------------
# SummaryResponse
# ---------------------------------------------------------------------------

def test_summary_counts_must_reconcile():
    response = SummaryResponse(total_matches=40, dimension_stats={
        "battery_hours": DimensionStats(min=6, max=40, known_count=30, unknown_count=10)})
    assert response.total_matches == 40

    with pytest.raises(ValidationError):
        SummaryResponse(total_matches=40, dimension_stats={
            "battery_hours": DimensionStats(min=6, max=40, known_count=30, unknown_count=5)})


def test_summary_may_report_no_known_values():
    stats = DimensionStats(min=None, max=None, known_count=0, unknown_count=7)
    assert stats.min is None and stats.max is None


def test_natural_directions_cover_the_comparable_attributes():
    assert NATURAL_DIRECTION["price_cents"] == "lower"
    assert NATURAL_DIRECTION["battery_hours"] == "higher"
    assert NATURAL_DIRECTION["wearing_weight_g"] == "lower"
