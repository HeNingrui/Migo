"""Formal B boundary: D filters live SQL data; B ranks and explains it.

Uses the team's proven conversational response helpers and the independent B
prototype's deterministic scoring functions. No separate purchase catalog.
"""
from app.agent.local_search import LocalSearchClient
from app.catalog.reviews import ReviewRepository
from app.contracts.product import HardConstraints, Product
from app.contracts.search import (SearchResponse, ComparisonCell, ComparisonRow, DimensionStats,
    GapReport, MatchReason, OverBudgetAlternative, RelaxHints, RelaxationOption,
    SummaryResponse)
from .policy import _ranking_key, _score_products, CRITERION_FIELDS
from .review_analysis import analyze_reviews

COMPARABLE = {"price_cents", "battery_hours", "wearing_weight_g", "anc",
              "connection", "form_factor", "color", "estimated_delivery_days"}
RELAXED = {"min_price_cents": None, "max_price_cents": None, "brand_allowlist": [],
           "connection": None, "form_factor": None, "anc_required": False,
           "min_battery_hours": None, "max_wearing_weight_g": None,
           "in_stock_only": False, "color": None, "required_device": None,
           "tags": [], "max_estimated_delivery_days": None}


class SearchService(LocalSearchClient):
    def __init__(self, *, db_path=None, repository=None, reviews=None):
        super().__init__(db_path=db_path)
        if repository is not None:
            self._repository = repository
        self._reviews = reviews or ReviewRepository(db_path)

    def analyze(self, product_id):
        return analyze_reviews(product_id, self._reviews.list_reviews(product_id))

    def search_page(self, request, *, exclude_product_ids=()):
        """Rank the entire eligible catalog before excluding displayed products."""
        eligible = self._eligible(request)
        ranked = self._rank(eligible, request)
        excluded = set(exclude_product_ids)
        candidates = [c for c in ranked if c.product_id not in excluded][:request.limit]
        return SearchResponse(total_matches=len(eligible), returned=len(candidates),
            applied_constraints=request.constraints,
            applied_criteria=request.preferences.attributes(), candidates=candidates,
            comparison=self._comparison(candidates, request), gaps=self._gaps(eligible, request),
            relax_hints=self._relax_hints(request) if not eligible else None,
            currency="HKD")

    def review_context(self, product_id):
        """Read-only public evidence for A; never includes evaluation labels."""
        rows = self._reviews.list_reviews(product_id)
        analysis = analyze_reviews(product_id, rows)
        findings = {f['review_id']: f for f in analysis['findings']}
        flagged = [r for r in rows if findings[r['review_id']]['assessment'] == 'potentially_coordinated']
        ordinary = [r for r in rows if r not in flagged]
        # Include different sentiments plus possible repeated hype, rather than
        # feeding only positive or highest-rated reviews to the model.
        selected = []
        if ordinary:
            selected.extend([min(ordinary, key=lambda r:r['rating']), max(ordinary, key=lambda r:r['rating'])])
        selected.extend(flagged[:2])
        selected.extend(rows)
        evidence = {}
        for r in selected:
            if r['review_id'] in evidence:
                continue
            evidence[r['review_id']] = {k:r[k] for k in ('review_id', 'rating', 'title', 'text')}
            evidence[r['review_id']]['finding'] = findings[r['review_id']]
            if len(evidence) == 4:
                break
        return {"synthetic": True, "analysis": analysis, "sample_reviews": list(evidence.values())}

    def _rank(self, products, request):
        raw = [p.model_dump(mode="json") for p in products]
        analyses = {p.product_id: self.analyze(p.product_id) for p in products}
        review_scores = {pid: a["review_score"] for pid, a in analyses.items()}
        preferences = request.preferences.model_dump(mode="json")
        scores = _score_products(raw, preferences, review_scores)
        ordered = sorted(raw, key=lambda p: _ranking_key(p, preferences, scores[p["product_id"]], raw))
        result = []
        for data in ordered:
            p = Product.model_validate(data)
            candidate = self._candidate(p, request, set(request.preferences.use_cases))
            score = scores[p.product_id]
            analysis = analyses[p.product_id]
            reasons = list(candidate.reasons)
            for field in ("color", "price_cents", "battery_hours", "wearing_weight_g", "estimated_delivery_days"):
                value = getattr(p, field)
                if value is not None:
                    reasons.append(MatchReason(field=field, observed=value, text=f"{field}: {value}", kind="spec"))
            summary = {k: analysis[k] for k in ("review_count", "average_rating", "rating_scale", "suspicious_count")}
            result.append(candidate.model_copy(update={
                "feature_score": round(score["feature"], 4), "price_score": round(score["price"], 4),
                "review_score": score["review"], "composite_score": round(score["composite"], 4),
                "review_score_source": analysis["score_source"], "review_summary": summary,
                "reasons": reasons}))
        return result

    def _comparison(self, candidates, request):
        if not candidates:
            return []
        criteria = {c.attribute: c for c in request.preferences.criteria}
        rows = []
        for attribute in request.comparison.dimensions:
            if attribute not in COMPARABLE:
                continue
            cells = [ComparisonCell(value=getattr(c.product, attribute), known=getattr(c.product, attribute) is not None) for c in candidates]
            values = [c.value for c in cells if c.known]
            complete = len(values) == len(cells)
            numeric = values and all(type(v) in (int, float) for v in values)
            spread = max(values)-min(values) if complete and numeric else None
            direction = criteria[attribute].direction if attribute in criteria else "lower" if attribute in {"price_cents","wearing_weight_g","estimated_delivery_days"} else "higher"
            rows.append(ComparisonRow(attribute=attribute, direction=direction, values=cells,
                spread=spread, is_distinguishing=complete and len(set(values)) > 1,
                is_criterion=attribute in criteria))
        rows.sort(key=lambda r: (not (r.is_criterion and r.is_distinguishing), not r.is_distinguishing))
        return rows[:request.comparison.max_columns]

    def _gaps(self, eligible, request):
        unsupported = [c.attribute for c in request.preferences.criteria if c.attribute not in CRITERION_FIELDS]
        dimensions = [d for d in request.comparison.dimensions if d not in COMPARABLE]
        missing = [d for d in set(request.comparison.dimensions + request.preferences.attributes())
                   if d in COMPARABLE and any(getattr(p,d) is None for p in eligible)]
        alternatives = []
        cap = request.constraints.max_price_cents
        if cap is not None:
            relaxed = request.constraints.model_copy(update={"max_price_cents": None})
            products = self._repository.list_candidates(relaxed)
            alternatives = [OverBudgetAlternative(product_id=p.product_id, price_cents=p.price_cents,
                delta_cents=p.price_cents-cap) for p in sorted(products, key=lambda p:(p.price_cents,p.product_id)) if p.price_cents > cap]
        return GapReport(unsupported_criteria=unsupported, dropped_criteria=unsupported,
            dropped_dimensions=dimensions, missing_data_attributes=sorted(missing),
            over_budget_alternatives=alternatives)

    def _relax_hints(self, request):
        constraints = request.constraints
        current = constraints.model_dump(mode="python")
        active = [f for f, value in RELAXED.items() if current[f] != value]
        hints = []
        for field in active:
            relaxed = constraints.model_copy(update={field: RELAXED[field]})
            gained = self._repository.list_candidates(relaxed)
            if not gained:
                continue
            target = RELAXED[field]
            mapped = {"max_price_cents": ("price_cents", min), "min_price_cents": ("price_cents", max),
                "min_battery_hours": ("battery_hours", max), "max_wearing_weight_g": ("wearing_weight_g", min),
                "max_estimated_delivery_days": ("estimated_delivery_days", min)}
            if field in mapped:
                attr, choose = mapped[field]
                known = [getattr(p,attr) for p in gained if getattr(p,attr) is not None]
                if not known:
                    continue
                target = choose(known)
                gained = self._repository.list_candidates(constraints.model_copy(update={field: target}))
            hints.append(RelaxationOption(field=field, from_value=current[field], to_value=target,
                gained_count=len(gained), example_product_id=gained[0].product_id))
        # Ask D about each condition separately; unknown hard specs still fail.
        everything = self._repository.list_candidates(HardConstraints(in_stock_only=False))
        passes = {field: {p.product_id for p in self._repository.list_candidates(
            HardConstraints.model_validate({"in_stock_only": False, field: current[field]}))} for field in active}
        near = []
        for p in everything:
            violated = [field for field in active if p.product_id not in passes[field]]
            if violated:
                c = self._candidate(p, request, set(request.preferences.use_cases))
                near.append(c.model_copy(update={"violated_fields": violated}))
        near.sort(key=lambda c: (len(c.violated_fields), -c.match_score, c.product.price_cents, c.product_id))
        return RelaxHints(hints=sorted(hints,key=lambda h:(-h.gained_count,h.field)), closest_candidates=near[:3])

    def summarize(self, request):
        eligible = self._eligible(request)
        stats = {}
        for field in ("price_cents", "battery_hours", "wearing_weight_g", "estimated_delivery_days"):
            known = [getattr(p,field) for p in eligible if getattr(p,field) is not None]
            stats[field] = DimensionStats(min=min(known) if known else None, max=max(known) if known else None,
                known_count=len(known), unknown_count=len(eligible)-len(known))
        return SummaryResponse(total_matches=len(eligible), dimension_stats=stats,
            boolean_stats={"anc": {"true": sum(p.anc is True for p in eligible),
                "false": sum(p.anc is False for p in eligible), "unknown": sum(p.anc is None for p in eligible)}})
