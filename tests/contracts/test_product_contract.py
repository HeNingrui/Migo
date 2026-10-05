"""Contract tests: envelope, error codes, Product and HardConstraints.

These guard the A/C shared boundary. They must pass before any commerce code is
written, because everything downstream validates against these models.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import (
    Envelope,
    ErrorCode,
    ErrorDetail,
    HardConstraints,
    Product,
    new_request_id,
)
from app.contracts.common import http_status_for, is_retryable
from app.contracts.product import CURRENCIES, USE_CASES


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def test_success_envelope_shape():
    env = Envelope.success({"product_id": "hp_0001"}, "req_test")
    payload = env.model_dump()
    assert payload == {
        "ok": True,
        "data": {"product_id": "hp_0001"},
        "error": None,
        "request_id": "req_test",
    }


def test_failure_envelope_shape():
    err = ErrorDetail.of(ErrorCode.PRODUCT_NOT_FOUND, "Product not found")
    env = Envelope.failure(err, "req_test")
    payload = env.model_dump()
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert payload["request_id"] == "req_test"


def test_error_details_is_always_an_object():
    """The team contract forbids error.details = null."""
    err = ErrorDetail.of(ErrorCode.PRODUCT_NOT_FOUND, "Product not found")
    assert err.details == {}
    assert isinstance(err.details, dict)
    assert err.model_dump()["details"] == {}


def test_error_details_preserved_and_json_serialisable():
    err = ErrorDetail.of(
        ErrorCode.INVALID_CONSTRAINTS,
        "minimum price exceeds maximum",
        details={"min_price_cents": 30000, "max_price_cents": 20000},
    )
    assert err.details["min_price_cents"] == 30000
    json.dumps(err.model_dump())  # must not raise


def test_retryable_defaults_follow_the_code():
    assert is_retryable(ErrorCode.DB_BUSY) is True
    assert is_retryable(ErrorCode.MANDATE_REVOKED) is False
    assert ErrorDetail.of(ErrorCode.DB_BUSY, "busy").retryable is True
    assert ErrorDetail.of(ErrorCode.MANDATE_REVOKED, "revoked").retryable is False


def test_policy_denials_are_handled_outcomes_not_transport_errors():
    for code in (
        ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
        ErrorCode.ROLLING_CAP_EXCEEDED,
        ErrorCode.VELOCITY_LIMIT_EXCEEDED,
        ErrorCode.ANC_REQUIREMENT_NOT_MET,
    ):
        assert http_status_for(code) == 200


def test_missing_resources_map_to_404():
    assert http_status_for(ErrorCode.PRODUCT_NOT_FOUND) == 404
    assert http_status_for(ErrorCode.MANDATE_NOT_FOUND) == 404
    assert http_status_for(ErrorCode.QUOTE_NOT_FOUND) == 404


def test_request_ids_are_unique_and_prefixed():
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("req_") for i in ids)


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

def _product(**overrides):
    base = dict(
        product_id="hp_0001",
        category="headphones",
        name="Demo Headphones",
        brand="Demo",
        model="D1",
        variant="Black",
        connection="wireless",
        form_factor="in_ear",
        price_cents=29900,
        currency="HKD",
        stock=5,
        anc=True,
        battery_hours=8.0,
        wearing_weight_g=9.0,
        use_cases=["commute"],
        source_type="demo",
        source_url=None,
        data_note="demo record",
        seller_description="A short demo description.",
        shipping_origin="Hong Kong",
    )
    base.update(overrides)
    return base


def test_product_accepts_the_full_hkd_field_set():
    product = Product.model_validate(_product())
    assert product.currency == "HKD"
    assert product.price_cents == 29900
    assert product.price_hkd == 299.0


def test_product_rejects_cny():
    """The catalog was revised to HKD; CNY must be refused, not silently accepted."""
    with pytest.raises(ValidationError):
        Product.model_validate(_product(currency="CNY"))


def test_product_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Product.model_validate(_product(extra_field="nope"))


@pytest.mark.parametrize("field", ["seller_description", "shipping_origin"])
def test_new_required_fields_are_enforced(field):
    data = _product()
    del data[field]
    with pytest.raises(ValidationError):
        Product.model_validate(data)


def test_seller_description_must_be_under_100_characters():
    Product.model_validate(_product(seller_description="x" * 99))
    with pytest.raises(ValidationError):
        Product.model_validate(_product(seller_description="x" * 100))


def test_wired_products_must_have_null_battery():
    Product.model_validate(_product(connection="wired", battery_hours=None))
    with pytest.raises(ValidationError):
        Product.model_validate(_product(connection="wired", battery_hours=8.0))


def test_demo_products_must_have_null_source_url():
    Product.model_validate(_product(source_type="demo", source_url=None))
    with pytest.raises(ValidationError):
        Product.model_validate(_product(source_url="https://example.com/x"))


def test_source_url_must_be_http():
    with pytest.raises(ValidationError):
        Product.model_validate(
            _product(source_type="external", source_url="ftp://example.com/x")
        )


def test_unknown_specs_stay_null_and_are_not_zero():
    """anc=None means unknown. It must not become False, and must not pass a
    mandate that requires ANC."""
    product = Product.model_validate(_product(anc=None, battery_hours=None,
                                              wearing_weight_g=None,
                                              connection="wireless"))
    assert product.anc is None
    assert product.anc_is_known is False
    assert product.battery_hours is None
    assert product.wearing_weight_g is None


def test_use_cases_must_be_unique_and_known():
    with pytest.raises(ValidationError):
        Product.model_validate(_product(use_cases=["commute", "commute"]))
    with pytest.raises(ValidationError):
        Product.model_validate(_product(use_cases=["skiing"]))
    assert set(USE_CASES) == {"commute", "study", "gaming", "sports", "calls", "music"}


def test_product_rejects_negative_and_float_money():
    with pytest.raises(ValidationError):
        Product.model_validate(_product(price_cents=-1))
    with pytest.raises(ValidationError):
        Product.model_validate(_product(price_cents=299.0))


def test_product_is_immutable():
    product = Product.model_validate(_product())
    with pytest.raises(ValidationError):
        product.price_cents = 1


def test_to_row_flattens_for_the_strict_sqlite_table():
    product = Product.model_validate(_product(anc=False))
    row = product.to_row()
    assert len(row) == 24
    assert row["supported_devices"] is None
    assert row["tags"] == "[]"
    assert row["color"] is None
    assert row["estimated_delivery_days"] is None
    assert row["anc"] == 0                      # bool stored as integer
    assert row["use_cases"] == '["commute"]'    # list stored as JSON text
    assert row["currency"] == "HKD"
    assert isinstance(row["use_cases"], str)

    unknown = Product.model_validate(_product(anc=None)).to_row()
    assert unknown["anc"] is None               # unknown stays NULL


def test_currencies_are_hkd_only():
    assert CURRENCIES == ("HKD",)


# ---------------------------------------------------------------------------
# HardConstraints
# ---------------------------------------------------------------------------

def test_hard_constraints_defaults():
    c = HardConstraints()
    assert c.category == "headphones"
    assert c.anc_required is False
    assert c.in_stock_only is True
    assert c.brand_allowlist == []
    assert c.min_price_cents is None


def test_hard_constraints_accepts_inclusive_price_boundary():
    """A product priced exactly at max_price_cents is included."""
    HardConstraints(max_price_cents=30000)
    HardConstraints(min_price_cents=30000, max_price_cents=30000)


def test_hard_constraints_rejects_inverted_price_range():
    with pytest.raises(ValidationError) as exc:
        HardConstraints(min_price_cents=30000, max_price_cents=20000)
    assert "INVALID_CONSTRAINTS" in str(exc.value)


def test_hard_constraints_as_dict_yields_primitives():
    """D's coerce() re-validates each value with isinstance(str)/isinstance(bool)."""
    c = HardConstraints(connection="wireless", anc_required=True)
    data = c.as_dict()
    assert type(data["connection"]) is str
    assert type(data["anc_required"]) is bool
    assert type(data["in_stock_only"]) is bool
    assert data["connection"] == "wireless"


def test_hard_constraints_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        HardConstraints(prefer_anc=True)


def test_hard_constraints_rejects_duplicate_brands():
    with pytest.raises(ValidationError):
        HardConstraints(brand_allowlist=["A", "A"])


# ---------------------------------------------------------------------------
# Integration with D (the shared type boundary)
# ---------------------------------------------------------------------------

def test_repository_returns_the_shared_pydantic_product(tmp_path):
    """The documented seam: ProductRepository(product_model=Product)."""
    pytest.importorskip("app.catalog.repository")
    from app.catalog.repository import ProductRepository
    from app.db.initialize import initialize_database

    db = tmp_path / "catalog.sqlite3"
    initialize_database(db)

    repo = ProductRepository(db_path=db, product_model=Product)
    product = repo.get_product("hp_0001")
    assert isinstance(product, Product)
    assert product.currency == "HKD"


def test_repository_accepts_pydantic_hard_constraints(tmp_path):
    from app.catalog.repository import ProductRepository
    from app.db.initialize import initialize_database

    db = tmp_path / "catalog.sqlite3"
    initialize_database(db)

    repo = ProductRepository(db_path=db, product_model=Product)
    results = repo.list_candidates(
        HardConstraints(max_price_cents=30000, connection="wireless", anc_required=True)
    )
    assert results, "expected at least one wireless ANC product at or under HK$300"
    assert all(isinstance(p, Product) for p in results)
    for product in results:
        assert product.price_cents <= 30000      # inclusive boundary
        assert product.connection == "wireless"
        assert product.anc is True               # unknown never passes
        assert product.stock > 0
    ids = [p.product_id for p in results]
    assert "hp_0007" in ids, "hp_0007 is the HK$300 boundary product"


def test_repository_excludes_unknown_anc_when_anc_is_required(tmp_path):
    from app.catalog.repository import ProductRepository
    from app.db.initialize import initialize_database

    db = tmp_path / "catalog.sqlite3"
    initialize_database(db)

    repo = ProductRepository(db_path=db, product_model=Product)
    results = repo.list_candidates(HardConstraints(anc_required=True))
    assert results
    assert all(p.anc is True for p in results)
