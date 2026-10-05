"""Contract tests for the mandate, commerce and policy models.

These pin the semantics that the commerce layer depends on: exact integer
arithmetic, content-hash determinism, and the invariants that make a denial
auditable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts import (
    DenialReceipt,
    ErrorCode,
    Mandate,
    MandateDraft,
    PaymentRail,
    PaymentReceipt,
    PaymentRouteEvaluation,
    PolicyDecision,
    PolicyViolation,
    PurchaseProposal,
    Quote,
    Reservation,
    SourceType,
    SpendState,
    canonical_bytes,
    policy_hash,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _mandate(**overrides) -> dict:
    policy = {
        "allowed_categories": ["headphones"],
        "allowed_merchants": ["demo_audio_store"],
        "allowed_payment_routes": ["fps_demo", "mc_1234"],
        "anc_required": True,
        "cap_per_transaction_cents": 30000,
        "currency": "HKD",
        "escalate_above_cents": None,
        "max_quantity_total": 2,
        "required_connection": "wireless",
        "rolling_cap_cents": 50000,
        "rolling_window_seconds": 86400,
        "velocity_max_count": 2,
        "velocity_window_seconds": 300,
    }
    base = dict(
        mandate_id="man_0001",
        version=1,
        principal_id="demo_user",
        agent_id="demo_agent",
        status="ACTIVE",
        currency="HKD",
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=7),
        allowed_merchants=("demo_audio_store",),
        allowed_categories=("headphones",),
        required_connection="wireless",
        anc_required=True,
        cap_per_transaction_cents=30000,
        rolling_cap_cents=50000,
        rolling_window_seconds=86400,
        velocity_max_count=2,
        velocity_window_seconds=300,
        max_quantity_total=2,
        escalate_above_cents=None,
        allowed_payment_routes=("fps_demo", "mc_1234"),
        shipping_address_id="addr_demo_01",
        address_change_allowed=False,
        canonical_policy=json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        policy_hash=policy_hash(policy),
        consent_event_id="evt_consent_0001",
        created_at=NOW,
    )
    base.update(overrides)
    return base


def _quote(**overrides) -> dict:
    base = dict(
        quote_id="q_0001",
        merchant_id="demo_audio_store",
        product_id="hp_0001",
        product_name="Demo Headphones",
        category="headphones",
        connection="wireless",
        form_factor="in_ear",
        anc=True,
        quantity=1,
        unit_price_cents=29900,
        subtotal_cents=29900,
        shipping_cents=1000,
        tax_cents=0,
        discount_cents=0,
        merchant_total_cents=30900,
        currency="HKD",
        source_type=SourceType.SANDBOX,
        source_ref=None,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        quote_hash="sha256:placeholder",
    )
    base.update(overrides)
    if "quote_hash" not in overrides:
        base["quote_hash"] = Quote.model_construct(**base).compute_hash()
    return base


def _route(**overrides) -> dict:
    base = dict(
        route_id="fps_demo",
        rail=PaymentRail.FPS,
        accepted_by_merchant=True,
        allowed_by_mandate=True,
        owned_by_principal=True,
        eligible=True,
        merchant_total_cents=30900,
        fee_cents=0,
        fx_cost_cents=0,
        cash_total_cents=30900,
        reward_value_cents=0,
        effective_cost_cents=30900,
        evidence_id="ev_fps_0001",
        evidence_type=SourceType.SANDBOX,
        rejection_reasons=[],
    )
    base.update(overrides)
    return base


def _spend(**overrides) -> dict:
    base = dict(
        settled_cents=0, captured_cents=0, reserved_cents=0,
        settled_count=0, captured_count=0, reserved_count=0,
        purchased_quantity=0, reserved_quantity=0,
        rolling_window_start=NOW - timedelta(hours=1),
        velocity_window_start=NOW - timedelta(minutes=1),
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Canonical policy hashing
# ---------------------------------------------------------------------------

def test_hash_is_stable_across_key_order():
    a = {"b": 1, "a": 2, "c": [3, 4]}
    b = {"c": [3, 4], "a": 2, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert policy_hash(a) == policy_hash(b)


def test_hash_changes_when_a_rule_changes():
    base = {"cap_per_transaction_cents": 30000}
    changed = {"cap_per_transaction_cents": 31000}
    assert policy_hash(base) != policy_hash(changed)


def test_hash_is_prefixed_and_hex():
    digest = policy_hash({"a": 1})
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_hash_tolerates_non_ascii_merchant_names():
    """ensure_ascii=False keeps non-ASCII stable, not escaped."""
    payload = {"allowed_merchants": ["陳記耳機"]}
    assert "陳記耳機" in canonical_bytes(payload).decode("utf-8")
    assert policy_hash(payload) == policy_hash({"allowed_merchants": ["陳記耳機"]})


# ---------------------------------------------------------------------------
# Mandate
# ---------------------------------------------------------------------------

def test_mandate_accepts_a_consistent_policy():
    mandate = Mandate.model_validate(_mandate())
    assert mandate.is_active(NOW) is True
    assert mandate.is_expired(NOW) is False


def test_mandate_rejects_tampered_canonical_policy():
    """A row whose stored policy no longer matches its hash must not load."""
    data = _mandate()
    tampered = json.loads(data["canonical_policy"])
    tampered["cap_per_transaction_cents"] = 999999
    data["canonical_policy"] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValidationError) as exc:
        Mandate.model_validate(data)
    assert "does not match policy_hash" in str(exc.value)


def test_mandate_rejects_escalation_above_cap():
    with pytest.raises(ValidationError):
        Mandate.model_validate(_mandate(escalate_above_cents=99900))


def test_mandate_rejects_per_transaction_cap_above_rolling_cap():
    with pytest.raises(ValidationError):
        Mandate.model_validate(_mandate(cap_per_transaction_cents=90000))


def test_mandate_rejects_expiry_before_valid_from():
    with pytest.raises(ValidationError):
        Mandate.model_validate(_mandate(expires_at=NOW - timedelta(days=1)))


def test_mandate_is_immutable():
    mandate = Mandate.model_validate(_mandate())
    with pytest.raises(ValidationError):
        mandate.status = "REVOKED"


def test_mandate_executable_policy_is_order_stable():
    mandate = Mandate.model_validate(_mandate())
    first = mandate.executable_policy()
    second = mandate.executable_policy()
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["allowed_categories"] == ["headphones"]
    assert first["allowed_payment_routes"] == ["fps_demo", "mc_1234"]
    assert "created_at" not in first  # no timestamps in the hashed policy


def test_revoked_or_expired_mandate_is_not_active():
    assert Mandate.model_validate(_mandate(status="REVOKED")).is_active(NOW) is False
    assert Mandate.model_validate(
        _mandate(expires_at=NOW - timedelta(seconds=1))
    ).is_expired(NOW) is True


@pytest.mark.parametrize("field", [
    "allowed_merchants", "allowed_categories", "allowed_payment_routes",
])
def test_an_empty_allowlist_is_refused(field):
    """An empty allowlist permits nothing, so the mandate could never authorise.

    The evaluator treats these as filters, so a builder that dropped the list
    would produce a mandate that silently refuses everything. Caught at
    construction rather than discovered as an unexplained denial.
    """
    with pytest.raises(ValidationError) as exc:
        Mandate.model_validate(_mandate(**{field: ()}))
    assert "permits nothing" in str(exc.value)


def test_allowlist_entries_must_be_nonempty():
    with pytest.raises(ValidationError):
        Mandate.model_validate(_mandate(allowed_merchants=("  ",)))
    with pytest.raises(ValidationError):
        Mandate.model_validate(_mandate(allowed_payment_routes=("",)))


# ---------------------------------------------------------------------------
# MandateDraft
# ---------------------------------------------------------------------------

def test_draft_reports_missing_fields_then_becomes_activatable():
    draft = MandateDraft()
    missing = draft.missing_required_fields()
    assert "cap_per_transaction_cents" in missing
    assert "valid_for_seconds" in missing
    assert draft.can_activate() is False

    complete = MandateDraft(
        allowed_merchants=["demo_audio_store"],
        allowed_categories=["headphones"],
        required_connection="wireless",
        anc_required=True,
        cap_per_transaction_cents=30000,
        rolling_cap_cents=50000,
        rolling_window_seconds=86400,
        velocity_max_count=2,
        velocity_window_seconds=300,
        max_quantity_total=2,
        valid_for_seconds=604800,
        allowed_payment_routes=["fps_demo"],
        shipping_address_id="addr_demo_01",
        address_change_allowed=False,
    )
    assert complete.missing_required_fields() == []
    assert complete.structural_problems() == []
    assert complete.can_activate() is True


def test_draft_flags_contradictions_not_just_missing_values():
    draft = MandateDraft(
        cap_per_transaction_cents=60000,
        rolling_cap_cents=50000,
        rolling_window_seconds=86400,
        escalate_above_cents=90000,
        valid_for_seconds=600,
    )
    problems = draft.structural_problems()
    assert "transaction cap exceeds rolling cap" in problems
    assert "escalation threshold exceeds transaction cap" in problems
    assert draft.can_activate() is False


def test_draft_flags_half_specified_velocity_limit():
    draft = MandateDraft(velocity_max_count=2)
    assert "velocity limit needs both a count and a window" in draft.structural_problems()


def test_draft_rejects_negative_money():
    with pytest.raises(ValidationError):
        MandateDraft(cap_per_transaction_cents=-1)


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

def test_quote_amounts_must_reconcile():
    quote = Quote.model_validate(_quote())
    assert quote.merchant_total_cents == 30900
    assert quote.hash_matches() is True


def test_quote_rejects_inconsistent_subtotal():
    with pytest.raises(ValidationError):
        Quote.model_validate(_quote(subtotal_cents=100, merchant_total_cents=30900))


def test_quote_rejects_inconsistent_total():
    with pytest.raises(ValidationError):
        Quote.model_validate(_quote(merchant_total_cents=30000))


def test_quote_hash_is_stable_and_excludes_timestamps():
    first = Quote.model_validate(_quote())
    later = Quote.model_validate(_quote(issued_at=NOW + timedelta(minutes=1),
                                       expires_at=NOW + timedelta(minutes=6)))
    assert first.quote_hash == later.quote_hash  # same price, same hash


def test_quote_hash_changes_when_price_changes():
    first = Quote.model_validate(_quote())
    repriced = _quote(unit_price_cents=28900, subtotal_cents=28900, merchant_total_cents=29900)
    assert Quote.model_validate(repriced).quote_hash != first.quote_hash


def test_quote_detects_a_tampered_hash():
    data = _quote()
    data["quote_hash"] = "sha256:" + "0" * 64
    assert Quote.model_validate(data).hash_matches() is False


def test_quote_expiry():
    quote = Quote.model_validate(_quote())
    assert quote.is_expired(NOW) is False
    assert quote.is_expired(NOW + timedelta(minutes=10)) is True


# ---------------------------------------------------------------------------
# PaymentRouteEvaluation
# ---------------------------------------------------------------------------

def test_route_arithmetic_is_enforced():
    route = PaymentRouteEvaluation.model_validate(_route())
    assert route.cash_total_cents == 30900
    assert route.effective_cost_cents == 30900


def test_route_rejects_broken_cash_total():
    with pytest.raises(ValidationError):
        PaymentRouteEvaluation.model_validate(_route(cash_total_cents=1))


def test_route_rejects_broken_effective_cost():
    with pytest.raises(ValidationError):
        PaymentRouteEvaluation.model_validate(
            _route(reward_value_cents=500, effective_cost_cents=999)
        )


def test_reward_never_reduces_cash_total():
    """The cap is checked against cash_total, so a reward must not shrink it."""
    route = PaymentRouteEvaluation.model_validate(
        _route(fee_cents=100, cash_total_cents=31000,
               reward_value_cents=2000, effective_cost_cents=29000)
    )
    assert route.cash_total_cents == 31000
    assert route.effective_cost_cents == 29000
    assert route.effective_cost_cents < route.cash_total_cents


def test_ineligible_route_must_explain_itself():
    good = _route(eligible=False, allowed_by_mandate=False,
                  rejection_reasons=["route not permitted by mandate"])
    route = PaymentRouteEvaluation.model_validate(good)
    assert route.is_usable is False
    assert route.rejection_reasons

    with pytest.raises(ValidationError):
        PaymentRouteEvaluation.model_validate(_route(eligible=False, rejection_reasons=[]))
    with pytest.raises(ValidationError):
        PaymentRouteEvaluation.model_validate(_route(eligible=True, rejection_reasons=["nope"]))


def test_route_rejects_negative_fee():
    with pytest.raises(ValidationError):
        PaymentRouteEvaluation.model_validate(_route(fee_cents=-1))


# ---------------------------------------------------------------------------
# PurchaseProposal
# ---------------------------------------------------------------------------

def test_proposal_carries_no_derived_amounts():
    """A must not be able to submit amounts, budget or hashes."""
    base = dict(
        proposal_id="prop_0001",
        mandate_id="man_0001",
        expected_mandate_version=1,
        principal_id="demo_user",
        agent_id="demo_agent",
        product_id="hp_0001",
        quantity=1,
        merchant_id="demo_audio_store",
        quote_id="q_0001",
        preferred_payment_route_ids=["fps_demo"],
        shipping_address_id="addr_demo_01",
        request_id="req_0001",
        idempotency_key="idem_0001",
        created_at=NOW,
    )
    proposal = PurchaseProposal.model_validate(base)
    assert not hasattr(proposal, "cash_total_cents")

    for forbidden in ("cash_total_cents", "remaining_budget_cents", "policy_hash",
                      "payment_status", "wallet_balance_after_cents", "reward_earned_cents"):
        with pytest.raises(ValidationError):
            PurchaseProposal.model_validate({**base, forbidden: 1})


def test_proposal_request_hash_ignores_the_idempotency_key():
    base = dict(
        proposal_id="prop_0001", mandate_id="man_0001", expected_mandate_version=1,
        principal_id="demo_user", agent_id="demo_agent", product_id="hp_0001",
        quantity=1, merchant_id="demo_audio_store", quote_id="q_0001",
        preferred_payment_route_ids=["fps_demo"], shipping_address_id="addr_demo_01",
        request_id="req_0001", idempotency_key="a", created_at=NOW,
    )
    first = PurchaseProposal.model_validate(base)
    second = PurchaseProposal.model_validate({**base, "idempotency_key": "b",
                                              "proposal_id": "prop_0002"})
    assert first.request_hash() == second.request_hash()

    third = PurchaseProposal.model_validate({**base, "quantity": 2})
    assert third.request_hash() != first.request_hash()


# ---------------------------------------------------------------------------
# SpendState
# ---------------------------------------------------------------------------

def test_exposure_includes_held_reservations():
    state = SpendState.model_validate(_spend(settled_cents=20000, reserved_cents=15000))
    assert state.exposure_cents == 35000
    assert state.exposure_cents > state.settled_cents


def test_with_reservation_adds_to_exposure():
    state = SpendState.model_validate(_spend(settled_cents=10000))
    after = state.with_reservation(6000, 1)
    assert after.reserved_cents == 6000
    assert after.exposure_cents == 16000
    assert after.reserved_quantity == 1
    assert state.reserved_cents == 0  # original untouched


def test_two_concurrent_proposals_share_one_window():
    """The concurrency bug this type exists to prevent.

    Each proposal alone fits; together they do not.
    """
    rolling_cap = 50000
    state = SpendState.model_validate(_spend())

    first = state.with_reservation(30000, 1)
    assert first.exposure_cents <= rolling_cap

    second = first.with_reservation(30000, 1)
    assert second.exposure_cents == 60000
    assert second.exposure_cents > rolling_cap


def test_the_window_is_sliding_and_the_caller_recomputes_the_counts():
    """Documents the obligation the evaluator cannot check from numbers alone.

    ``rolling_window_start`` is when the supplied counts begin, not the start of
    the window being judged. A caller that accumulates instead of recomputing
    produces plausible, wrong values -- so the semantics are pinned here.
    """

    # A purchase settled an hour ago, inside the window.
    recent = SpendState.model_validate(_spend(
        settled_cents=20000, settled_count=1, purchased_quantity=1,
        rolling_window_start=NOW - timedelta(hours=1)))
    # The same purchase, one day later: the caller has recomputed the counts
    # over the new window and it has aged out.
    aged_out = SpendState.model_validate(_spend(
        settled_cents=0, settled_count=0, purchased_quantity=0,
        rolling_window_start=NOW - timedelta(hours=1)))

    assert recent.exposure_cents == 20000
    assert aged_out.exposure_cents == 0
    # Both states are structurally valid; only the caller's recomputation makes
    # the difference, which is exactly why it is written down.


# ---------------------------------------------------------------------------
# PolicyDecision / DenialReceipt invariants
# ---------------------------------------------------------------------------

def _decision(**overrides) -> dict:
    base = dict(
        outcome="APPROVE",
        primary_reason=None,
        violations=[],
        mandate_id="man_0001",
        mandate_version=1,
        policy_hash="sha256:" + "a" * 64,
        proposal_id="prop_0001",
        quote_id="q_0001",
        quote_hash="sha256:" + "b" * 64,
        cash_total_cents=30900,
        currency="HKD",
        payment_route_id="fps_demo",
        observed_values={"cash_total_cents": 30900},
        applicable_limits={"cap_per_transaction_cents": 30000},
        reservation_created=True,
        payment_adapter_called=False,
        evaluated_at=NOW,
    )
    base.update(overrides)
    return base


def test_approve_must_be_clean():
    decision = PolicyDecision.model_validate(_decision())
    assert decision.is_approved is True

    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(_decision(primary_reason=ErrorCode.ROLLING_CAP_EXCEEDED))
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(_decision(violations=[
            PolicyViolation(code=ErrorCode.ROLLING_CAP_EXCEEDED, field="rolling_cap_cents",
                            message="x")
        ]))


def test_deny_must_never_reserve_or_pay():
    violation = PolicyViolation(
        code=ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
        field="cap_per_transaction_cents",
        message="landed cost exceeds the per-transaction cap",
        observed=32500,
        limit=30000,
    )
    deny = PolicyDecision.model_validate(_decision(
        outcome="DENY",
        primary_reason=ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
        violations=[violation],
        reservation_created=False,
        payment_adapter_called=False,
    ))
    assert deny.is_approved is False
    assert deny.violations[0].observed == 32500
    assert "observed=32500" in deny.violations[0].describe()

    for bad in ({"reservation_created": True}, {"payment_adapter_called": True}):
        payload = _decision(
            outcome="DENY",
            primary_reason=ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
            violations=[violation],
            reservation_created=False,
            payment_adapter_called=False,
        )
        payload.update(bad)
        with pytest.raises(ValidationError):
            PolicyDecision.model_validate(payload)


def test_escalate_also_must_not_reserve_or_pay():
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(_decision(
            outcome="ESCALATE",
            primary_reason=ErrorCode.ESCALATION_REQUIRED,
            violations=[],
            reservation_created=False,
            payment_adapter_called=True,
        ))


def test_non_approve_outcomes_need_a_primary_reason():
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(_decision(outcome="DENY", violations=[], primary_reason=None))


def test_denial_receipt_forbids_money_movement():
    violation = PolicyViolation(
        code=ErrorCode.ROLLING_CAP_EXCEEDED, field="rolling_cap_cents",
        message="rolling window cap exceeded", observed=60000, limit=50000,
    )
    receipt = DenialReceipt.model_validate(dict(
        denial_id="den_0001",
        proposal_id="prop_0001",
        mandate_id="man_0001",
        mandate_version=1,
        policy_hash="sha256:" + "a" * 64,
        primary_reason=ErrorCode.ROLLING_CAP_EXCEEDED,
        violations=[violation],
        observed_values={"exposure_cents": 60000},
        applicable_limits={"rolling_cap_cents": 50000},
        quote_id="q_0001",
        cash_total_cents=30900,
        currency="HKD",
        reservation_created=False,
        payment_adapter_called=False,
        created_at=NOW,
    ))
    assert receipt.reservation_created is False
    assert receipt.payment_adapter_called is False

    with pytest.raises(ValidationError):
        DenialReceipt.model_validate({**receipt.model_dump(), "reservation_created": True})


# ---------------------------------------------------------------------------
# PaymentReceipt: one name for one quantity
# ---------------------------------------------------------------------------

def _receipt(**overrides) -> dict:
    base = dict(
        payment_id="pay_0001",
        reservation_id="rsv_0001",
        order_id="ord_0001",
        principal_id="demo_user",
        mandate_id="man_0001",
        mandate_version=1,
        quote_id="q_0001",
        quote_hash="sha256:" + "b" * 64,
        payment_route_id="fps_demo",
        rail="FPS",
        cash_total_cents=30900,
        fee_cents=0,
        fx_cost_cents=0,
        merchant_total_cents=30900,
        currency="HKD",
        balance_after_cents=69100,
        reservation_status="CAPTURED",
        order_status="paid",
        reward_earned_cents=0,
        reward_evidence_type=None,
        settled_at=NOW,
        audit_event_id=None,
    )
    base.update(overrides)
    return base


def test_receipt_uses_the_same_money_name_as_the_rest_of_the_system():
    """``cash_total_cents`` everywhere, so two readers cannot disagree.

    An earlier version called this ``amount_cents`` alongside a separate
    ``fee_cents`` and never said whether the fee was inside it.
    """
    receipt = PaymentReceipt.model_validate(_receipt())
    assert receipt.cash_total_cents == 30900
    assert "amount_cents" not in PaymentReceipt.model_fields


def test_receipt_fee_is_inside_the_total_not_added_to_it():
    receipt = PaymentReceipt.model_validate(_receipt(
        merchant_total_cents=29900, fee_cents=1000, cash_total_cents=30900))
    assert receipt.cash_total_cents == 30900          # not 31900
    assert receipt.fee_cents == 1000
    assert receipt.merchant_total_cents + receipt.fee_cents == receipt.cash_total_cents


def test_receipt_rejects_a_total_that_does_not_reconcile():
    with pytest.raises(ValidationError):
        PaymentReceipt.model_validate(_receipt(
            merchant_total_cents=29900, fee_cents=1000, cash_total_cents=29900))


def test_receipt_rejects_a_fee_larger_than_the_total():
    with pytest.raises(ValidationError):
        PaymentReceipt.model_validate(_receipt(
            fee_cents=99999, merchant_total_cents=0, cash_total_cents=100))


def test_receipt_may_omit_the_breakdown_but_must_state_the_total():
    receipt = PaymentReceipt.model_validate(_receipt(merchant_total_cents=None))
    assert receipt.cash_total_cents == 30900
    assert receipt.merchant_total_cents is None


def test_receipt_carries_the_rail_for_the_reconciliation_view():
    assert PaymentReceipt.model_validate(_receipt()).rail is PaymentRail.FPS


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------

def test_reservation_holding_statuses():
    base = dict(
        reservation_id="rsv_0001", proposal_id="prop_0001", principal_id="demo_user",
        mandate_id="man_0001", mandate_version=1, quote_id="q_0001",
        quote_hash="sha256:" + "b" * 64, payment_route_id="fps_demo",
        amount_cents=30900, currency="HKD", quantity=1,
        created_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    for status, expected in (("ACTIVE", True), ("PAYMENT_SUBMITTING", True),
                             ("UNKNOWN", True), ("CAPTURED", False),
                             ("SETTLED", False), ("RELEASED", False),
                             ("FAILED", False), ("EXPIRED", False)):
        reservation = Reservation.model_validate({**base, "status": status})
        assert reservation.holds_budget is expected, status
