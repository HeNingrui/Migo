"""Read-only estimates for the user's fixed JD/HKD scenario (2026-10-03).

Rewards never credit the wallet, reduce the quote, or grant payment authority.
Conditions are explicitly supplied simulation assumptions, not bank verification.
"""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool

HSBC_SOURCE = "https://www.hsbc.com.hk/zh-hk/credit-cards/products/student-visa-gold/"
MMP_SOURCE = "https://www.hangseng.com/content/dam/wpb/hase/rwd/personal/cards/pdfs/everyday_tnc_en.pdf"


class RewardConditions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transaction_date: date = date(2026, 10, 3)
    channel: Literal["direct_card", "alipay", "wechat", "ewallet", "unknown"] = "unknown"
    eligible_transaction: StrictBool | None = None
    account_valid: StrictBool | None = None
    cards_owned: StrictBool | None = None
    posted_as_other_online_retail: StrictBool | None = None
    registration_confirmed: StrictBool | None = None
    registered_on: date | None = None
    eligible_student_proof: StrictBool | None = None
    graduation_year: StrictInt | None = Field(default=None, ge=2000, le=2100)
    days_until_posted: StrictInt | None = Field(default=None, ge=0)
    monthly_eligible_retail_cents_before: StrictInt = Field(default=0, ge=0)
    monthly_other_online_net_cents_before: StrictInt = Field(default=0, ge=0)
    monthly_extra_reward_used_units: StrictInt = Field(default=0, ge=0, le=500)
    hsbc_carry_cents: StrictInt = Field(default=0, ge=0, lt=25000)
    mmpower_basic_carry_cents: StrictInt = Field(default=0, ge=0, lt=25000)


class RewardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: StrictInt = Field(gt=0)
    conditions: RewardConditions = Field(default_factory=RewardConditions)


def _basic(amount, carry):
    units, remainder = divmod(amount + carry, 25000)
    return units * 100, remainder


def estimate_rewards(request: RewardRequest):
    amount = request.amount_cents
    c = request.conditions
    unknown = [name for name in ("cards_owned", "account_valid", "eligible_transaction") if getattr(c,name) is None]
    unavailable = [name for name in ("cards_owned", "account_valid", "eligible_transaction") if getattr(c,name) is False]
    hsbc_basic, hsbc_carry = _basic(amount, c.hsbc_carry_cents)
    mmp_basic, mmp_carry = _basic(amount, c.mmpower_basic_carry_cents)
    common_status = "ineligible" if unavailable else "conditional" if unknown else "estimated"
    hsbc = {"card_id": "hsbc_student_gold_visa", "name": "HSBC Student Visa Gold",
        "status": common_status, "basic_reward_cents": hsbc_basic if not unavailable else 0,
        "extra_reward_cents": 0, "estimated_reward_cents": hsbc_basic if common_status=="estimated" else 0 if unavailable else None,
        "carry_remainder_cents_after": hsbc_carry, "missing_conditions": unknown,
        "reasons": unavailable or ["JD headphones paid in HKD: basic reward only; no category multiplier."],
        "source_url": HSBC_SOURCE}

    excluded = list(unavailable)
    missing = list(unknown)
    day = c.transaction_date
    if not date(2026,4,1) <= day <= date(2026,12,31):
        excluded.append("Outside the 2026-04-01 to 2026-12-31 promotion.")
    if c.channel in {"alipay", "wechat", "ewallet"}:
        excluded.append("Wallet channel is excluded from the promotional estimate.")
    elif c.channel == "unknown":
        missing.append("payment_channel")
    if c.posted_as_other_online_retail is False:
        excluded.append("Posting is not classified as other online retail.")
    elif c.posted_as_other_online_retail is None:
        missing.append("posted_merchant_and_channel_classification")
    if c.registration_confirmed is False:
        excluded.append("No successful promotion registration.")
    elif c.registration_confirmed is None or c.registered_on is None:
        missing.append("registration_date")
    elif not date(2026,4,1) <= c.registered_on <= date(2026,12,31) or (c.registered_on.year,c.registered_on.month) > (day.year,day.month):
        excluded.append("Registration month does not cover this transaction.")
    if c.days_until_posted is None:
        missing.append("days_until_posted")
    elif c.days_until_posted > 15:
        excluded.append("Transaction is not posted within 15 days.")
    student_waiver = c.eligible_student_proof is True and c.graduation_year is not None and day.year <= c.graduation_year
    spend_after = c.monthly_eligible_retail_cents_before + amount
    if not student_waiver and spend_after < 300000:
        if c.eligible_student_proof is None or (c.eligible_student_proof is True and c.graduation_year is None):
            missing.append("student_proof_and_graduation_year")
        else:
            excluded.append("Non-waived monthly eligible retail spending is below HKD 3,000.")
    remaining = 500-c.monthly_extra_reward_used_units
    # Integer dollars from monthly cumulative net spend, then monthly cap.
    net_before = c.monthly_other_online_net_cents_before
    before_units = net_before * 460 // 1000000
    qualifies_before = student_waiver or c.monthly_eligible_retail_cents_before >= 300000
    retroactive = not qualifies_before and not student_waiver and spend_after >= 300000
    if retroactive:
        before_units = 0
    after_units = (net_before+amount) * 460 // 1000000
    extra = min(remaining, max(0,after_units-before_units)) * 100
    status = "ineligible" if unavailable else "conditional" if unknown else "basic_only" if excluded else "conditional" if missing else "estimated"
    basic = mmp_basic if not unavailable else 0
    estimate = 0 if unavailable else None if unknown or status=="conditional" else basic if status=="basic_only" else basic+extra
    mmp = {"card_id": "hangseng_mmpower_mastercard", "name": "Hang Seng MMPOWER World Mastercard",
        "status": status, "basic_reward_cents": basic,
        "extra_reward_cents": extra if status=="estimated" else 0 if excluded else None,
        "estimated_reward_cents": estimate, "conditional_upper_estimate_cents": basic+extra if status=="conditional" else None,
        "carry_remainder_cents_after": mmp_carry, "extra_cap_remaining_units_before": remaining,
        "student_threshold_waived": student_waiver, "includes_retroactive_monthly_qualification": retroactive,
        "missing_conditions": sorted(set(missing)),
        "reasons": excluded or ["Other online retail: basic 0.4% plus extra 4.6%, subject to monthly rounding and the HKD 500 extra cap."],
        "source_url": MMP_SOURCE}
    confirmed = [card for card in (hsbc,mmp) if card["estimated_reward_cents"] is not None and card["status"]!="ineligible"]
    best = max(confirmed,key=lambda card:card["estimated_reward_cents"])["card_id"] if confirmed else None
    return {"amount_cents": amount, "currency": "HKD", "cards": [hsbc,mmp],
        "best_estimated_card_id": best, "cash_payable_cents": amount,
        "reduces_current_payment": False, "credited_to_wallet": False,
        "rule_observation_date": "2026-10-03", "settlement_mode": "simulation",
        "note": "Scenario estimate only. Posting classification and supplied eligibility conditions govern rewards; the estimate is not available balance or payment authority."}
