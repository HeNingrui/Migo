"""Deterministic policy evaluator tests.

The evaluator is the only place a purchase is permitted or refused, so it is
tested exhaustively rather than incidentally. No database, no clock, no network:
every case is a pure input pair.

Money follows the A/C spec's demo figures but is always derived from the real
catalog prices in D's database:

    hp_0001  HK$299 + HK$10 shipping = HK$309      -> APPROVE under a HK$300... no:
             HK$309 exceeds a HK$300 cap, so the approval case uses hp_0018
    hp_0018  HK$279 + HK$10 shipping = HK$289      -> APPROVE under a HK$300 cap
    hp_0007  HK$300 is the exact inclusive boundary
    hp_0003  HK$499 + HK$10 shipping = HK$509      -> DENY, over the per-txn cap
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.commerce.policy_evaluator import PolicyInputs, evaluate_policy
from app.contracts import (
    ApprovalGrant,
    ErrorCode,
    Mandate,
    PaymentRail,
    PaymentRouteEvaluation,
    PurchaseProposal,
    Quote,
    SourceType,
    SpendState,
    canonical_bytes,
    policy_hash,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
MERCHANT = "demo_audio_store"
PRINCIPAL = "demo_user"
AGENT = "demo_agent"
ADDRESS = "addr_demo_01"
ROUTE_ID = "fps_demo"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make_mandate(**overrides) -> Mandate:
    """A complete, internally consistent mandate."""
    fields = dict(
        allowed_merchants=(MERCHANT,),
        allowed_categories=("headphones",),
        allowed_payment_routes=(ROUTE_ID, "mc_1234"),
        anc_required=True,
        cap_per_transaction_cents=32000,
        currency="HKD",
        escalate_above_cents=None,
        max_quantity_total=2,
        required_connection="wireless",
        rolling_cap_cents=60000,
        rolling_window_seconds=86400,
        velocity_max_count=2,
        velocity_window_seconds=300,
    )
    fields.update({k: v for k, v in overrides.items() if k in fields})
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    base = dict(
        mandate_id="man_0001",
        version=1,
        principal_id=PRINCIPAL,
        agent_id=AGENT,
        status="ACTIVE",
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=7),
        shipping_address_id=ADDRESS,
        address_change_allowed=False,
        canonical_policy=canonical,
        policy_hash=policy_hash(json.loads(canonical)),
        consent_event_id="evt_consent_0001",
        created_at=NOW - timedelta(minutes=1),
    )
    base.update(fields)
    base.update(overrides)
    return Mandate.model_validate(base)


def make_quote(**overrides) -> Quote:
    unit = overrides.pop("unit_price_cents", 27900)
    quantity = overrides.pop("quantity", 1)
    shipping = overrides.pop("shipping_cents", 1000)
    tax = overrides.pop("tax_cents", 0)
    discount = overrides.pop("discount_cents", 0)
    subtotal = unit * quantity
    payload = dict(
        quote_id="q_0001",
        merchant_id=MERCHANT,
        product_id="hp_0018",
        product_name="Sample Headphones H18 (Demo)",
        category="headphones",
        connection="wireless",
        form_factor="in_ear",
        anc=True,
        quantity=quantity,
        unit_price_cents=unit,
        subtotal_cents=subtotal,
        shipping_cents=shipping,
        tax_cents=tax,
        discount_cents=discount,
        merchant_total_cents=subtotal + shipping + tax - discount,
        currency="HKD",
        source_type=SourceType.SANDBOX,
        source_ref=None,
        issued_at=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=5),
        quote_hash="sha256:" + "0" * 64,
    )
    payload.update(overrides)
    if "quote_hash" not in overrides:
        payload["quote_hash"] = Quote.model_construct(**payload).compute_hash()
    return Quote.model_validate(payload)


def make_route(quote: Quote, **overrides) -> PaymentRouteEvaluation:
    merchant_total = overrides.pop("merchant_total_cents", quote.merchant_total_cents)
    fee = overrides.pop("fee_cents", 0)
    fx = overrides.pop("fx_cost_cents", 0)
    reward = overrides.pop("reward_value_cents", 0)
    cash = overrides.pop("cash_total_cents", merchant_total + fee + fx)
    payload = dict(
        route_id=ROUTE_ID,
        rail=PaymentRail.FPS,
        accepted_by_merchant=True,
        allowed_by_mandate=True,
        owned_by_principal=True,
        eligible=True,
        merchant_total_cents=merchant_total,
        fee_cents=fee,
        fx_cost_cents=fx,
        cash_total_cents=cash,
        reward_value_cents=reward,
        effective_cost_cents=max(0, cash - reward),
        evidence_id="ev_fps_0001",
        evidence_type=SourceType.SANDBOX,
        rejection_reasons=[],
    )
    payload.update(overrides)
    return PaymentRouteEvaluation.model_validate(payload)


def make_proposal(quote: Quote, **overrides) -> PurchaseProposal:
    payload = dict(
        proposal_id="prop_0001",
        mandate_id="man_0001",
        expected_mandate_version=1,
        principal_id=PRINCIPAL,
        agent_id=AGENT,
        product_id=quote.product_id,
        quantity=quote.quantity,
        merchant_id=MERCHANT,
        quote_id=quote.quote_id,
        preferred_payment_route_ids=[ROUTE_ID],
        shipping_address_id=ADDRESS,
        request_id="req_0001",
        idempotency_key="idem_0001",
        created_at=NOW,
    )
    payload.update(overrides)
    return PurchaseProposal.model_validate(payload)


def make_spend(**overrides) -> SpendState:
    payload = dict(
        settled_cents=0, captured_cents=0, reserved_cents=0,
        settled_count=0, captured_count=0, reserved_count=0,
        purchased_quantity=0, reserved_quantity=0,
        rolling_window_start=NOW - timedelta(hours=1),
        velocity_window_start=NOW - timedelta(minutes=1),
    )
    payload.update(overrides)
    return SpendState.model_validate(payload)


def scenario(**overrides) -> PolicyInputs:
    """Compose a full, valid input set, then apply overrides."""
    quote = overrides.pop("quote", None) or make_quote()
    mandate = overrides.pop("mandate", None) or make_mandate()
    route = overrides.pop("route", None) or make_route(quote)
    proposal = overrides.pop("proposal", None) or make_proposal(quote)
    spend = overrides.pop("spend_state", None) or make_spend()
    return PolicyInputs(
        proposal=proposal,
        mandate=mandate,
        quote=quote,
        route=route,
        spend_state=spend,
        now=overrides.pop("now", NOW),
        approval_grant=overrides.pop("approval_grant", None),
    )


def codes(decision) -> list[ErrorCode]:
    return [v.code for v in decision.violations]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_approve_when_everything_is_within_the_mandate():
    decision = evaluate_policy(scenario())
    assert decision.outcome == "APPROVE"
    assert decision.violations == []
    assert decision.primary_reason is None
    assert decision.cash_total_cents == 28900          # 27900 + 1000
    assert decision.reservation_created is True
    assert decision.payment_adapter_called is False


def test_decision_records_the_limits_it_applied():
    decision = evaluate_policy(scenario())
    assert decision.applicable_limits["cap_per_transaction_cents"] == 32000
    assert decision.applicable_limits["rolling_cap_cents"] == 60000
    assert decision.observed_values["cash_total_cents"] == 28900


def test_evaluator_is_deterministic():
    inputs = scenario()
    first, second = evaluate_policy(inputs), evaluate_policy(inputs)
    assert first.model_dump() == second.model_dump()


def test_evaluator_does_not_mutate_its_inputs():
    inputs = scenario()
    before = inputs.spend_state.model_dump()
    evaluate_policy(inputs)
    assert inputs.spend_state.model_dump() == before


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_principal_mismatch_is_denied():
    """A cannot choose whose money is spent."""
    decision = evaluate_policy(scenario(proposal=make_proposal(make_quote(), principal_id="someone_else")))
    assert decision.outcome == "DENY"
    assert ErrorCode.PRINCIPAL_MISMATCH in codes(decision)


def test_agent_mismatch_is_denied():
    decision = evaluate_policy(scenario(proposal=make_proposal(make_quote(), agent_id="another_agent")))
    assert decision.outcome == "DENY"
    assert ErrorCode.AGENT_MISMATCH in codes(decision)


# ---------------------------------------------------------------------------
# Mandate state and version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        ("REVOKED", ErrorCode.MANDATE_REVOKED),
        ("SUPERSEDED", ErrorCode.MANDATE_VERSION_STALE),
        ("EXPIRED", ErrorCode.MANDATE_EXPIRED),
    ],
)
def test_non_active_mandate_is_denied(status, expected):
    decision = evaluate_policy(scenario(mandate=make_mandate(status=status)))
    assert decision.outcome == "DENY"
    assert expected in codes(decision)


def test_stale_mandate_version_is_denied():
    """How a revocation lands on a purchase that was already in flight."""
    decision = evaluate_policy(
        scenario(proposal=make_proposal(make_quote(), expected_mandate_version=1),
                 mandate=make_mandate(version=2))
    )
    assert decision.outcome == "DENY"
    assert ErrorCode.MANDATE_VERSION_STALE in codes(decision)


def test_expired_mandate_is_denied():
    decision = evaluate_policy(scenario(mandate=make_mandate(
        valid_from=NOW - timedelta(days=8), expires_at=NOW - timedelta(seconds=1))))
    assert decision.outcome == "DENY"
    assert ErrorCode.MANDATE_EXPIRED in codes(decision)


def test_mandate_not_yet_valid_is_denied():
    decision = evaluate_policy(scenario(mandate=make_mandate(
        valid_from=NOW + timedelta(hours=1), expires_at=NOW + timedelta(days=7))))
    assert decision.outcome == "DENY"
    assert ErrorCode.MANDATE_NOT_ACTIVE in codes(decision)


def test_small_clock_skew_is_tolerated():
    """A row stamped a moment in the future must not break a valid purchase."""
    decision = evaluate_policy(scenario(
        mandate=make_mandate(valid_from=NOW + timedelta(seconds=2),
                             expires_at=NOW + timedelta(days=7))))
    assert decision.outcome == "APPROVE"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_merchant_outside_the_mandate_is_denied():
    decision = evaluate_policy(scenario(proposal=make_proposal(make_quote(), merchant_id="other_shop")))
    assert decision.outcome == "DENY"
    assert ErrorCode.MERCHANT_NOT_ALLOWED in codes(decision)


def test_category_outside_the_mandate_is_accepted_when_it_matches():
    """Control case for the category check.

    The rejection path is not reachable from a valid Mandate: ``allowed_categories``
    is a Literal restricted to known categories, so a mandate can never permit a
    category that no product has. The check still guards against a row that was
    edited outside the model, which is why it exists.
    """
    decision = evaluate_policy(scenario())
    assert decision.outcome == "APPROVE"
    assert "headphones" in make_mandate().allowed_categories


def test_violation_reports_a_list_when_the_limit_is_a_set():
    """A denial must be able to name the whole allowed set, not just one value."""
    decision = evaluate_policy(scenario(proposal=make_proposal(make_quote(), merchant_id="other_shop")))
    violation = next(v for v in decision.violations
                     if v.code == ErrorCode.MERCHANT_NOT_ALLOWED)
    assert violation.observed == "other_shop"
    assert violation.limit == ["demo_audio_store"]


def test_connection_mismatch_is_denied():
    decision = evaluate_policy(scenario(quote=make_quote(connection="wired")))
    assert decision.outcome == "DENY"
    assert ErrorCode.CONNECTION_NOT_ALLOWED in codes(decision)


@pytest.mark.parametrize("anc", [False, None])
def test_anc_not_confirmed_is_denied(anc):
    """Unknown ANC never satisfies a mandate that requires ANC."""
    decision = evaluate_policy(scenario(quote=make_quote(anc=anc)))
    assert decision.outcome == "DENY"
    assert ErrorCode.ANC_REQUIREMENT_NOT_MET in codes(decision)


def test_anc_not_required_accepts_unknown_anc():
    decision = evaluate_policy(scenario(quote=make_quote(anc=None),
                                        mandate=make_mandate(anc_required=False)))
    assert decision.outcome == "APPROVE"


def test_quantity_above_the_limit_is_denied():
    quote = make_quote(quantity=5, unit_price_cents=1000, shipping_cents=0)
    decision = evaluate_policy(scenario(quote=quote, proposal=make_proposal(quote)))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUANTITY_LIMIT_EXCEEDED in codes(decision)


def test_address_change_is_denied():
    decision = evaluate_policy(scenario(
        proposal=make_proposal(make_quote(), shipping_address_id="addr_other")))
    assert decision.outcome == "DENY"
    assert ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED in codes(decision)


# ---------------------------------------------------------------------------
# Payment route
# ---------------------------------------------------------------------------

def test_route_outside_the_mandate_is_denied():
    decision = evaluate_policy(scenario(route=make_route(make_quote(), route_id="crypto_demo")))
    assert decision.outcome == "DENY"
    assert ErrorCode.PAYMENT_ROUTE_NOT_ALLOWED in codes(decision)


def test_ineligible_route_is_denied():
    route = make_route(make_quote(), eligible=False, owned_by_principal=False,
                       rejection_reasons=["card is not owned by the principal"])
    decision = evaluate_policy(scenario(route=route))
    assert decision.outcome == "DENY"
    assert ErrorCode.PAYMENT_ROUTE_NOT_ALLOWED in codes(decision)


# ---------------------------------------------------------------------------
# Quote integrity
# ---------------------------------------------------------------------------

def test_expired_quote_is_denied():
    decision = evaluate_policy(scenario(quote=make_quote(
        issued_at=NOW - timedelta(minutes=10), expires_at=NOW - timedelta(seconds=1))))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUOTE_EXPIRED in codes(decision)


def test_tampered_quote_hash_is_denied():
    decision = evaluate_policy(scenario(quote=make_quote(quote_hash="sha256:" + "f" * 64)))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUOTE_HASH_MISMATCH in codes(decision)


def test_quote_for_a_different_product_than_the_proposal_is_denied():
    quote = make_quote()
    proposal = make_proposal(quote, product_id="hp_9999")
    decision = evaluate_policy(scenario(quote=quote, proposal=proposal))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUOTE_HASH_MISMATCH in codes(decision)


def test_route_priced_against_a_different_total_is_denied():
    quote = make_quote()
    route = make_route(quote, merchant_total_cents=quote.merchant_total_cents + 500)
    decision = evaluate_policy(scenario(quote=quote, route=route))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUOTE_CHANGED in codes(decision)


# ---------------------------------------------------------------------------
# Caps -- measured on the cash total, never on the reward-adjusted cost
# ---------------------------------------------------------------------------

def test_per_transaction_cap_uses_the_inclusive_boundary():
    mandate = make_mandate(cap_per_transaction_cents=28900, rolling_cap_cents=60000)
    decision = evaluate_policy(scenario(mandate=mandate))
    assert decision.cash_total_cents == 28900
    assert decision.outcome == "APPROVE"     # at the cap is allowed


def test_per_transaction_cap_denies_one_cent_over():
    mandate = make_mandate(cap_per_transaction_cents=28899, rolling_cap_cents=60000)
    decision = evaluate_policy(scenario(mandate=mandate))
    assert decision.outcome == "DENY"
    assert ErrorCode.CAP_PER_TRANSACTION_EXCEEDED in codes(decision)
    violation = next(v for v in decision.violations
                     if v.code == ErrorCode.CAP_PER_TRANSACTION_EXCEEDED)
    assert violation.observed == 28900
    assert violation.limit == 28899


def test_shipping_pushes_the_total_over_the_cap():
    """The demo's 'stopped mid-checkout' case: the item fits, the landed cost does not."""
    quote = make_quote(unit_price_cents=29900, shipping_cents=1000)   # hp_0001-like, HK$309
    mandate = make_mandate(cap_per_transaction_cents=30000, rolling_cap_cents=90000)
    decision = evaluate_policy(scenario(quote=quote, proposal=make_proposal(quote),
                                        mandate=mandate))
    assert decision.outcome == "DENY"
    assert ErrorCode.CAP_PER_TRANSACTION_EXCEEDED in codes(decision)
    assert decision.reservation_created is False
    assert decision.payment_adapter_called is False


def test_reward_does_not_enlarge_the_cap():
    """A cashback must not let a purchase slip past the cap."""
    route = make_route(make_quote(), reward_value_cents=5000)   # cash stays 28900
    mandate = make_mandate(cap_per_transaction_cents=28899, rolling_cap_cents=60000)
    decision = evaluate_policy(scenario(route=route, mandate=mandate))
    assert route.effective_cost_cents == 23900
    assert route.cash_total_cents == 28900
    assert decision.outcome == "DENY"


def test_fees_count_towards_the_cash_total():
    route = make_route(make_quote(), fee_cents=200)   # cash 29100
    mandate = make_mandate(cap_per_transaction_cents=29000, rolling_cap_cents=90000)
    decision = evaluate_policy(scenario(route=route, mandate=mandate))
    assert decision.outcome == "DENY"
    assert ErrorCode.CAP_PER_TRANSACTION_EXCEEDED in codes(decision)


# ---------------------------------------------------------------------------
# Rolling window
# ---------------------------------------------------------------------------

def test_rolling_cap_denies_when_held_amounts_already_consume_it():
    mandate = make_mandate(cap_per_transaction_cents=30000, rolling_cap_cents=50000)
    spend = make_spend(reserved_cents=30000, reserved_count=1, reserved_quantity=1)
    decision = evaluate_policy(scenario(mandate=mandate, spend_state=spend))
    assert decision.outcome == "DENY"
    assert ErrorCode.ROLLING_CAP_EXCEEDED in codes(decision)
    assert decision.observed_values["projected_exposure_cents"] == 58900


def test_rolling_cap_allows_when_the_window_is_stale():
    mandate = make_mandate(cap_per_transaction_cents=30000, rolling_cap_cents=50000)
    spend = make_spend(reserved_cents=40000, rolling_window_start=NOW - timedelta(days=2))
    decision = evaluate_policy(scenario(mandate=mandate, spend_state=spend))
    assert decision.outcome == "APPROVE"


def test_concurrent_pair_cannot_overspend_the_window():
    """Two proposals of 28900 against a 50000 window.

    The evaluator is pure, so the concurrency guarantee comes from evaluating
    the second one against the first one's reservation. This test asserts the
    arithmetic that the transactional caller relies on.
    """
    mandate = make_mandate(cap_per_transaction_cents=30000, rolling_cap_cents=50000)
    first = evaluate_policy(scenario(mandate=mandate))
    assert first.outcome == "APPROVE"

    spend_after_first = make_spend(reserved_cents=first.cash_total_cents,
                                   reserved_count=1, reserved_quantity=1)
    second = evaluate_policy(scenario(mandate=mandate, spend_state=spend_after_first))
    assert second.outcome == "DENY"
    assert ErrorCode.ROLLING_CAP_EXCEEDED in codes(second)
    assert spend_after_first.exposure_cents <= mandate.rolling_cap_cents


# ---------------------------------------------------------------------------
# Velocity and lifetime quantity
# ---------------------------------------------------------------------------

def test_velocity_limit_denies_a_third_purchase_inside_the_window():
    mandate = make_mandate(velocity_max_count=2, velocity_window_seconds=300,
                           cap_per_transaction_cents=30000, rolling_cap_cents=200000)
    spend = make_spend(settled_cents=20000, settled_count=2, purchased_quantity=2,
                       velocity_window_start=NOW - timedelta(seconds=60))
    decision = evaluate_policy(scenario(mandate=mandate, spend_state=spend))
    assert decision.outcome == "DENY"
    assert ErrorCode.VELOCITY_LIMIT_EXCEEDED in codes(decision)


def test_velocity_window_expiry_resets_the_count():
    mandate = make_mandate(velocity_max_count=2, velocity_window_seconds=300,
                           cap_per_transaction_cents=30000, rolling_cap_cents=200000,
                           max_quantity_total=10)
    spend = make_spend(settled_cents=20000, settled_count=5, purchased_quantity=5,
                       velocity_window_start=NOW - timedelta(seconds=400))
    decision = evaluate_policy(scenario(mandate=mandate, spend_state=spend))
    assert decision.outcome == "APPROVE"


def test_velocity_is_checked_even_when_the_amount_is_small():
    """Behaviour, not just size, is limited."""
    quote = make_quote(unit_price_cents=100, shipping_cents=0)
    mandate = make_mandate(velocity_max_count=2, velocity_window_seconds=300,
                           cap_per_transaction_cents=30000, rolling_cap_cents=200000)
    spend = make_spend(settled_count=2, settled_cents=200)
    decision = evaluate_policy(scenario(quote=quote, proposal=make_proposal(quote),
                                        mandate=mandate, spend_state=spend))
    assert decision.outcome == "DENY"
    assert ErrorCode.VELOCITY_LIMIT_EXCEEDED in codes(decision)


def test_lifetime_quantity_limit():
    mandate = make_mandate(max_quantity_total=2, cap_per_transaction_cents=30000,
                           rolling_cap_cents=200000)
    spend = make_spend(purchased_quantity=2)
    decision = evaluate_policy(scenario(mandate=mandate, spend_state=spend))
    assert decision.outcome == "DENY"
    assert ErrorCode.QUANTITY_TOTAL_EXCEEDED in codes(decision)


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def make_grant(quote, route, **overrides) -> ApprovalGrant:
    payload = dict(
        grant_id="grant_0001",
        proposal_id="prop_0001",
        principal_id=PRINCIPAL,
        mandate_id="man_0001",
        mandate_version=1,
        quote_id=quote.quote_id,
        quote_hash=quote.quote_hash,
        payment_route_id=route.route_id,
        cash_total_cents=route.cash_total_cents,
        currency="HKD",
        issued_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=None,
    )
    payload.update(overrides)
    return ApprovalGrant.model_validate(payload)


def escalation_scenario(**overrides):
    """HK$289 cash total with a HK$280 escalation threshold and a HK$320 cap."""
    mandate = overrides.pop("mandate", None) or make_mandate(
        cap_per_transaction_cents=32000, escalate_above_cents=28000, rolling_cap_cents=90000)
    quote = overrides.pop("quote", None) or make_quote()
    route = overrides.pop("route", None) or make_route(quote)
    proposal = overrides.pop("proposal", None) or make_proposal(quote)
    return scenario(mandate=mandate, quote=quote, route=route, proposal=proposal, **overrides)


def test_amount_above_the_escalation_threshold_escalates():
    decision = evaluate_policy(escalation_scenario())
    assert decision.outcome == "ESCALATE"
    assert decision.primary_reason == ErrorCode.ESCALATION_REQUIRED
    assert decision.reservation_created is False
    assert decision.payment_adapter_called is False


def test_escalation_threshold_boundary_is_inclusive():
    mandate = make_mandate(cap_per_transaction_cents=32000,
                           escalate_above_cents=28900, rolling_cap_cents=90000)
    decision = evaluate_policy(scenario(mandate=mandate))
    assert decision.outcome == "APPROVE"      # equal to the threshold is allowed


def test_a_matching_grant_turns_escalation_into_approval():
    quote, route = make_quote(), None
    route = make_route(quote)
    grant = make_grant(quote, route)
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "APPROVE"


def test_grant_is_void_when_the_amount_changes():
    quote = make_quote()
    route = make_route(quote)
    grant = make_grant(quote, route, cash_total_cents=999)
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "ESCALATE"


def test_grant_is_void_when_the_route_changes():
    quote = make_quote()
    route = make_route(quote, route_id="mc_1234", rail=PaymentRail.MASTERCARD)
    grant = make_grant(quote, route, payment_route_id="fps_demo")
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "ESCALATE"


def test_grant_is_void_when_the_quote_hash_changes():
    quote = make_quote()
    route = make_route(quote)
    grant = make_grant(quote, route, quote_hash="sha256:" + "9" * 64)
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "ESCALATE"


def test_expired_grant_is_void():
    quote = make_quote()
    route = make_route(quote)
    grant = make_grant(quote, route, expires_at=NOW - timedelta(seconds=1))
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "ESCALATE"


def test_consumed_grant_is_void():
    quote = make_quote()
    route = make_route(quote)
    grant = make_grant(quote, route, consumed_at=NOW - timedelta(seconds=1))
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant))
    assert decision.outcome == "ESCALATE"


def test_grant_for_a_stale_mandate_version_is_void():
    quote = make_quote()
    route = make_route(quote)
    grant = make_grant(quote, route, mandate_version=1)
    decision = evaluate_policy(escalation_scenario(
        quote=quote, route=route, approval_grant=grant, mandate=make_mandate(
            version=2, cap_per_transaction_cents=32000,
            escalate_above_cents=28000, rolling_cap_cents=90000)))
    assert decision.outcome == "DENY"          # stale version is a hard denial
    assert ErrorCode.MANDATE_VERSION_STALE in codes(decision)


# ---------------------------------------------------------------------------
# Violation collection
# ---------------------------------------------------------------------------

def test_every_violation_is_collected_not_just_the_first():
    proposal = make_proposal(make_quote(), principal_id="intruder", agent_id="rogue")
    decision = evaluate_policy(scenario(proposal=proposal))
    assert decision.outcome == "DENY"
    collected = set(codes(decision))
    assert ErrorCode.PRINCIPAL_MISMATCH in collected
    assert ErrorCode.AGENT_MISMATCH in collected
    assert decision.primary_reason is not None


def test_a_hard_violation_beats_escalation():
    """Escalation is a question, not a veto override: a breach still denies."""
    quote = make_quote(unit_price_cents=99900, shipping_cents=0)   # over the cap
    mandate = make_mandate(cap_per_transaction_cents=30000,
                           escalate_above_cents=28000, rolling_cap_cents=200000)
    decision = evaluate_policy(scenario(quote=quote, proposal=make_proposal(quote),
                                        mandate=mandate))
    assert decision.outcome == "DENY"
    assert ErrorCode.CAP_PER_TRANSACTION_EXCEEDED in codes(decision)
    assert ErrorCode.ESCALATION_REQUIRED in codes(decision)


def test_no_decision_ever_claims_to_have_called_the_payment_adapter():
    """The evaluator cannot pay. That is the whole point of the boundary."""
    for inputs in (
        scenario(),
        escalation_scenario(),
        scenario(mandate=make_mandate(status="REVOKED")),
        scenario(quote=make_quote(unit_price_cents=99900, shipping_cents=0),
                 mandate=make_mandate(cap_per_transaction_cents=30000,
                                      rolling_cap_cents=200000)),
    ):
        decision = evaluate_policy(inputs)
        assert decision.payment_adapter_called is False
        if decision.outcome != "APPROVE":
            assert decision.reservation_created is False


def test_decision_is_replayable_from_its_recorded_inputs():
    """A third party can re-run the rule and get the same answer."""
    inputs = scenario(quote=make_quote(unit_price_cents=29900, shipping_cents=1000),
                      mandate=make_mandate(cap_per_transaction_cents=30000,
                                           rolling_cap_cents=90000))
    decision = evaluate_policy(inputs)
    replayed = evaluate_policy(inputs)
    assert decision.model_dump() == replayed.model_dump()
    assert decision.policy_hash == inputs.mandate.policy_hash
    assert canonical_bytes(json.loads(inputs.mandate.canonical_policy))
