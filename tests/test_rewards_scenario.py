"""User-provided examples, monthly rounding/cap and uncertain eligibility."""
import pytest
from app.commerce.rewards import RewardRequest, estimate_rewards


ELIGIBLE={"channel":"direct_card", "eligible_transaction":True, "account_valid":True,
    "cards_owned":True,"posted_as_other_online_retail":True,"registration_confirmed":True,
    "registered_on":"2026-04-01","eligible_student_proof":True,"graduation_year":2026,
    "days_until_posted":3}


def calculate(amount, **changes):
    return estimate_rewards(RewardRequest(amount_cents=amount,conditions={**ELIGIBLE,**changes}))


@pytest.mark.parametrize("amount,hsbc,mmp",[(100000,400,5000),(250000,1000,12500),(500000,2000,25000)])
def test_supplied_examples(amount,hsbc,mmp):
    result=calculate(amount)
    assert [c["estimated_reward_cents"] for c in result["cards"]]==[hsbc,mmp]
    assert result["cash_payable_cents"]==amount


def test_basic_rounding_carry_and_extra_cap():
    hsbc,mmp=calculate(199900)["cards"]
    assert hsbc["estimated_reward_cents"]==700 and hsbc["carry_remainder_cents_after"]==24900
    hsbc,mmp=calculate(100,hsbc_carry_cents=24900)["cards"]
    assert hsbc["basic_reward_cents"]==100 and hsbc["carry_remainder_cents_after"]==0
    mmp=calculate(1100000)["cards"][1]
    assert mmp["extra_reward_cents"]==50000 and mmp["estimated_reward_cents"]==54400
    assert calculate(100000,monthly_extra_reward_used_units=500)["cards"][1]["estimated_reward_cents"]==400


@pytest.mark.parametrize("change",[{"channel":"alipay"},{"channel":"wechat"},{"channel":"ewallet"},
    {"days_until_posted":16},{"registration_confirmed":False},{"registered_on":"2026-11-01"},
    {"transaction_date":"2027-01-01"},{"eligible_student_proof":False},
    {"graduation_year":2025},{"posted_as_other_online_retail":False}])
def test_excluded_promotion_keeps_only_explicitly_eligible_basic(change):
    card=calculate(100000,**change)["cards"][1]
    assert card["status"]=="basic_only" and card["extra_reward_cents"]==0
    assert card["estimated_reward_cents"]==400


def test_unknown_eligibility_is_not_five_percent_and_threshold_can_qualify():
    card=calculate(100000,days_until_posted=None)["cards"][1]
    assert card["status"]=="conditional" and card["estimated_reward_cents"] is None
    card=calculate(100000,eligible_student_proof=False,monthly_eligible_retail_cents_before=200000,
                   monthly_other_online_net_cents_before=200000)["cards"][1]
    assert card["status"]=="estimated" and card["extra_reward_cents"]==13800
    assert card["includes_retroactive_monthly_qualification"]


def test_invalid_account_and_strict_minor_units():
    assert all(c["estimated_reward_cents"]==0 for c in calculate(100000,account_valid=False)["cards"])
    for invalid in (True,0,-1,1.5):
        with pytest.raises(ValueError):
            RewardRequest(amount_cents=invalid)


def test_excluded_channel_does_not_turn_unknown_basic_eligibility_into_cash():
    result=estimate_rewards(RewardRequest(amount_cents=100000,conditions={"channel":"alipay"}))
    assert result["best_estimated_card_id"] is None
    assert all(c["estimated_reward_cents"] is None for c in result["cards"])
