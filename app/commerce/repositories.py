"""C's persistence: one small repository per aggregate, and no SQL above this file.

The rule this module exists to enforce is stated in the plan and is worth
repeating, because it is what keeps the transaction boundary visible: **a
service opens the transaction, a repository only uses the connection it is
handed.** No method here commits, rolls back, closes, or opens a connection of
its own. A service that needs two writes to be atomic calls two repositories
with the same connection, and the atomicity is a property of the caller.

Reads reconstruct the contract object from the row's ``payload`` column with
``model_validate_json``, so a row is validated on the way out. That is not
decoration: ``Mandate`` re-checks that ``canonical_policy`` hashes to
``policy_hash``, and ``Quote`` re-checks that the amounts reconcile, so a row
edited in the database fails on read instead of being trusted.

See ``app/commerce/schema.py`` for why a row keeps both columns and a payload.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, NamedTuple

from app.contracts.audit import AuditEvent, AuditEventType
from app.contracts.commerce import (
    HOLDING_STATUSES,
    PurchaseProposal,
    Quote,
    Reservation,
    ReservationStatus,
)
from app.contracts.common import ActorType
from app.contracts.mandate import ApprovalGrant, Mandate, MandateStatus
from app.contracts.policy import (
    DenialReceipt,
    EscalationRequest,
    PaymentReceipt,
    PolicyDecision,
)

#: Reservation states in which the attempt is over and no further movement is
#: expected. Anything outside this set is still holding budget or still in
#: flight, which is what the reconciliation reader looks for.
TERMINAL_RESERVATION_STATUSES: frozenset[str] = frozenset(
    {"CAPTURED", "SETTLED", "RELEASED", "EXPIRED", "FAILED"}
)


def _json(model) -> str:
    return model.model_dump_json()


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class StoredProposal(NamedTuple):
    """A proposal row plus the digest of what it asked for.

    The digest is what distinguishes a replay from a conflict, so the two travel
    together rather than being re-read separately.
    """

    proposal: PurchaseProposal
    request_hash: str


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

class WalletRepository:
    """The principal's balance. Money leaves here, and only here.

    The debit is a conditional UPDATE rather than a read-then-write: two
    concurrent settlements would both see enough money, and the second would
    overdraw. ``balance_cents >= ?`` inside the UPDATE is what makes the check
    and the write one operation instead of two that can be interleaved.
    """

    def ensure(self, conn, *, principal_id: str, currency: str, opening_cents: int,
               now: datetime) -> None:
        conn.execute(
            "INSERT INTO wallets(principal_id, currency, balance_cents, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(principal_id) DO NOTHING",
            (principal_id, currency, opening_cents, now.isoformat()),
        )

    def balance(self, conn, principal_id: str) -> int | None:
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE principal_id = ?", (principal_id,)
        ).fetchone()
        return None if row is None else int(row["balance_cents"])

    def debit_if_sufficient(self, conn, *, principal_id: str, amount_cents: int,
                            now: datetime) -> bool:
        """Take ``amount_cents`` if the balance covers it. False means it did not."""
        cursor = conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ?, updated_at = ? "
            "WHERE principal_id = ? AND balance_cents >= ?",
            (amount_cents, now.isoformat(), principal_id, amount_cents),
        )
        return cursor.rowcount == 1

    def credit(self, conn, *, principal_id: str, amount_cents: int, now: datetime) -> None:
        """Return money.

        Used only when a debit has already landed and a later step of the same
        settlement cannot complete -- never as a refund path, because a refund
        is a different transaction with its own evidence and the plan is
        explicit that a revocation must not be reported as one.
        """
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents + ?, updated_at = ? "
            "WHERE principal_id = ?",
            (amount_cents, now.isoformat(), principal_id),
        )


# ---------------------------------------------------------------------------
# Mandates -- one identity row, one immutable row per version
# ---------------------------------------------------------------------------

class MandateRepository:
    def create(self, conn, mandate: Mandate, *, now: datetime) -> None:
        conn.execute(
            "INSERT INTO mandates(mandate_id, current_version, principal_id, agent_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mandate.mandate_id, mandate.version, mandate.principal_id,
             mandate.agent_id, now.isoformat(), now.isoformat()),
        )
        self.add_version(conn, mandate, now=now)

    def add_version(self, conn, mandate: Mandate, *, now: datetime) -> None:
        conn.execute(
            "INSERT INTO mandate_versions(mandate_id, version, status, principal_id, "
            "agent_id, expires_at, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mandate.mandate_id, mandate.version, mandate.status, mandate.principal_id,
             mandate.agent_id, mandate.expires_at.isoformat(), _json(mandate),
             now.isoformat()),
        )

    def advance(self, conn, *, mandate_id: str, version: int, now: datetime) -> None:
        conn.execute(
            "UPDATE mandates SET current_version = ?, updated_at = ? WHERE mandate_id = ?",
            (version, now.isoformat(), mandate_id),
        )

    def set_version_status(self, conn, *, mandate_id: str, version: int,
                           status: MandateStatus) -> None:
        """Move one version's status forward.

        A revocation adds a new version and marks the previous one SUPERSEDED,
        so this is the only mutation a mandate row ever takes. It is
        forward-only -- ACTIVE becomes SUPERSEDED or REVOKED and never back --
        the policy content is untouched, and the payload is re-validated rather
        than patched, so a status the contract does not allow cannot be written.
        """
        row = conn.execute(
            "SELECT payload FROM mandate_versions WHERE mandate_id = ? AND version = ?",
            (mandate_id, version),
        ).fetchone()
        if row is None:
            return
        previous = Mandate.model_validate_json(row["payload"])
        updated = Mandate.model_validate({**previous.model_dump(), "status": status})
        conn.execute(
            "UPDATE mandate_versions SET status = ?, payload = ? "
            "WHERE mandate_id = ? AND version = ?",
            (updated.status, _json(updated), mandate_id, version),
        )

    @staticmethod
    def _to_mandate(row) -> Mandate:
        return Mandate.model_validate_json(row["payload"])

    def current(self, conn, mandate_id: str) -> Mandate | None:
        """The version in force: the highest one recorded for this mandate."""
        row = conn.execute(
            "SELECT v.payload FROM mandates m "
            "JOIN mandate_versions v "
            "  ON v.mandate_id = m.mandate_id AND v.version = m.current_version "
            "WHERE m.mandate_id = ?",
            (mandate_id,),
        ).fetchone()
        return None if row is None else self._to_mandate(row)

    def history(self, conn, mandate_id: str) -> list[Mandate]:
        rows = conn.execute(
            "SELECT payload FROM mandate_versions WHERE mandate_id = ? ORDER BY version",
            (mandate_id,),
        ).fetchall()
        return [self._to_mandate(row) for row in rows]


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

class QuoteRepository:
    def add(self, conn, quote: Quote) -> None:
        conn.execute(
            "INSERT INTO quotes(quote_id, merchant_id, product_id, quantity, "
            "merchant_total_cents, currency, quote_hash, issued_at, expires_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (quote.quote_id, quote.merchant_id, quote.product_id, quote.quantity,
             quote.merchant_total_cents, quote.currency, quote.quote_hash,
             quote.issued_at.isoformat(), quote.expires_at.isoformat(), _json(quote)),
        )

    def get(self, conn, quote_id: str) -> Quote | None:
        row = conn.execute(
            "SELECT payload FROM quotes WHERE quote_id = ?", (quote_id,)
        ).fetchone()
        return None if row is None else Quote.model_validate_json(row["payload"])


# ---------------------------------------------------------------------------
# Proposals and the idempotency record
# ---------------------------------------------------------------------------

class ProposalRepository:
    def add(self, conn, proposal: PurchaseProposal, *, request_hash: str,
            submitted_at: datetime) -> None:
        conn.execute(
            "INSERT INTO purchase_proposals(proposal_id, mandate_id, "
            "expected_mandate_version, principal_id, agent_id, product_id, quantity, "
            "merchant_id, quote_id, idempotency_key, request_hash, submitted_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (proposal.proposal_id, proposal.mandate_id, proposal.expected_mandate_version,
             proposal.principal_id, proposal.agent_id, proposal.product_id,
             proposal.quantity, proposal.merchant_id, proposal.quote_id,
             proposal.idempotency_key, request_hash, submitted_at.isoformat(),
             _json(proposal)),
        )

    def get(self, conn, proposal_id: str) -> PurchaseProposal | None:
        row = conn.execute(
            "SELECT payload FROM purchase_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else PurchaseProposal.model_validate_json(row["payload"])

    def by_idempotency_key(self, conn, key: str) -> StoredProposal | None:
        row = conn.execute(
            "SELECT payload, request_hash FROM purchase_proposals WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return StoredProposal(
            proposal=PurchaseProposal.model_validate_json(row["payload"]),
            request_hash=row["request_hash"],
        )

    def request_hash(self, conn, proposal_id: str) -> str | None:
        row = conn.execute(
            "SELECT request_hash FROM purchase_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return None if row is None else row["request_hash"]


# ---------------------------------------------------------------------------
# Decisions, denials, escalations, grants
# ---------------------------------------------------------------------------

class DecisionRepository:
    """Append-only, because one proposal legitimately has several decisions.

    An escalated proposal that the principal then approves is evaluated twice.
    Overwriting the first decision would erase the evidence that the agent
    stopped and asked, which is the thing the demo exists to show.
    """

    def add(self, conn, decision_id: str, decision: PolicyDecision) -> None:
        conn.execute(
            "INSERT INTO policy_decisions(decision_id, proposal_id, outcome, "
            "cash_total_cents, evaluated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, decision.proposal_id, decision.outcome,
             decision.cash_total_cents, decision.evaluated_at.isoformat(), _json(decision)),
        )

    def latest(self, conn, proposal_id: str) -> PolicyDecision | None:
        row = conn.execute(
            "SELECT payload FROM policy_decisions WHERE proposal_id = ? "
            "ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return None if row is None else PolicyDecision.model_validate_json(row["payload"])

    def all_for(self, conn, proposal_id: str) -> list[PolicyDecision]:
        rows = conn.execute(
            "SELECT payload FROM policy_decisions WHERE proposal_id = ? "
            "ORDER BY evaluated_at, rowid",
            (proposal_id,),
        ).fetchall()
        return [PolicyDecision.model_validate_json(row["payload"]) for row in rows]


class DenialRepository:
    def add(self, conn, denial: DenialReceipt) -> None:
        conn.execute(
            "INSERT INTO denial_receipts(denial_id, proposal_id, primary_reason, "
            "created_at, payload) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(proposal_id) DO NOTHING",
            (denial.denial_id, denial.proposal_id, denial.primary_reason.value,
             denial.created_at.isoformat(), _json(denial)),
        )

    def get(self, conn, proposal_id: str) -> DenialReceipt | None:
        row = conn.execute(
            "SELECT payload FROM denial_receipts WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else DenialReceipt.model_validate_json(row["payload"])


class EscalationRepository:
    def add(self, conn, escalation: EscalationRequest) -> None:
        conn.execute(
            "INSERT INTO escalation_requests(escalation_id, proposal_id, created_at, "
            "expires_at, resolved_at, resolution, payload) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(proposal_id) DO NOTHING",
            (escalation.escalation_id, escalation.proposal_id,
             escalation.created_at.isoformat(), escalation.expires_at.isoformat(),
             escalation.resolved_at.isoformat() if escalation.resolved_at else None,
             escalation.resolution, _json(escalation)),
        )

    def get(self, conn, proposal_id: str) -> EscalationRequest | None:
        row = conn.execute(
            "SELECT payload FROM escalation_requests WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else EscalationRequest.model_validate_json(row["payload"])

    def open_for(self, conn, proposal_id: str) -> EscalationRequest | None:
        """The escalation still awaiting an answer, if there is one."""
        row = conn.execute(
            "SELECT payload FROM escalation_requests "
            "WHERE proposal_id = ? AND resolved_at IS NULL",
            (proposal_id,),
        ).fetchone()
        return None if row is None else EscalationRequest.model_validate_json(row["payload"])

    def resolve(self, conn, *, proposal_id: str, resolution: str,
                resolved_at: datetime) -> None:
        """Record the principal's answer, or a timeout.

        The one place a contract object is updated after creation, which the
        ownership matrix states explicitly: ``EscalationRequest`` is the only
        object C produces and may still change, and it may only change these two
        fields.
        """
        row = conn.execute(
            "SELECT payload FROM escalation_requests WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return
        updated = EscalationRequest.model_validate({
            **EscalationRequest.model_validate_json(row["payload"]).model_dump(),
            "resolution": resolution,
            "resolved_at": resolved_at,
        })
        conn.execute(
            "UPDATE escalation_requests SET resolved_at = ?, resolution = ?, payload = ? "
            "WHERE proposal_id = ?",
            (updated.resolved_at.isoformat() if updated.resolved_at else None,
             updated.resolution, _json(updated), proposal_id),
        )


class GrantRepository:
    def add(self, conn, grant: ApprovalGrant) -> None:
        conn.execute(
            "INSERT INTO approval_grants(grant_id, proposal_id, mandate_id, "
            "mandate_version, quote_hash, payment_route_id, cash_total_cents, issued_at, "
            "expires_at, consumed_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (grant.grant_id, grant.proposal_id, grant.mandate_id, grant.mandate_version,
             grant.quote_hash, grant.payment_route_id, grant.cash_total_cents,
             grant.issued_at.isoformat(), grant.expires_at.isoformat(), _json(grant)),
        )

    def usable_for(self, conn, proposal_id: str) -> ApprovalGrant | None:
        """The newest unconsumed grant for a proposal, if there is one.

        Consumption is what removes a grant from this result, so the evaluator's
        view and the single-use rule cannot disagree about whether one exists.
        """
        row = conn.execute(
            "SELECT payload FROM approval_grants WHERE proposal_id = ? AND consumed_at IS NULL "
            "ORDER BY issued_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return None if row is None else ApprovalGrant.model_validate_json(row["payload"])

    def consume(self, conn, *, proposal_id: str, consumed_at: datetime) -> ApprovalGrant | None:
        """Spend the grant, once.

        The conditional ``consumed_at IS NULL`` in the UPDATE, and the rowcount
        check after it, is the whole mechanism: a second caller matches no row
        and gets ``None``, so "approved once, used once" is enforced by the
        database rather than by the order in which callers happen to run. The
        evaluator only ever reads a grant, which is why this cannot live there.
        """
        row = conn.execute(
            "SELECT grant_id, payload FROM approval_grants "
            "WHERE proposal_id = ? AND consumed_at IS NULL "
            "ORDER BY issued_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        consumed = ApprovalGrant.model_validate({
            **ApprovalGrant.model_validate_json(row["payload"]).model_dump(),
            "consumed_at": consumed_at,
        })
        cursor = conn.execute(
            "UPDATE approval_grants SET consumed_at = ?, payload = ? "
            "WHERE grant_id = ? AND consumed_at IS NULL",
            (consumed_at.isoformat(), _json(consumed), row["grant_id"]),
        )
        return consumed if cursor.rowcount == 1 else None


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

class ReservationRepository:
    def add(self, conn, reservation: Reservation) -> None:
        conn.execute(
            "INSERT INTO reservations(reservation_id, proposal_id, principal_id, mandate_id, "
            "mandate_version, quote_id, quote_hash, payment_route_id, amount_cents, quantity, "
            "currency, status, created_at, expires_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reservation.reservation_id, reservation.proposal_id, reservation.principal_id,
             reservation.mandate_id, reservation.mandate_version, reservation.quote_id,
             reservation.quote_hash, reservation.payment_route_id, reservation.amount_cents,
             reservation.quantity, reservation.currency, reservation.status,
             reservation.created_at.isoformat(), reservation.expires_at.isoformat(),
             _json(reservation)),
        )

    def get(self, conn, reservation_id: str) -> Reservation | None:
        row = conn.execute(
            "SELECT payload FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        return None if row is None else Reservation.model_validate_json(row["payload"])

    def for_proposal(self, conn, proposal_id: str) -> Reservation | None:
        row = conn.execute(
            "SELECT payload FROM reservations WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else Reservation.model_validate_json(row["payload"])

    def set_status(self, conn, *, reservation_id: str, status: ReservationStatus) -> None:
        row = conn.execute(
            "SELECT payload FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            return
        updated = Reservation.model_validate({
            **Reservation.model_validate_json(row["payload"]).model_dump(), "status": status,
        })
        conn.execute(
            "UPDATE reservations SET status = ?, payload = ? WHERE reservation_id = ?",
            (updated.status, _json(updated), reservation_id),
        )

    def holding(self, conn, *, principal_id: str, mandate_id: str) -> list[Reservation]:
        """Every reservation that still holds budget for this mandate.

        ``HOLDING_STATUSES`` is imported rather than spelled out here so that
        adding a holding state to the contract cannot leave this query behind --
        and a reservation that stopped counting without being released is
        exactly how a cap check starts passing for the wrong reason.
        """
        placeholders = ", ".join("?" for _ in HOLDING_STATUSES)
        rows = conn.execute(
            f"SELECT payload FROM reservations WHERE principal_id = ? AND mandate_id = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at",
            (principal_id, mandate_id, *sorted(HOLDING_STATUSES)),
        ).fetchall()
        return [Reservation.model_validate_json(row["payload"]) for row in rows]

    def counted_after(self, conn, *, principal_id: str, mandate_id: str,
                      after: datetime) -> list[Reservation]:
        """Reservations taken strictly after a window boundary.

        ``after`` is compared on ``created_at`` rather than on a settlement
        timestamp on purpose: the window has to answer "how much did this
        mandate commit in the last N seconds?", and a hold taken inside the
        window and settled after it still committed the money then.

        The comparison is strict at the lower edge, and that is load-bearing
        rather than tidy. ``SpendState.rolling_window_start`` is what the
        evaluator uses to decide whether the counts it was handed are current
        (``now - rolling_window_start < window``). A row sitting exactly on the
        boundary would make that difference equal to the window, so the
        evaluator would conclude the snapshot was stale and treat exposure as
        zero -- skipping the cap comparison entirely, which is the failure mode
        this whole service exists to remove.
        """
        rows = conn.execute(
            "SELECT payload FROM reservations WHERE principal_id = ? AND mandate_id = ? "
            "AND created_at > ? ORDER BY created_at",
            (principal_id, mandate_id, after.isoformat()),
        ).fetchall()
        return [Reservation.model_validate_json(row["payload"]) for row in rows]

    def quantity_totals(self, conn, *, principal_id: str, mandate_id: str) -> tuple[int, int]:
        """``(purchased, reserved)`` quantities over the mandate's whole life.

        Summed in SQL and unbounded in time, because ``max_quantity_total`` is a
        lifetime limit rather than a windowed one: "buy me one pair" does not
        reset every 24 hours.
        """
        placeholders = ", ".join("?" for _ in HOLDING_STATUSES)
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CAPTURED') THEN quantity ELSE 0 END), 0), "
            f"COALESCE(SUM(CASE WHEN status IN ({placeholders}) THEN quantity ELSE 0 END), 0) "
            "FROM reservations WHERE principal_id = ? AND mandate_id = ?",
            (*sorted(HOLDING_STATUSES), principal_id, mandate_id),
        ).fetchone()
        return int(row[0]), int(row[1])

    def settled_for_mandate(self, conn, mandate_id: str) -> list[Reservation]:
        """Reservations under this mandate that have already taken money.

        Used when a mandate is revoked: money that has moved cannot be recalled
        by withdrawing permission, and the plan requires that to be recorded as
        a missed revocation rather than reported as a cancellation.
        """
        rows = conn.execute(
            "SELECT payload FROM reservations WHERE mandate_id = ? "
            "AND status IN ('SETTLED', 'CAPTURED') ORDER BY created_at",
            (mandate_id,),
        ).fetchall()
        return [Reservation.model_validate_json(row["payload"]) for row in rows]

    def not_terminal(self, conn, *, principal_id: str | None = None) -> list[Reservation]:
        """Reservations that are neither settled nor released.

        The reconciliation reader: a row here is budget that is held by an
        attempt whose outcome is not recorded, which is precisely the state the
        plan says must never be quietly cleaned up.
        """
        placeholders = ", ".join("?" for _ in TERMINAL_RESERVATION_STATUSES)
        sql = (
            f"SELECT payload FROM reservations WHERE status NOT IN ({placeholders})"
        )
        params: tuple[Any, ...] = (*sorted(TERMINAL_RESERVATION_STATUSES),)
        if principal_id is not None:
            sql += " AND principal_id = ?"
            params = (*params, principal_id)
        rows = conn.execute(sql + " ORDER BY created_at", params).fetchall()
        return [Reservation.model_validate_json(row["payload"]) for row in rows]


# ---------------------------------------------------------------------------
# Capability nonces
# ---------------------------------------------------------------------------

class CapabilityNonceRepository:
    """The single-use record behind a payment capability.

    The token itself is never stored -- it is a bearer credential, and a copy in
    the database is a copy that leaks. What is stored is the nonce it carries
    and a digest of the transaction context it was bound to, which is enough to
    detect a replay and a substitution without holding anything worth stealing.
    The audit chain is under the same rule, and the contract states it: the
    chain is meant to be shown to people.
    """

    def issue(self, conn, *, nonce: str, reservation_id: str, proposal_id: str,
              context_hash: str, issued_at: datetime, expires_at: datetime) -> None:
        conn.execute(
            "INSERT INTO capability_nonces(nonce, reservation_id, proposal_id, context_hash, "
            "issued_at, expires_at, consumed_at, payment_id) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
            (nonce, reservation_id, proposal_id, context_hash,
             issued_at.isoformat(), expires_at.isoformat()),
        )

    def get(self, conn, nonce: str) -> Any:
        return conn.execute(
            "SELECT nonce, reservation_id, proposal_id, context_hash, issued_at, expires_at, "
            "consumed_at, payment_id FROM capability_nonces WHERE nonce = ?",
            (nonce,),
        ).fetchone()

    def consume(self, conn, *, nonce: str, payment_id: str,
                consumed_at: datetime) -> bool:
        """Spend the nonce. False means it was already spent."""
        cursor = conn.execute(
            "UPDATE capability_nonces SET consumed_at = ?, payment_id = ? "
            "WHERE nonce = ? AND consumed_at IS NULL",
            (consumed_at.isoformat(), payment_id, nonce),
        )
        return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Orders and payment attempts
# ---------------------------------------------------------------------------

class OrderRepository:
    def add(self, conn, *, order_id: str, proposal_id: str, principal_id: str,
            merchant_id: str, product_id: str, quantity: int, amount_cents: int,
            currency: str, status: str, now: datetime) -> None:
        conn.execute(
            "INSERT INTO orders(order_id, proposal_id, principal_id, merchant_id, product_id, "
            "quantity, amount_cents, currency, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(proposal_id) DO NOTHING",
            (order_id, proposal_id, principal_id, merchant_id, product_id, quantity,
             amount_cents, currency, status, now.isoformat(), now.isoformat()),
        )

    def set_status(self, conn, *, order_id: str, status: str, now: datetime) -> None:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, now.isoformat(), order_id),
        )

    def for_proposal(self, conn, proposal_id: str) -> Any:
        return conn.execute(
            "SELECT order_id, status FROM orders WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()


class PaymentAttemptRepository:
    """Every settlement attempt, successful or not.

    A failed attempt is stored rather than discarded: "the payment was refused"
    is a fact a reviewer should be able to check, and a system that keeps only
    its successes cannot show that it ever stopped.
    """

    def record_settlement(self, conn, *, attempt_id: str, proposal_id: str,
                          reservation_id: str, provider_reference: str | None,
                          attempted_at: datetime, receipt: PaymentReceipt) -> None:
        conn.execute(
            "INSERT INTO payment_attempts(attempt_id, proposal_id, reservation_id, status, "
            "code, message, retryable, provider_reference, attempted_at, payload) "
            "VALUES (?, ?, ?, 'SETTLED', NULL, NULL, NULL, ?, ?, ?)",
            (attempt_id, proposal_id, reservation_id, provider_reference,
             attempted_at.isoformat(), _json(receipt)),
        )

    def record_failure(self, conn, *, attempt_id: str, proposal_id: str,
                       reservation_id: str, status: str, code: str, message: str,
                       retryable: bool, provider_reference: str | None,
                       attempted_at: datetime) -> None:
        conn.execute(
            "INSERT INTO payment_attempts(attempt_id, proposal_id, reservation_id, status, "
            "code, message, retryable, provider_reference, attempted_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (attempt_id, proposal_id, reservation_id, status, code, message,
             1 if retryable else 0, provider_reference, attempted_at.isoformat()),
        )

    def latest_settled(self, conn, proposal_id: str) -> PaymentReceipt | None:
        row = conn.execute(
            "SELECT payload FROM payment_attempts WHERE proposal_id = ? AND status = 'SETTLED' "
            "ORDER BY attempted_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None or row["payload"] is None:  # pragma: no cover - the filter guards it
            return None
        return PaymentReceipt.model_validate_json(row["payload"])

    def latest_failure(self, conn, proposal_id: str) -> Any:
        return conn.execute(
            "SELECT attempt_id, reservation_id, status, code, message, retryable, "
            "provider_reference, attempted_at FROM payment_attempts "
            "WHERE proposal_id = ? AND status IN ('FAILED', 'UNKNOWN') "
            "ORDER BY attempted_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "sequence, event_id, event_type, actor_type, actor_id, mandate_id, proposal_id, "
    "reservation_id, transaction_id, payload, previous_hash, event_hash, occurred_at"
)


class AuditRepository:
    """The append-only chain.

    There is no update and no delete here, and the database enforces that with
    triggers rather than trusting this class to stay disciplined.
    """

    def head(self, conn) -> AuditEvent | None:
        row = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._to_event(row)

    def add(self, conn, event: AuditEvent) -> None:
        conn.execute(
            "INSERT INTO audit_events(sequence, event_id, event_type, actor_type, actor_id, "
            "mandate_id, proposal_id, reservation_id, transaction_id, payload, previous_hash, "
            "event_hash, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.sequence, event.event_id, event.event_type.value, event.actor_type.value,
             event.actor_id, event.mandate_id, event.proposal_id, event.reservation_id,
             event.transaction_id, _json_payload(event.payload), event.previous_hash,
             event.event_hash, event.occurred_at.isoformat()),
        )

    def all(self, conn) -> list[AuditEvent]:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM audit_events ORDER BY sequence"
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def for_proposal(self, conn, proposal_id: str) -> list[AuditEvent]:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM audit_events WHERE proposal_id = ? ORDER BY sequence",
            (proposal_id,),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def for_mandate(self, conn, mandate_id: str) -> list[AuditEvent]:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM audit_events WHERE mandate_id = ? ORDER BY sequence",
            (mandate_id,),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    @staticmethod
    def _to_event(row) -> AuditEvent:
        return AuditEvent(
            event_id=row["event_id"],
            sequence=row["sequence"],
            event_type=AuditEventType(row["event_type"]),
            actor_type=ActorType(row["actor_type"]),
            actor_id=row["actor_id"],
            mandate_id=row["mandate_id"],
            proposal_id=row["proposal_id"],
            reservation_id=row["reservation_id"],
            transaction_id=row["transaction_id"],
            payload=json.loads(row["payload"]),
            previous_hash=row["previous_hash"],
            event_hash=row["event_hash"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
        )


__all__ = [
    "AuditRepository",
    "CapabilityNonceRepository",
    "DecisionRepository",
    "DenialRepository",
    "EscalationRepository",
    "GrantRepository",
    "MandateRepository",
    "OrderRepository",
    "PaymentAttemptRepository",
    "ProposalRepository",
    "QuoteRepository",
    "ReservationRepository",
    "StoredProposal",
    "TERMINAL_RESERVATION_STATUSES",
    "WalletRepository",
]
