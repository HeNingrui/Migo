"""Search contracts: what A asks B for, and what B must return.

The governing idea is that **A supplies the judgement, B supplies the facts**.
A knows the user: what matters to them, in what direction, and which dimensions
are worth putting side by side. B knows the catalog: it can filter, count and
compare, but it cannot know why the user cares.

That split is why the request carries two distinct kinds of instruction:

* ``constraints`` -- hard filters. Violating one eliminates a product.
* ``preferences`` -- a :class:`Criterion` list with a direction and a priority.
  These order the survivors and decide which dimensions are worth showing.

Everything in the response is derived from ``Product`` fields, so any figure B
reports can be traced back to a field rather than to B's opinion. Two rules make
that enforceable:

1. B never writes user-facing prose. It reports structured reasons; A renders
   them. A reason that cannot be traced to a field is not a reason.
2. Unknown is not a value. A ``null`` spec is reported as ``known: false`` and
   never coerced to ``0`` or ``False`` -- an unknown ``anc`` does not satisfy a
   requirement for active noise cancelling, and an unknown weight is not a
   light one.

Money remains integer minor units in the settlement currency. Currency is still
pinned to HKD; see the reserved seam in :mod:`app.contracts.common`.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import SETTLEMENT_CURRENCY
from .product import HardConstraints, Product, UseCase

# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

#: Dimensions a preference may be expressed over. These are ``Product`` field
#: names. Anything outside this set cannot be evaluated from catalog data, and
#: B must report it rather than guess.
CriterionAttribute = Literal[
    "price_cents",
    "battery_hours",
    "wearing_weight_g",
    "anc",
    "estimated_delivery_days",
]

Direction = Literal["higher", "lower", "closer_to"]
Priority = Literal["high", "medium", "low"]

#: The natural "better" direction of each attribute, used when a dimension is
#: compared because it came from a hard constraint rather than a criterion.
NATURAL_DIRECTION: dict[str, str] = {
    "price_cents": "lower",
    "wearing_weight_g": "lower",
    "battery_hours": "higher",
    "anc": "higher",
    "estimated_delivery_days": "lower",
}

#: Attributes whose values are numbers, so a spread can be computed.
NUMERIC_ATTRIBUTES: frozenset[str] = frozenset(
    {"price_cents", "battery_hours", "wearing_weight_g", "estimated_delivery_days"}
)

#: Maps a constraint field to the ``Product`` field it constrains.
#:
#: Needed because the two namespaces differ: a caller bounds ``max_price_cents``
#: while the comparable product field is ``price_cents``. Without this map,
#: "is this comparison dimension one the user had a reason to care about?" gets
#: the answer wrong for price, which is the dimension most often compared.
CONSTRAINT_TO_ATTRIBUTE: dict[str, str] = {
    "max_price_cents": "price_cents",
    "min_price_cents": "price_cents",
    "max_wearing_weight_g": "wearing_weight_g",
    "min_battery_hours": "battery_hours",
    "anc_required": "anc",
    "connection": "connection",
    "form_factor": "form_factor",
    "color": "color",
    "max_estimated_delivery_days": "estimated_delivery_days",
    "brand_allowlist": "brand",
}

PriorityPreset = Literal["best_match", "lower_price", "longer_battery", "lighter_weight"]


class Criterion(BaseModel):
    """One thing the user cares about, with a direction and a weight.

    ``evidence_quote`` is not decoration. It is what lets the agent answer "why
    is this ranked first?" from the user's own words instead of from a
    description invented afterwards.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute: str = Field(min_length=1)
    direction: Direction = "higher"
    target: float | None = None
    priority: Priority = "medium"
    source: Literal["explicit", "inferred"] = "explicit"
    evidence_quote: str | None = None

    @model_validator(mode="after")
    def _target_matches_direction(self) -> "Criterion":
        if self.direction == "closer_to":
            if self.target is None:
                raise ValueError("closer_to requires a target")
        elif self.target is not None:
            raise ValueError("target is only meaningful with direction=closer_to")
        return self


class PreferenceProfile(BaseModel):
    """What the user cares about, as opposed to what they require.

    ``criteria`` is ordered: earlier means more important. B may rely on that
    order, and must not re-sort it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    use_cases: list[UseCase] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    priority_preset: PriorityPreset = "best_match"

    #: A stated wish for noise cancelling that the user did not make a hard
    #: requirement. Defaults to false: a silent default of true would add five
    #: points to every candidate and so change nothing while looking meaningful.
    prefer_anc: bool = False

    @model_validator(mode="after")
    def _use_cases_unique(self) -> "PreferenceProfile":
        if len(set(self.use_cases)) != len(self.use_cases):
            raise ValueError("use_cases: duplicate entries")
        return self

    @model_validator(mode="after")
    def _criteria_are_unambiguous(self) -> "PreferenceProfile":
        seen: set[str] = set()
        for criterion in self.criteria:
            if criterion.attribute in seen:
                raise ValueError(
                    f"criteria names {criterion.attribute!r} more than once; "
                    "a dimension has one direction and one weight"
                )
            seen.add(criterion.attribute)
        return self

    def attributes(self) -> list[str]:
        return [c.attribute for c in self.criteria]

    def is_empty(self) -> bool:
        return not self.use_cases and not self.criteria


class ComparisonFrame(BaseModel):
    """Which dimensions A suggests worth putting side by side.

    A proposes; B may veto. B drops any dimension that is not a ``Product``
    field and reports it in :class:`GapReport`, so A learns its own mistake
    instead of silently getting a different table than it asked for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimensions: list[str] = Field(default_factory=list)
    max_columns: int = Field(default=3, ge=2, le=5)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """Everything B needs, and nothing it would have to guess."""

    model_config = ConfigDict(extra="forbid")

    constraints: HardConstraints = Field(default_factory=HardConstraints)
    preferences: PreferenceProfile = Field(default_factory=PreferenceProfile)
    comparison: ComparisonFrame = Field(default_factory=ComparisonFrame)

    #: How many candidates to return. Affects only ``candidates``; the totals
    #: and the relaxation hints always describe the whole eligible set.
    limit: int = Field(default=3, ge=1, le=20)

    # There is deliberately no ``summary_only`` flag. Distribution statistics
    # come from a separate endpoint, so the shape of a ``search`` response is
    # never conditional: a flag plus two possible response types would leave the
    # caller inferring which one it received from its own request history.

    @model_validator(mode="after")
    def _comparison_is_well_formed(self) -> "SearchRequest":
        """Reject a comparison frame that cannot describe a real table.

        Two checks, both about A's own consistency rather than B's work:

        * no duplicate dimension -- the same column twice is a mistake, and a
          table that silently dedupes it hides the mistake;
        * every dimension must be justified. A may only ask to compare a field
          the user constrained or a criterion they expressed. Mapping goes
          through :data:`CONSTRAINT_TO_ATTRIBUTE`, since a caller bounds
          ``max_price_cents`` while the comparable field is ``price_cents``.
          Inventing an axis is indistinguishable from having misunderstood one.
        """
        dimensions = self.comparison.dimensions
        duplicates = sorted({d for d in dimensions if dimensions.count(d) > 1})
        if duplicates:
            raise ValueError(f"comparison.dimensions repeats: {duplicates}")

        justified: set[str] = set()
        for name in self.constraints.explicitly_set_fields():
            justified.add(CONSTRAINT_TO_ATTRIBUTE.get(name, name))
        if self.constraints.anc_required:
            justified.add("anc")
        justified |= set(self.preferences.attributes())
        preset_attribute = {"lower_price": "price_cents", "longer_battery": "battery_hours", "lighter_weight": "wearing_weight_g"}.get(self.preferences.priority_preset)
        if preset_attribute:
            justified.add(preset_attribute)

        unjustified = [d for d in dimensions if d not in justified]
        if unjustified:
            raise ValueError(
                "comparison dimensions not justified by any constraint or "
                f"criterion the user expressed: {unjustified}"
            )
        return self


# ---------------------------------------------------------------------------
# Response pieces
# ---------------------------------------------------------------------------

class MatchReason(BaseModel):
    """One reason a candidate qualifies, traceable to a product field.

    ``field`` and ``observed`` are the traceability guarantee: a reason that
    cannot name where it came from is not a reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    observed: Any = None
    text: str = Field(min_length=1)
    kind: Literal["constraint", "preference", "spec"] = "spec"


class Candidate(BaseModel):
    """One eligible product, ranked and explained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: Product
    match_score: int = Field(ge=0)
    feature_score: float | None = Field(default=None, ge=0, le=1)
    price_score: float | None = Field(default=None, ge=0, le=1)
    review_score: float | None = Field(default=None, ge=0, le=1)
    composite_score: float | None = Field(default=None, ge=0, le=1)
    review_score_source: str | None = None
    review_summary: dict[str, Any] = Field(default_factory=dict)
    reasons: list[MatchReason] = Field(default_factory=list)
    preference_misses: list[str] = Field(default_factory=list)

    #: Hard constraints this product violates. Always empty in ``candidates``,
    #: and non-empty only in ``closest_candidates`` -- which is how A can tell
    #: the user exactly what a near miss fails on.
    violated_fields: list[str] = Field(default_factory=list)

    @property
    def product_id(self) -> str:
        return self.product.product_id

    @property
    def is_clean(self) -> bool:
        return not self.violated_fields


class ComparisonCell(BaseModel):
    """One product's value on one dimension."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | bool | str | None = None
    known: bool = False

    @model_validator(mode="after")
    def _known_agrees_with_value(self) -> "ComparisonCell":
        if self.known != (self.value is not None):
            raise ValueError("known must be true exactly when value is not null")
        return self


class ComparisonRow(BaseModel):
    """One dimension across the returned candidates, in the same order.

    ``is_distinguishing`` is the part that makes a comparison useful. A row where
    every candidate is identical is noise, and a dimension A never mentioned but
    where the candidates genuinely differ is often the real decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute: str
    direction: Direction
    values: list[ComparisonCell]

    #: Range across candidates. ``null`` when any value is unknown, because a
    #: range over a value that was never measured is not a range.
    spread: float | None = None

    is_distinguishing: bool = False
    is_criterion: bool = False

    @model_validator(mode="after")
    def _spread_requires_every_value(self) -> "ComparisonRow":
        if self.spread is not None and any(not c.known for c in self.values):
            raise ValueError("spread must be null when any value is unknown")
        return self


class OverBudgetAlternative(BaseModel):
    """A product that fits every constraint except the price cap.

    Reported so A can offer a specific, honest alternative -- naming the exact
    overage -- instead of quietly widening the budget, which the user did not
    agree to.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    price_cents: int = Field(ge=0)
    delta_cents: int = Field(ge=0)


class RelaxationOption(BaseModel):
    """What relaxing one dimension on its own would buy.

    Every other constraint stays frozen. Relaxing two dimensions at once would
    produce a number that answers no question the user asked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    from_value: Any = None
    to_value: Any = None
    gained_count: int = Field(gt=0)
    example_product_id: str


class RelaxHints(BaseModel):
    """Present only when nothing satisfies the constraints as given.

    B reports; B does not relax. A decides which of these to put to the user,
    and only the user's agreement changes the search.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hints: list[RelaxationOption] = Field(default_factory=list)
    closest_candidates: list[Candidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_near_miss_says_what_it_fails(self) -> "RelaxHints":
        for candidate in self.closest_candidates:
            if not candidate.violated_fields:
                raise ValueError(
                    "a closest candidate must report the constraints it violates; "
                    "without that, A cannot tell the user what is missing"
                )
        return self


class GapReport(BaseModel):
    """What B could not do, reported rather than swallowed.

    A system that only reports what it did is not being transparent. This is the
    record of the rest: criteria it could not evaluate, dimensions it refused,
    data that is missing, and alternatives just out of range.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Criteria the user expressed that catalog data cannot answer, such as
    #: sound quality. Reported so A can say so instead of implying it was met.
    unsupported_criteria: list[str] = Field(default_factory=list)

    #: Criteria that were accepted but played no part in ordering. A non-empty
    #: value here means A and B disagree about the contract.
    dropped_criteria: list[str] = Field(default_factory=list)

    #: Requested comparison dimensions B refused because they are not product
    #: fields.
    dropped_dimensions: list[str] = Field(default_factory=list)

    #: Comparison dimensions where at least one returned candidate has no value.
    missing_data_attributes: list[str] = Field(default_factory=list)

    over_budget_alternatives: list[OverBudgetAlternative] = Field(default_factory=list)

    def has_gaps(self) -> bool:
        return any((
            self.unsupported_criteria,
            self.dropped_criteria,
            self.dropped_dimensions,
            self.missing_data_attributes,
            self.over_budget_alternatives,
        ))


class DimensionStats(BaseModel):
    """Distribution of one numeric dimension across the eligible set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min: float | None = None
    max: float | None = None
    known_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)


class SummaryResponse(BaseModel):
    """The answer to a distribution query: what is on the table, before asking.

    This exists so A can ask a question grounded in the catalog. Asking "how much
    battery do you want?" before knowing whether any long-life option exists
    wastes the user's turn.

    ``kind`` makes the type self-identifying so a caller holding either response
    can tell which one it has without remembering which endpoint it called. That
    is what allows :data:`SearchResult` to be a discriminated union rather than a
    guess.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["summary"] = "summary"

    total_matches: int = Field(ge=0)
    dimension_stats: dict[str, DimensionStats] = Field(default_factory=dict)
    boolean_stats: dict[str, dict[str, int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts_are_coherent(self) -> "SummaryResponse":
        for attribute, stats in self.dimension_stats.items():
            if stats.known_count + stats.unknown_count != self.total_matches:
                raise ValueError(
                    f"{attribute}: known_count + unknown_count must equal total_matches"
                )
        return self


# ---------------------------------------------------------------------------
# The two possible answers, told apart by a field rather than by memory
# ---------------------------------------------------------------------------

class SearchResponse(BaseModel):
    """B's answer to a candidate search.

    ``total_matches`` counts every eligible product, before ``limit`` truncates
    ``candidates``. Collapsing the two is how a caller ends up saying "found 3"
    when there were 40.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["candidates"] = "candidates"

    total_matches: int = Field(ge=0)
    returned: int = Field(ge=0)

    applied_constraints: HardConstraints
    applied_criteria: list[str] = Field(default_factory=list)

    candidates: list[Candidate] = Field(default_factory=list)
    comparison: list[ComparisonRow] = Field(default_factory=list)
    gaps: GapReport = Field(default_factory=GapReport)
    relax_hints: RelaxHints | None = None
    suggestions: list[str] = Field(default_factory=list)

    currency: str = SETTLEMENT_CURRENCY
    score_weights: dict[str, float] = Field(default_factory=lambda: {"feature": 0.5, "price": 0.3, "review": 0.2})
    ranking_policy: str = "preferences_weighted_v2"

    @model_validator(mode="after")
    def _counts_agree_with_the_payload(self) -> "SearchResponse":
        if self.returned != len(self.candidates):
            raise ValueError("returned must equal the number of candidates")
        if self.returned > self.total_matches:
            raise ValueError("returned must not exceed total_matches")
        if self.total_matches == 0 and self.candidates:
            raise ValueError("an empty result must not carry candidates")
        if self.total_matches == 0 and self.relax_hints is None:
            raise ValueError("an empty result must report why nothing matched")
        for row in self.comparison:
            if len(row.values) != self.returned:
                raise ValueError(
                    f"comparison row {row.attribute!r} must align with candidates"
                )
        return self

    @property
    def is_empty(self) -> bool:
        return self.total_matches == 0

    @property
    def truncated(self) -> bool:
        return self.returned < self.total_matches

    def distinguishing_rows(self) -> list[ComparisonRow]:
        """Dimensions where the candidates genuinely differ."""
        return [r for r in self.comparison if r.is_distinguishing]

    def decision_dimensions(self) -> list[ComparisonRow]:
        """The dimensions most worth the user's attention.

        User-relevant and differentiating first, then merely differentiating --
        the ones the user did not mention but which actually separate the
        shortlist, and are therefore where the decision is really being made.
        """
        return sorted(
            self.distinguishing_rows(),
            key=lambda r: (not r.is_criterion, self.comparison.index(r)),
        )


# ---------------------------------------------------------------------------
# Classifying the outcome
# ---------------------------------------------------------------------------

class SearchOutcome(str, Enum):
    """What A should do with a response.

    Three states, not two. An empty result that could be widened and one that
    cannot are different conversations: the first offers the user a choice, the
    second has to say no.
    """

    SATISFIED = "SATISFIED"
    NEEDS_RELAXATION = "NEEDS_RELAXATION"
    IMPOSSIBLE = "IMPOSSIBLE"


#: Either answer B can give. Discriminated on ``kind``, so a caller holding a
#: response can tell which one it is from the payload alone. Without that, A
#: would have to remember which endpoint it called -- and would get it wrong the
#: first time a retry or a cache served the other one.
SearchResult = Annotated[SearchResponse | SummaryResponse, Field(discriminator="kind")]


def classify_outcome(response: SearchResponse) -> SearchOutcome:
    """Decide which conversation to have. Pure, no I/O."""
    if response.total_matches > 0:
        return SearchOutcome.SATISFIED
    if response.relax_hints and response.relax_hints.hints:
        return SearchOutcome.NEEDS_RELAXATION
    return SearchOutcome.IMPOSSIBLE


# Fields a relaxation hint may be offered for. Relaxing anything else is not a
# concession, it is a different request.
RELAXABLE_FIELDS: tuple[str, ...] = (
    "max_price_cents",
    "min_price_cents",
    "max_wearing_weight_g",
    "min_battery_hours",
    "anc_required",
)


__all__ = [
    "CONSTRAINT_TO_ATTRIBUTE",
    "Candidate",
    "ComparisonCell",
    "ComparisonFrame",
    "ComparisonRow",
    "Criterion",
    "CriterionAttribute",
    "DimensionStats",
    "Direction",
    "GapReport",
    "MatchReason",
    "NATURAL_DIRECTION",
    "NUMERIC_ATTRIBUTES",
    "OverBudgetAlternative",
    "PreferenceProfile",
    "Priority",
    "PriorityPreset",
    "RELAXABLE_FIELDS",
    "RelaxHints",
    "RelaxationOption",
    "SearchOutcome",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SummaryResponse",
    "classify_outcome",
]
