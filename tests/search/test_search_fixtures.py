"""Activate original hard-filter fixtures against real D, with weighted v2 ranking.
The v1 fixture prose/default-field snapshots are superseded by the public v2 DTO.
"""
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.db import initialize_database
from app.search import SearchService
from app.contracts.product import HardConstraints
from app.contracts.search import SearchRequest, SearchResponse
ROOT = Path(__file__).resolve().parents[2]
CASES = sorted((ROOT / "fixtures").glob("search.*.json"))

@pytest.fixture
def search(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    initialize_database(path)
    return SearchService(db_path=path)

@pytest.mark.parametrize("path", CASES, ids=lambda p:p.stem)
def test_search_fixture(path, search):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if "expected_error_code" in fixture:
        with pytest.raises(ValidationError, match="INVALID_CONSTRAINTS"):
            SearchRequest.model_validate(fixture["request"])
        return
    request = SearchRequest.model_validate(fixture["request"])
    result = search.search(request)
    expected = fixture["expected"]
    assert isinstance(result, SearchResponse)
    assert result.total_matches == expected["total_matches"]
    assert result.returned == expected["returned"]
    assert result.applied_constraints == request.constraints
    eligible = {p.product_id for p in search._repository.list_candidates(request.constraints)}
    assert {c.product_id for c in result.candidates} <= eligible
    assert all(not c.violated_fields for c in result.candidates)
    assert result.gaps.model_dump() == expected["gaps"]
    if path.name == "search.001.normal.json":
        assert [c.product_id for c in result.candidates] == ["hp_0007", "hp_0018", "hp_0001"]
    if path.name == "search.003.anc_unknown.json":
        assert all(c.product.anc is True for c in result.candidates)
        assert [c.product.price_cents for c in result.candidates] == sorted(c.product.price_cents for c in result.candidates)
    rows = {r.attribute:r for r in result.comparison}
    for reference in expected["comparison"]:
        row = rows[reference["attribute"]]
        assert row.direction == reference["direction"]
        assert row.is_criterion == reference["is_criterion"]
        assert row.is_distinguishing == reference["is_distinguishing"]
        assert len(row.values) == result.returned
        assert all(cell.known == (cell.value is not None) for cell in row.values)
    if expected["relax_hints"] is None:
        assert result.relax_hints is None
    else:
        assert result.relax_hints is not None
        assert [h.model_dump() for h in result.relax_hints.hints] == expected["relax_hints"]["hints"]
        assert result.relax_hints.closest_candidates
        for candidate in result.relax_hints.closest_candidates:
            assert candidate.violated_fields
            assert candidate.product_id not in eligible
        assert result.candidates == []
        assert result.applied_constraints.max_wearing_weight_g == 5

def test_summary_and_search_apply_identical_hard_filters(search):
    request = SearchRequest(constraints=HardConstraints(max_price_cents=30000, connection="wireless", anc_required=True))
    summary = search.summarize(request)
    candidates = search.search(request)
    assert summary.total_matches == candidates.total_matches == 4
    assert summary.dimension_stats["price_cents"].min == 27900
    assert summary.dimension_stats["price_cents"].max == 30000
    assert summary.dimension_stats["battery_hours"].min == 8
    for stats in summary.dimension_stats.values():
        assert stats.known_count + stats.unknown_count == 4

def test_closer_to_uses_distance_not_raw_value(search):
    request = SearchRequest.model_validate({"constraints":{"anc_required":True,"connection":"wireless","max_price_cents":30000},
        "preferences":{"criteria":[{"attribute":"battery_hours","direction":"closer_to","target":8}]}})
    result = search.search(request)
    assert [c.product_id for c in result.candidates[:2]] == ["hp_0001","hp_0008"]
