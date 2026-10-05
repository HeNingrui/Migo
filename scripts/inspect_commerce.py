"""Print the state of the demo database: mandates, ledger, chain, reconciliation.

    python scripts/inspect_commerce.py
    python scripts/inspect_commerce.py --mandate man_abc123
    python scripts/inspect_commerce.py --db var/demo.sqlite3

Read-only. This exists because the interesting claims are about state that is
otherwise invisible: whether the audit chain still verifies, whether anything is
holding budget with no recorded outcome, and what `SpendState` actually counts.
A demo that asserts those in prose and cannot show them is a demo nobody can
check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commerce.database import read_connection  # noqa: E402
from app.commerce.reconcile import reconcile  # noqa: E402
from app.commerce.repositories import (  # noqa: E402
    MandateRepository,
    OrderRepository,
    PaymentAttemptRepository,
    ReservationRepository,
)
from app.commerce.spend_state import SpendStateService  # noqa: E402
from app.db import database_status, resolve_db_path  # noqa: E402


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    whole, part = divmod(abs(int(cents)), 100)
    return f"{'-' if cents < 0 else ''}HK${whole:,}.{part:02d}"


def describe_mandate(conn, mandate_id: str) -> dict:
    mandates = MandateRepository()
    current = mandates.current(conn, mandate_id)
    if current is None:
        return {"mandate_id": mandate_id, "found": False}
    state = SpendStateService().compute(conn, mandate=current)
    return {
        "mandate_id": mandate_id,
        "found": True,
        "version": current.version,
        "status": current.status,
        "principal_id": current.principal_id,
        "expires_at": current.expires_at.isoformat(),
        "policy_hash": current.policy_hash,
        "limits": {
            "cap_per_transaction_cents": current.cap_per_transaction_cents,
            "rolling_cap_cents": current.rolling_cap_cents,
            "rolling_window_seconds": current.rolling_window_seconds,
            "velocity_max_count": current.velocity_max_count,
            "velocity_window_seconds": current.velocity_window_seconds,
            "max_quantity_total": current.max_quantity_total,
            "escalate_above_cents": current.escalate_above_cents,
        },
        "exposure": {
            "settled_cents": state.settled_cents,
            "captured_cents": state.captured_cents,
            "reserved_cents": state.reserved_cents,
            "exposure_cents": state.exposure_cents,
            "exposure_count": state.exposure_count,
            "exposure_quantity": state.exposure_quantity,
            "rolling_window_start": state.rolling_window_start.isoformat(),
        },
        "history": [
            {"version": m.version, "status": m.status, "created_at": m.created_at.isoformat()}
            for m in mandates.history(conn, mandate_id)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="Database path; defaults to DEMO_DB_PATH")
    parser.add_argument("--mandate", help="Inspect one mandate in detail")
    args = parser.parse_args()

    db_path = resolve_db_path(args.db)
    status = database_status(args.db)
    report = reconcile(args.db)

    summary: dict[str, object] = {
        "db_path": str(db_path),
        "database": status,
        "reconciliation": report.summary(),
    }

    with read_connection(args.db) as conn:
        mandate_ids = [
            row[0] for row in conn.execute(
                "SELECT mandate_id FROM mandates ORDER BY created_at")
        ]
        wallets = [
            {"principal_id": row["principal_id"], "balance_cents": row["balance_cents"]}
            for row in conn.execute(
                "SELECT principal_id, balance_cents FROM wallets ORDER BY principal_id")
        ]
        settled = conn.execute(
            "SELECT count(*), COALESCE(SUM(amount_cents), 0) FROM reservations "
            "WHERE status IN ('SETTLED', 'CAPTURED')"
        ).fetchone()
        summary["wallets"] = wallets
        summary["purchases"] = {"count": settled[0], "cash_total_cents": settled[1]}
        summary["mandates"] = mandate_ids

        if args.mandate:
            summary["mandate"] = describe_mandate(conn, args.mandate)
        elif mandate_ids:
            summary["mandate"] = describe_mandate(conn, mandate_ids[-1])

        if args.mandate:
            reservations = ReservationRepository().not_terminal(conn)
            summary["open_reservations"] = [
                {"reservation_id": r.reservation_id, "proposal_id": r.proposal_id,
                 "status": r.status, "amount_cents": r.amount_cents}
                for r in reservations
            ]
            attempts = PaymentAttemptRepository()
            orders = OrderRepository()
            summary["recent_orders"] = [
                {"order_id": row["order_id"], "status": row["status"]}
                for row in conn.execute(
                    "SELECT order_id, status FROM orders ORDER BY created_at DESC LIMIT 5")
            ]
            assert attempts is not None and orders is not None

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not report.is_clean:
        print(
            "\nWARNING: the reconciliation report is not clean. "
            "Something is holding budget with no recorded outcome, or the chain "
            "does not verify. Do not tidy it away -- read the report.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
