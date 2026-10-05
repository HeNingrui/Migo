"""Exposure, and the guard that decides whether the exposure is even looked at.

This is the regression suite for the defect the stand-in shipped with, so it is
worth stating exactly what broke.

``SpendState`` carries ``rolling_window_start``, which is *not* the start of the
window being judged -- it is the caller's record of when the counts it supplies
begin. The evaluator uses it for one thing::

    if now - rolling_window_start < mandate.rolling_window_seconds:
        compare exposure against the rolling cap
    else:
        treat exposure as zero          # the snapshot has aged out

The stand-in set it to ``now - rolling_window_seconds``, making that difference
exactly equal to the window. The guard was false, the comparison was skipped, and
the rolling cap could not fire no matter what the exposure was. Fixing the
exposure arithmetic without fixing the window start would have changed nothing
observable, which is why both are tested here and the guard is asserted directly
rather than only through its effect.
"""

from __future__ import annotations

from datetime import timedelta

from app.commerce.policy_evaluator import PolicyInputs, evaluate_policy
from app.commerce.quote_service import SandboxPaymentRouter
from app.commerce.spend_state import SpendStateService
from app.contracts.common import ErrorCode
from tests.commerce.conftest import HP_APPROVE, NOW, a_proposal


def buy(commerce, mandate, product_id: str, proposal_id: str, *, attempt: str = "1",
        now=NOW):
    quote = commerce.create_quote(product_id=product_id, now=now)
    proposal = a_proposal(mandate, quote, proposal_id=proposal_id, attempt=attempt)
    decision = commerce.submit_proposal(proposal, now=now)
    return quote, proposal, decision


class TestTheWindowStartGuard:
    """The half of the defect that no exposure assertion would have caught."""

    def test_an_empty_mandate_still_gets_its_cap_compared(self, commerce, mandate):
        """``now`` is the right answer when there is nothing to count.

        Setting the start a full window in the past makes the evaluator skip the
        comparison; leaving it unset is not possible. An empty exposure and a
        skipped check produce the same decision until the day they do not, which
        is why the guard is asserted and not just the total.
        """
        state = commerce.spend_state(mandate, now=NOW)
        assert state.exposure_cents == 0
        assert NOW - state.rolling_window_start < timedelta(
            seconds=mandate.rolling_window_seconds), (
            "the freshness guard would be false, so the evaluator would treat the "
            "exposure as stale and skip the rolling cap comparison entirely"
        )
        assert NOW - state.velocity_window_start < timedelta(
            seconds=mandate.velocity_window_seconds)

    def test_a_recorded_purchase_starts_the_window_at_its_own_instant(
        self, commerce, mandate
    ):
        buy(commerce, mandate, HP_APPROVE, "prop_win")
        state = commerce.spend_state(mandate, now=NOW)
        assert state.rolling_window_start == NOW
        assert NOW - state.rolling_window_start < timedelta(
            seconds=mandate.rolling_window_seconds)

    def test_a_first_purchase_is_compared_against_the_cap(self, commerce, mandate):
        """A cap of zero must refuse the very first purchase.

        With the guard broken this passes for the wrong reason -- and so does a
        genuine overspend, which is the failure that matters.
        """
        from tests.commerce.conftest import a_draft

        tiny = commerce.activate_mandate(
            a_draft(cap_per_transaction_cents=0, rolling_cap_cents=0,
                    escalate_above_cents=None),
            principal_id="demo_user", agent_id="demo_agent", now=NOW,
        )
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        state = commerce.spend_state(tiny, now=NOW)
        decision = evaluate_policy(PolicyInputs(
            proposal=a_proposal(tiny, quote), mandate=tiny, quote=quote,
            route=SandboxPaymentRouter().choose(quote, tiny, []),
            spend_state=state, now=NOW,
        ))
        assert decision.outcome == "DENY"
        assert ErrorCode.ROLLING_CAP_EXCEEDED in {v.code for v in decision.violations}


class TestExposure:
    def test_a_hold_counts_before_the_money_moves(self, commerce, mandate):
        """Held amounts are counted, not just settled ones.

        Counting only settled spending is how two concurrent proposals each
        observe the full remaining budget and the pair overspends.
        """
        from app.commerce.database import write_transaction
        from app.commerce.repositories import ReservationRepository
        from app.contracts.commerce import Reservation

        with write_transaction(commerce._db_path) as conn:
            ReservationRepository().add(conn, Reservation(
                reservation_id="res_held", proposal_id="prop_held",
                principal_id=mandate.principal_id, mandate_id=mandate.mandate_id,
                mandate_version=mandate.version, quote_id="q_x",
                quote_hash="sha256:x", payment_route_id="fps_demo",
                amount_cents=12345, currency="HKD", quantity=2, status="ACTIVE",
                created_at=NOW, expires_at=NOW + timedelta(seconds=600),
            ))
        state = commerce.spend_state(mandate, now=NOW)
        assert state.exposure_cents == 12345
        assert state.exposure_count == 1
        assert state.exposure_quantity == 2

    def test_a_settled_purchase_is_exposure(self, commerce, mandate):
        buy(commerce, mandate, HP_APPROVE, "prop_settled")
        state = commerce.spend_state(mandate, now=NOW)
        assert state.exposure_cents == 28900
        assert state.settled_cents == 28900
        assert state.reserved_cents == 0
        assert state.exposure_count == 1
        assert state.purchased_quantity == 1

    def test_the_rolling_cap_reflects_what_was_already_committed(
        self, commerce, mandate
    ):
        """Two that fit inside the rolling window, and a third that does not.

        ``cap`` HK$320 and rolling HK$600 against a HK$289 landed cost: the pair
        is HK$578 and the third would be HK$867. The two are ten minutes apart,
        so the five-minute velocity window is empty -- which is what makes this a
        test of the rolling cap rather than of the pair of them together.
        """
        buy(commerce, mandate, HP_APPROVE, "prop_a", now=NOW)
        later = NOW + timedelta(seconds=600)
        buy(commerce, mandate, HP_APPROVE, "prop_b", now=later)

        checked = NOW + timedelta(seconds=1200)
        state = commerce.spend_state(mandate, now=checked)
        assert state.exposure_cents == 57800
        assert state.exposure_count == 0, "both are outside the velocity window"

        quote = commerce.create_quote(product_id=HP_APPROVE, now=checked)
        decision = evaluate_policy(PolicyInputs(
            proposal=a_proposal(mandate, quote, proposal_id="prop_c", created_at=checked),
            mandate=mandate, quote=quote,
            route=SandboxPaymentRouter().choose(quote, mandate, []),
            spend_state=state, now=checked,
        ))
        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.ROLLING_CAP_EXCEEDED

    def test_exposure_ages_out_of_the_window(self, commerce, mandate):
        """The window slides, and the counts are recomputed for it.

        The contract states the obligation: the caller recomputes over
        ``now - window_seconds`` rather than accumulating since the window
        started. A caller that accumulates produces plausible, wrong values, so
        this is checked at the layer that owns the obligation.
        """
        buy(commerce, mandate, HP_APPROVE, "prop_old")
        assert commerce.spend_state(mandate, now=NOW).exposure_cents == 28900

        later = NOW + timedelta(seconds=mandate.rolling_window_seconds + 1)
        aged = SpendStateService(db_path=commerce._db_path).snapshot(mandate, now=later)
        assert aged.exposure_cents == 0
        assert aged.settled_cents == 0

    def test_lifetime_quantity_does_not_reset_with_the_window(self, commerce, mandate):
        """"Buy me one pair" is not a 24-hour instruction.

        The cents fields are windowed and the quantity fields are not, and the
        contract's field names are what say so.
        """
        buy(commerce, mandate, HP_APPROVE, "prop_q")
        later = NOW + timedelta(seconds=mandate.rolling_window_seconds + 1)
        aged = SpendStateService(db_path=commerce._db_path).snapshot(mandate, now=later)
        assert aged.exposure_cents == 0
        assert aged.purchased_quantity == 1, (
            "max_quantity_total is a lifetime limit; expiring it with the rolling "
            "window would let a mandate be spent down in instalments"
        )

    def test_the_velocity_window_is_shorter_than_the_rolling_one(self, commerce, mandate):
        buy(commerce, mandate, HP_APPROVE, "prop_v")
        twenty_minutes = NOW + timedelta(seconds=1200)
        state = SpendStateService(db_path=commerce._db_path).snapshot(
            mandate, now=twenty_minutes)
        assert state.exposure_count == 0, "outside the 5-minute velocity window"
        assert state.settled_cents == 28900, "still inside the 24-hour rolling window"

    def test_velocity_fires_inside_its_window(self, commerce, mandate):
        """Two purchases inside five minutes, and the third is refused.

        This is the check the stand-in could not demonstrate at all, because it
        reported an exposure count of zero forever. Velocity is evaluated before
        the amount caps, so it is also the named reason rather than the rolling
        breach the same third purchase would cause.
        """
        buy(commerce, mandate, HP_APPROVE, "prop_v1")
        buy(commerce, mandate, HP_APPROVE, "prop_v2", attempt="2")
        state = commerce.spend_state(mandate, now=NOW)
        assert state.exposure_count == 2
        assert state.exposure_cents == 57800

        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        decision = evaluate_policy(PolicyInputs(
            proposal=a_proposal(mandate, quote, proposal_id="prop_v3"),
            mandate=mandate, quote=quote,
            route=SandboxPaymentRouter().choose(quote, mandate, []),
            spend_state=state, now=NOW,
        ))
        assert decision.outcome == "DENY"
        assert decision.primary_reason is ErrorCode.VELOCITY_LIMIT_EXCEEDED

    def test_a_second_mandate_is_not_charged_for_the_first(self, commerce):
        """Exposure is per principal and per mandate, not global."""
        from tests.commerce.conftest import a_draft

        first = commerce.activate_mandate(a_draft(), principal_id="demo_user",
                                          agent_id="demo_agent", now=NOW)
        buy(commerce, first, HP_APPROVE, "prop_m1")

        second = commerce.activate_mandate(a_draft(), principal_id="demo_user",
                                           agent_id="demo_agent", now=NOW)
        assert commerce.spend_state(second, now=NOW).exposure_cents == 0
