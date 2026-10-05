"""Cumulative exposure, computed from C's own records.

This is the module the plan calls the first priority, and the reason is a
concrete defect rather than a preference. The stand-in returned an all-zero
:class:`~app.contracts.commerce.SpendState`, so three of the evaluator's checks
-- rolling cap, velocity, lifetime quantity -- could never fire in a demo. The
mandate's own limits were enforced by a rule that never saw any spending.

There is a second, quieter defect in the same place, and fixing it is most of
what this module is about. ``rolling_window_start`` is not decoration: the
evaluator asks whether the counts it was handed are *current*::

    if now - rolling_window_start < mandate.rolling_window_seconds:
        compare exposure against the cap
    else:
        treat exposure as zero          # the snapshot has aged out

The stand-in set ``rolling_window_start = now - rolling_window_seconds``, which
makes that difference exactly equal to the window. The guard is false, exposure
is treated as zero, and the comparison is skipped altogether -- so even a
correctly-computed exposure would have been ignored. The start has to be *when
the supplied counts begin*, and this module sets it to the earliest contributing
record, or to ``now`` when there is nothing to count. Both are strictly inside
the window, so the guard holds and the comparison runs.

**Which window applies to which field.** The two windows are different, and the
contract's field names say which is which: the ``*_cents`` fields and the
``*_count`` fields are windowed, the ``*_quantity`` fields are not.

* rolling window -> ``settled_cents`` / ``captured_cents`` / ``reserved_cents``,
  which sum to ``exposure_cents`` and are compared against ``rolling_cap_cents``;
* velocity window -> ``settled_count`` / ``captured_count`` / ``reserved_count``,
  which sum to ``exposure_count`` and are compared against ``velocity_max_count``;
* lifetime -> ``purchased_quantity`` / ``reserved_quantity``, compared against
  ``max_quantity_total``. "Buy me one pair" does not reset every 24 hours.

**Why holds count.** ``HOLDING_STATUSES`` is imported from the contract rather
than spelled out, and it includes ``UNKNOWN`` -- a payment whose outcome is not
recorded. Counting only settled spending is how two concurrent proposals each
observe the full remaining budget: both evaluate, both approve, and together
they overspend. Counting every hold is what makes the pair fail instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.commerce.database import read_connection
from app.commerce.policy_evaluator import as_utc
from app.commerce.repositories import ReservationRepository
from app.contracts.commerce import Reservation, SpendState
from app.contracts.mandate import Mandate

#: Statuses that represent money committed, as opposed to merely held.
_COMMITTED_STATUSES: frozenset[str] = frozenset({"SETTLED", "CAPTURED"})


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class SpendStateService:
    """Builds the only correct input to a cap check."""

    def __init__(self, *, db_path=None) -> None:
        self._db_path = db_path
        self._reservations = ReservationRepository()

    def compute(self, conn, *, mandate: Mandate,
                now: datetime | None = None) -> SpendState:
        """Compute exposure using the caller's connection.

        Taking a connection rather than opening one is the point: the authority
        service calls this *inside* its write transaction, after ``BEGIN
        IMMEDIATE`` has taken the lock and before the reservation is written, so
        the exposure the decision was based on cannot have changed by the time
        the reservation lands.
        """
        moment = as_utc(now or _utc())
        rolling_after = moment - timedelta(seconds=mandate.rolling_window_seconds)
        velocity_after = moment - timedelta(seconds=mandate.velocity_window_seconds)

        # One query bounded by the older of the two windows; the split into
        # rolling and velocity is then plain arithmetic over the same rows, so
        # the two windows cannot be applied to inconsistent data.
        candidates = self._reservations.counted_after(
            conn,
            principal_id=mandate.principal_id,
            mandate_id=mandate.mandate_id,
            after=min(rolling_after, velocity_after),
        )
        rolling = [r for r in candidates if as_utc(_created(r)) > rolling_after]
        velocity = [r for r in candidates if as_utc(_created(r)) > velocity_after]

        purchased, reserved_qty = self._reservations.quantity_totals(
            conn, principal_id=mandate.principal_id, mandate_id=mandate.mandate_id
        )

        return SpendState(
            settled_cents=_sum_cents(rolling, "SETTLED"),
            captured_cents=_sum_cents(rolling, "CAPTURED"),
            reserved_cents=_sum_cents(rolling, None),
            settled_count=_count(velocity, "SETTLED"),
            captured_count=_count(velocity, "CAPTURED"),
            reserved_count=_count(velocity, None),
            purchased_quantity=purchased,
            reserved_quantity=reserved_qty,
            rolling_window_start=_window_start(rolling, moment),
            velocity_window_start=_window_start(velocity, moment),
        )

    def snapshot(self, mandate: Mandate, *, now: datetime | None = None) -> SpendState:
        """Read exposure on a read-only connection, for a status question.

        Deliberately separate from :meth:`compute`: a read may observe a state
        that has since moved on, which is fine for answering "how much is left?"
        and not fine for deciding whether a purchase may proceed.
        """
        with read_connection(self._db_path) as conn:
            return self.compute(conn, mandate=mandate, now=now)


def _created(reservation: Reservation) -> datetime:
    return reservation.created_at


def _contributes(reservation: Reservation, status: str | None) -> bool:
    if status is None:
        return reservation.holds_budget
    return reservation.status == status


def _sum_cents(reservations: list[Reservation], status: str | None) -> int:
    return sum(r.amount_cents for r in reservations if _contributes(r, status))


def _count(reservations: list[Reservation], status: str | None) -> int:
    return sum(1 for r in reservations if _contributes(r, status))


def _window_start(reservations: list[Reservation], moment: datetime) -> datetime:
    """When the counts supplied begin -- and a value the freshness guard accepts.

    ``now`` when there is nothing to count, so that a first purchase is still
    compared against the rolling cap rather than skipping the check because
    nothing has been spent yet. That case is the one a naive implementation gets
    wrong in the direction nobody notices: an empty exposure and a skipped check
    produce the same decision until the day they do not.
    """
    contributing = [as_utc(_created(r)) for r in reservations
                    if r.holds_budget or r.status in _COMMITTED_STATUSES]
    return min(contributing) if contributing else moment


__all__ = ["SpendStateService"]
