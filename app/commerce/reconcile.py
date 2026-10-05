"""Reconciliation: what C cannot account for, reported rather than tidied away.

Two things can be true of a settlement at the same time, and neither is an
error the system may resolve on its own:

* a reservation is holding budget while no outcome was recorded for it -- the
  process died between claiming the attempt and applying the rail's answer, or
  the rail has not answered yet;
* a payment attempt is ``UNKNOWN`` -- the money may have moved, and the only
  honest thing to do is check against the rail before touching anything.

Both are read-only observations. This module deliberately has no way to release a
hold or retry a payment: the plan's rule is that an unresolved attempt is
*reported*, and a system that quietly cleans one up is a system whose ledger
cannot be trusted after a crash. Resolving one needs a human, or a real
reconciliation against the rail's own statement.

The audit chain is verified in the same report, because "the records are
complete" and "the records are internally consistent" are the two halves of the
same claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.commerce.audit import AuditLog
from app.commerce.database import read_connection
from app.commerce.repositories import ReservationRepository
from app.contracts.audit import VerifyReport
from app.contracts.commerce import Reservation


@dataclass(frozen=True)
class ReconciliationReport:
    """A point-in-time statement of what is not accounted for."""

    checked_at: datetime
    unaccounted_holds: tuple[Reservation, ...]
    chain: VerifyReport

    @property
    def is_clean(self) -> bool:
        """True when every hold has a recorded outcome and the chain verifies."""
        return not self.unaccounted_holds and self.chain.ok

    def summary(self) -> dict[str, object]:
        """A JSON-serialisable form, for the HTTP status view and for scripts."""
        return {
            "checked_at": self.checked_at.isoformat(),
            "clean": self.is_clean,
            "unaccounted_holds": [
                {
                    "reservation_id": r.reservation_id,
                    "proposal_id": r.proposal_id,
                    "status": r.status,
                    "amount_cents": r.amount_cents,
                    "created_at": r.created_at.isoformat(),
                }
                for r in self.unaccounted_holds
            ],
            "audit_chain": {
                "ok": self.chain.ok,
                "checked": self.chain.checked,
                "head_hash": self.chain.head_hash,
                "breaks": [
                    {"sequence": b.sequence, "problem": b.problem, "detail": b.detail}
                    for b in self.chain.breaks
                ],
            },
        }


def reconcile(db_path=None, *, now: datetime | None = None,
              principal_id: str | None = None) -> ReconciliationReport:
    """Read every hold without a recorded outcome, and verify the chain."""
    reservations = ReservationRepository()
    with read_connection(db_path) as conn:
        holds = reservations.not_terminal(conn, principal_id=principal_id)
        chain = AuditLog().verify(conn)
    return ReconciliationReport(
        checked_at=now or datetime.now(timezone.utc),
        unaccounted_holds=tuple(holds),
        chain=chain,
    )


__all__ = ["ReconciliationReport", "reconcile"]
