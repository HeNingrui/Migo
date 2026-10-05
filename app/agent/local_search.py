"""The stand-in for B, kept apart from the contract it stands in for.

**Delete this module when ``app/search/`` lands.** It exists so the conversation
can be built and demoed before B is written, and it is deliberately thin: it does
the two things A cannot do without -- apply the hard constraints (by delegating
to D's repository, which is real code) and order the result. It does not
implement five-level ranking, distinguishing-dimension analysis or relaxation
semantics beyond "what would dropping this one constraint admit"; those are B's,
and a half-copy of them here would drift.

It lives in its own file rather than beside the commerce stand-in so that the
thing to remove is obvious.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.catalog.models import DataValidationError
from app.contracts.product import HardConstraints, Product
from app.contracts.search import (
    Candidate,
    ComparisonCell,
    ComparisonRow,
    GapReport,
    MatchReason,
    SearchRequest,
    SearchResponse,
)

#: Fields a caller may ask to compare, mapped to the product attribute that
#: actually holds the value. Mirrors ``app/contracts/search.py``; kept here so
#: the stand-in does not import B's private tables.
_COMPARABLE = {
    "price_cents": "price_cents",
    "battery_hours": "battery_hours",
    "wearing_weight_g": "wearing_weight_g",
    "anc": "anc",
    "connection": "connection",
    "form_factor": "form_factor",
}

#: What "drop this constraint" means, per field. ``None`` removes a bound; a
#: flag becomes ``False``; a list becomes empty. Every relaxable field must
#: appear here -- a missing entry sends ``None`` to a field that rejects it, and
#: the resulting error used to be swallowed, silently removing that relaxation
#: from every answer.
_RELAXED_VALUE: dict[str, object] = {
    "max_price_cents": None,
    "min_price_cents": None,
    "max_wearing_weight_g": None,
    "min_battery_hours": None,
    "brand_allowlist": [],
    "connection": None,
    "form_factor": None,
    "anc_required": False,
    "in_stock_only": False,
    "color": None, "required_device": None, "tags": [], "max_estimated_delivery_days": None,
}

#: No filtering at all, used to enumerate the near misses.
_UNFILTERED = HardConstraints(in_stock_only=False)

#: A relaxation that cannot be expressed. Two sources, because the constraints
#: pass through two validators: ours (Pydantic) on the way in, and D's own on the
#: way to the repository. Catching only the first let a real defect hide -- the
#: missing ``anc_required`` entry below meant "relax the ANC requirement" was
#: silently never offered.
_CONSTRAINT_ERRORS: tuple[type[BaseException], ...] = (
    ValidationError,
    DataValidationError,
)


def _relaxable_fields(constraints: HardConstraints) -> list[str]:
    """Constraints worth trying to relax, in a stable order.

    ``explicitly_set_fields()`` deliberately omits the flags whose default is
    already the common case, because they are not things the user "asked for".
    They are still things that can be *doing the excluding*, so a demand for ANC
    and the stock filter are added back here by hand.
    """
    fields = list(constraints.explicitly_set_fields())
    if constraints.anc_required:
        fields.append("anc_required")
    if constraints.in_stock_only:
        fields.append("in_stock_only")
    return sorted(set(fields))

class LocalSearchClient:
    """A stand-in for B, so the conversation can be built and demoed first.

    **This is scaffolding, not B's module.** It does the two things A cannot do
    without: apply the hard constraints (by delegating to D's repository, which
    is real code) and order the result. It deliberately does *not* implement
    five-level ranking, distinguishing-dimension analysis or relaxation hints --
    those are B's, and a half-copy of them here would drift.

    When ``app/search/`` exists, delete this class.
    """

    def __init__(self, *, product_model: type[Product] = Product, db_path=None) -> None:
        from app.catalog import ProductRepository

        self._repository = ProductRepository(db_path, product_model=product_model)

    def search(self, request: SearchRequest) -> SearchResponse:
        eligible = self._eligible(request)
        ranked = self._rank(eligible, request)
        candidates = ranked[: request.limit]

        return SearchResponse(
            total_matches=len(eligible),
            returned=len(candidates),
            applied_constraints=request.constraints,
            applied_criteria=request.preferences.attributes(),
            candidates=candidates,
            comparison=self._comparison(candidates, request),
            gaps=self._gaps(eligible, request),
            # The contract refuses an empty result with no explanation: "an empty
            # result must report why nothing matched". A system that only says
            # "nothing found" leaves the user guessing which of their conditions
            # was the impossible one.
            relax_hints=self._relax_hints(request) if not eligible else None,
            currency="HKD",
        )

    # -- steps --------------------------------------------------------------

    def _eligible(self, request: SearchRequest) -> list[Product]:
        """Hard constraints only. D's repository does the filtering."""
        return list(self._repository.list_candidates(request.constraints))

    def _relax_hints(self, request: SearchRequest):
        """Which single constraint is doing the excluding, and by how much.

        Computed **by asking D's repository**, not by re-implementing its
        filter: each constraint is dropped on its own and the result compared.
        A second copy of the matching rules here would drift from the first, and
        the drift would show up as a relaxation hint that does not work.

        One field at a time, because relaxing two at once produces a number that
        answers no question the user asked.
        """
        from app.contracts.search import RelaxHints, RelaxationOption

        hints: list[RelaxationOption] = []
        base = request.constraints

        for field in _relaxable_fields(base):
            relaxed = base.model_copy(update={field: _RELAXED_VALUE.get(field)})
            try:
                gained = list(self._repository.list_candidates(relaxed))
            except _CONSTRAINT_ERRORS:
                # Some relaxations are not representable -- dropping a lower
                # bound while keeping an upper one, for instance. That is not a
                # hint, but it is also not an error. A repository failure is a
                # different thing entirely and is deliberately not caught: it
                # would make a broken database look like "nothing to relax".
                continue
            if not gained:
                continue

            hints.append(RelaxationOption(
                field=field,
                from_value=getattr(base, field, None),
                to_value=self._minimal_value(field, gained),
                gained_count=len(gained),
                example_product_id=gained[0].product_id,
            ))

        return RelaxHints(
            hints=sorted(hints, key=lambda h: (-h.gained_count, h.field)),
            closest_candidates=self._closest(request),
        )

    @staticmethod
    def _minimal_value(field: str, gained: list[Product]):
        """The smallest change that admits something, not the largest."""
        if field == "max_price_cents":
            return min(p.price_cents for p in gained)
        if field == "max_wearing_weight_g":
            weights = [p.wearing_weight_g for p in gained if p.wearing_weight_g is not None]
            return min(weights) if weights else None
        if field == "min_battery_hours":
            batteries = [p.battery_hours for p in gained if p.battery_hours is not None]
            return max(batteries) if batteries else None
        if field == "min_price_cents":
            return max(p.price_cents for p in gained)
        return None  # an enum or a flag: there is no "nearly" value to report

    def _closest(self, request: SearchRequest) -> list[Candidate]:
        """The near misses, each saying which constraints it breaks.

        Determined by asking the repository one constraint at a time, so the
        answer is D's own notion of a violation rather than a parallel one.
        """
        base = request.constraints
        everything = list(self._repository.list_candidates(_UNFILTERED))
        eligible_ids = {p.product_id for p in self._eligible(request)}

        near: list[tuple[int, Candidate]] = []
        for product in everything:
            if product.product_id in eligible_ids:
                continue
            findings = [
                (field, self._survives(product.product_id, base, field))
                for field in _relaxable_fields(base)
            ]
            if any(answer is None for _, answer in findings):
                # One unanswerable constraint makes the whole near-miss
                # unreliable, and the contract requires every entry to state
                # what it violates. Omit it rather than state something false.
                continue
            violated = [field for field, answer in findings if answer is False]
            if not violated:
                continue
            near.append((len(violated), Candidate(
                product=product, match_score=0, reasons=[],
                preference_misses=[], violated_fields=violated,
            )))

        near.sort(key=lambda pair: (pair[0], pair[1].product_id))
        return [candidate for _, candidate in near[:3]]

    def _survives(self, product_id: str, base: HardConstraints, field: str) -> bool | None:
        """Whether the product passes when only ``field`` is dropped.

        ``None`` when the question cannot be answered. Guessing either way would
        be worse than saying nothing: reporting "survives" puts a product into
        the near-miss list with nothing wrong with it, and reporting "violated"
        names a constraint it does not break.
        """
        update = {field: _RELAXED_VALUE[field]}
        try:
            relaxed = base.model_copy(update=update)
            products = self._repository.list_candidates(relaxed)
        except _CONSTRAINT_ERRORS:
            return None
        return any(p.product_id == product_id for p in products)

    def _rank(
        self, products: list[Product], request: SearchRequest
    ) -> list[Candidate]:
        criteria = request.preferences.criteria
        use_cases = set(request.preferences.use_cases)

        def key(product: Product) -> tuple:
            # Criteria first, in the order the user gave them -- earlier means
            # more important, and B's spec says the order is not to be re-sorted.
            preference: list[float] = []
            for criterion in criteria:
                value = getattr(product, criterion.attribute, None)
                if value is None:
                    # Unknown sorts last, never as zero: an unmeasured battery
                    # is not an empty one.
                    preference.append(float("-inf") if criterion.direction == "higher"
                                       else float("inf"))
                else:
                    preference.append(-value if criterion.direction == "higher" else value)
            # Then price, then id, so the order is total and reproducible.
            return (*preference, product.price_cents, product.product_id)

        ordered = sorted(products, key=key)
        return [self._candidate(p, request, use_cases) for p in ordered]

    def _candidate(
        self, product: Product, request: SearchRequest, use_cases: set[str]
    ) -> Candidate:
        # The score formula is B's, taken from the handoff spec rather than
        # invented: it counts label hits and is often flat across candidates.
        hits = len(use_cases & set(product.use_cases))
        score = 10 * hits
        if request.preferences.prefer_anc and product.anc is True:
            score += 5

        reasons = [
            MatchReason(
                field="use_cases", observed=sorted(use_cases & set(product.use_cases)),
                text=f"matches {hits} of the requested uses", kind="preference",
            )
        ]
        if product.anc is True:
            reasons.append(MatchReason(
                field="anc", observed=True, text="active noise cancelling", kind="spec",
            ))

        misses = [
            c.attribute for c in request.preferences.criteria
            if getattr(product, c.attribute, None) is None
        ]
        return Candidate(
            product=product, match_score=score, reasons=reasons,
            preference_misses=misses, violated_fields=[],
        )

    def _comparison(
        self, candidates: list[Candidate], request: SearchRequest
    ) -> list[ComparisonRow]:
        rows: list[ComparisonRow] = []
        asked = request.comparison.dimensions
        for attribute in asked:
            target = _COMPARABLE.get(attribute)
            if target is None:
                continue  # not a product field; B reports it, the stand-in drops it
            cells = [
                ComparisonCell(value=(v := getattr(c.product, target, None)),
                               known=v is not None)
                for c in candidates
            ]
            known_values = [c.value for c in cells if c.known]
            spread = None
            if len(known_values) == len(cells) and known_values:
                numeric = [float(v) for v in known_values if isinstance(v, (int, float))]
                if len(numeric) == len(known_values):
                    spread = max(numeric) - min(numeric)
            rows.append(ComparisonRow(
                attribute=attribute,
                direction="lower" if attribute in ("price_cents", "wearing_weight_g") else "higher",
                values=cells, spread=spread,
                is_distinguishing=bool(spread),
                is_criterion=attribute in request.preferences.attributes(),
            ))
        return rows

    def _gaps(self, eligible: list[Product], request: SearchRequest) -> GapReport:
        missing = [
            c.attribute for c in request.preferences.criteria
            if all(getattr(p, c.attribute, None) is None for p in eligible)
        ] if eligible else []
        return GapReport(missing_data_attributes=missing)
